"""repo-pulse tests with a fake GitHub API (no network). Run from the repo root: python -m pytest tests/"""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "repo-pulse" / "scripts" / "repo_pulse.py"
ZWSP = chr(0x200B)  # built at runtime: no invisible characters in this file
spec = importlib.util.spec_from_file_location("repo_pulse", SCRIPT)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


class FakeGitHub:
    """Serves /repos/... paths from a dict; supports ETag 304s and rate-limit simulation."""

    def __init__(self):
        self.repos = {}
        self.calls = []
        self.rate_limited = False

    def add(self, name, *, release="v1.0.0", ci="success", commits=2, prs=3, stars=100, private=False):
        self.repos[name] = dict(release=release, ci=ci, commits=commits, prs=prs, stars=stars, private=private)

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.rate_limited:
            return 403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1794600000"}, b'{"message":"API rate limit exceeded"}'
        path = url.split("api.github.com", 1)[1].split("?")[0]
        parts = path.strip("/").split("/")
        name = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else ""
        r = next((v for k, v in self.repos.items() if k.lower() == name.lower()), None)
        if r is None:
            return 404, {}, b'{"message":"Not Found"}'
        canon = next(k for k in self.repos if k.lower() == name.lower())
        etag = f'"{canon}-{path}-{json.dumps(r, sort_keys=True)}"'
        if headers.get("If-None-Match") == etag:
            return 304, {"etag": etag, "x-ratelimit-remaining": "55"}, b""
        h = {"etag": etag, "x-ratelimit-remaining": "55"}
        tail = parts[3:]
        if not tail:
            body = {"full_name": canon, "private": r["private"], "default_branch": "main", "html_url": f"https://github.com/{canon}",
                    "stargazers_count": r["stars"], "open_issues_count": 10 + r["prs"], "pushed_at": "2026-11-12T10:00:00Z"}
        elif tail == ["releases", "latest"]:
            if not r["release"]:
                return 404, {}, b'{"message":"Not Found"}'
            body = {"tag_name": r["release"], "name": r["release"], "published_at": "2026-11-11T09:00:00Z",
                    "body": "## Highlights\n\n<b>Faster</b> sync" + ZWSP + " and more", "html_url": "https://x/rel"}
        elif tail == ["commits"]:
            body = [{"sha": f"{i:040x}", "commit": {"message": f"fix: thing {i}\n\nbody"}} for i in range(r["commits"])]
        elif tail == ["actions", "runs"]:
            body = {"workflow_runs": [{"name": "tests", "status": "completed", "conclusion": r["ci"],
                                       "html_url": "https://x/run", "updated_at": "2026-11-12T11:00:00Z"}]}
        elif tail == ["pulls"]:
            body = [{"number": 1}] if r["prs"] else []
            if r["prs"] > 1:
                h["link"] = (f'<https://api.github.com/repos/{canon}/pulls?state=open&per_page=1&page=2>; rel="next", '
                             f'<https://api.github.com/repos/{canon}/pulls?state=open&per_page=1&page={r["prs"]}>; rel="last"')
        else:
            return 404, {}, b"{}"
        return 200, h, json.dumps(body).encode()


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_PULSE_HOME", str(tmp_path))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("REPO_PULSE_GITHUB_TOKEN", raising=False)
    fake = FakeGitHub()
    monkeypatch.setattr(rp, "_request", fake)
    return fake


def run(capsys, *argv):
    rc = rp.main(list(argv))
    return rc, capsys.readouterr().out


def test_add_validates_and_normalizes(gh, capsys):
    gh.add("NousResearch/hermes-agent")
    gh.add("acme/secret", private=True)
    rc, out = run(capsys, "add", "nousresearch/hermes-agent", "acme/secret", "nobody/missing", "not a repo")
    assert rc == 1
    assert "✓ watching NousResearch/hermes-agent" in out
    assert "private repositories are not supported" in out and "not found" in out and "is not owner/repo" in out
    assert rp.watchlist() == ["NousResearch/hermes-agent"]
    rc, out = run(capsys, "add", "https://github.com/NousResearch/hermes-agent")
    assert "already watched" in out


def test_cap_without_token(gh, capsys, monkeypatch):
    monkeypatch.setattr(rp, "MAX_REPOS_ANON", 2)
    for n in ("a/one", "a/two", "a/three"):
        gh.add(n)
    rc, out = run(capsys, "add", "a/one", "a/two", "a/three")
    assert rc == 1 and "watchlist is full (2 repos without a GitHub token)" in out


def test_first_report_sets_baseline_then_detects_changes(gh, capsys):
    gh.add("o/r", release="v1.0.0", ci="success", stars=100, prs=3)
    run(capsys, "add", "o/r")
    rc, out = run(capsys, "report")
    assert rc == 0 and "first check: baseline set" in out and "NEW release" not in out
    assert "Open PRs 3, open issues 10, stars 100" in out
    gh.add("o/r", release="v1.1.0", ci="failure", stars=112, prs=3)
    rc, out = run(capsys, "report", "--json")
    s = json.loads(out)["repos"][0]
    assert s["new_release"] and s["ci_broke"] and s["star_delta"] == 12
    assert s["release"]["notes"] == "Highlights"            # first non-empty line, markdown stripped
    assert {a["kind"] for a in json.loads(out)["alerts"]} == {"new_release", "ci_failed"}


def test_release_notes_are_cleaned():
    assert rp.first_line("<b>Faster</b> sync" + ZWSP + " now") == "Faster sync now"
    assert len(rp.clean("x" * 500)) == 201


def test_alerts_keep_their_own_baseline(gh, capsys):
    gh.add("o/r", release="v1")
    run(capsys, "add", "o/r")
    run(capsys, "alerts")                                    # alerts baseline
    gh.add("o/r", release="v2")
    run(capsys, "report")                                    # a user report sees v2 first...
    rc, out = run(capsys, "alerts", "--json")                # ...the alert job must still fire once
    assert [a["tag"] for a in json.loads(out)["alerts"]] == ["v2"]
    rc, out = run(capsys, "alerts", "--json")
    assert json.loads(out)["alerts"] == []                  # and only once


def test_ci_fixed_is_not_an_alert(gh, capsys):
    gh.add("o/r", ci="failure")
    run(capsys, "add", "o/r")
    run(capsys, "alerts")
    gh.add("o/r", ci="success")
    rc, out = run(capsys, "alerts", "--json")
    assert json.loads(out)["alerts"] == []


def test_etag_304_reuses_cache(gh, capsys):
    gh.add("o/r")
    run(capsys, "add", "o/r")
    run(capsys, "report", "--no-save")
    rc, out = run(capsys, "report", "--json", "--no-save")
    assert json.loads(out)["repos"][0]["stars"] == 100
    assert any("If-None-Match" in h for _, h in gh.calls)


def test_rate_limit_is_reported_not_raised(gh, capsys):
    gh.add("o/r")
    gh.add("o/s")
    run(capsys, "add", "o/r", "o/s")
    gh.rate_limited = True
    rc, out = run(capsys, "report")
    assert rc == 1 and "rate limit reached" in out and "set REPO_PULSE_GITHUB_TOKEN" in out and "skipped: rate limit" in out


def test_token_sent_only_as_header_never_printed(gh, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 36)
    gh.add("o/r")
    rc, out = run(capsys, "add", "o/r")
    assert gh.calls[-1][1]["Authorization"].startswith("Bearer ghp_")
    assert "ghp_" not in out and all(u.startswith("https://api.github.com/") for u, _ in gh.calls)


def test_since_parsing_and_empty_watchlist(gh, capsys):
    assert (rp.now() - rp.parse_since("24h")).total_seconds() == pytest.approx(86400, abs=5)
    rc, out = run(capsys, "report", "--since", "bogus")
    assert rc == 2
    rc, out = run(capsys, "report")
    assert "watchlist is empty" in out


def test_remove_and_list(gh, capsys):
    gh.add("o/r")
    run(capsys, "add", "o/r")
    rc, out = run(capsys, "list", "--json")
    assert json.loads(out) == {"repos": ["o/r"]}
    rc, out = run(capsys, "remove", "O/R")
    assert rc == 0 and rp.watchlist() == []


def test_ci_query_is_push_and_completed_and_alerts_skip_pulls(gh, capsys):
    gh.add("o/r")
    run(capsys, "add", "o/r")
    gh.calls.clear()
    run(capsys, "alerts")
    urls = [u for u, _ in gh.calls]
    runs = next(u for u in urls if "/actions/runs" in u)
    assert "event=push" in runs and "status=completed" in runs
    assert not any("/pulls" in u for u in urls) and len(urls) == 4


def test_since_urls_are_not_cached(gh, capsys):
    gh.add("o/r")
    run(capsys, "add", "o/r")
    run(capsys, "report")
    cache = json.loads((rp.home() / "cache.json").read_text())
    assert cache and not any("since=" in u for u in cache)


def test_repo_with_no_completed_push_runs_has_no_ci(gh, capsys, monkeypatch):
    gh.add("o/r")
    run(capsys, "add", "o/r")
    real = gh.__call__

    def no_runs(url, headers):
        if "/actions/runs" in url:
            return 200, {}, b'{"total_count": 0, "workflow_runs": []}'
        return real(url, headers)
    monkeypatch.setattr(rp, "_request", no_runs)
    rc, out = run(capsys, "report", "--json")
    s = json.loads(out)
    assert rc == 0 and s["errors"] == [] and s["repos"][0]["ci"] is None


def test_unexpected_shape_is_a_per_repo_error(gh, capsys, monkeypatch):
    gh.add("o/r")
    gh.add("o/s")
    run(capsys, "add", "o/r", "o/s")
    real = gh.__call__

    def broken(url, headers):
        if "/repos/o/r/commits" in url:
            return 200, {}, b'[{"no_sha": true}]'
        return real(url, headers)
    monkeypatch.setattr(rp, "_request", broken)
    rc, out = run(capsys, "report", "--json")
    s = json.loads(out)
    assert rc == 1 and [e["repo"] for e in s["errors"]] == ["o/r"] and [x["repo"] for x in s["repos"]] == ["o/s"]


def test_skill_token_name_wins_and_github_token_is_fallback(gh, capsys, monkeypatch):
    gh.add("o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    run(capsys, "add", "o/r")
    assert gh.calls[-1][1]["Authorization"] == "Bearer fallback-token"
    monkeypatch.setenv("REPO_PULSE_GITHUB_TOKEN", "skill-token")
    run(capsys, "list")
    rp.GitHub().get("/repos/o/r")
    assert gh.calls[-1][1]["Authorization"] == "Bearer skill-token"
