#!/usr/bin/env python3
"""repo-pulse: watch public GitHub repos and report what changed since the last check.

Standard library only (Python >= 3.10). No credentials are required; an optional token raises the GitHub
API limit from 60 to 5,000 requests/hour. It is read from REPO_PULSE_GITHUB_TOKEN (the name SKILL.md
declares, because Hermes keeps GITHUB_TOKEN out of terminal commands), else GITHUB_TOKEN. It is only ever
sent to api.github.com and never printed.

  repo_pulse.py add owner/repo [owner/repo ...]   validate (public repos only) and add to the watchlist
  repo_pulse.py remove owner/repo                 remove from the watchlist
  repo_pulse.py list [--json]                     show the watchlist
  repo_pulse.py report [--json] [--since 24h|7d|ISO] [--no-save] [owner/repo ...]
                                                  what changed since the last report
  repo_pulse.py alerts [--json]                   only alert-worthy changes (new release, default-branch CI
                                                  turning from success to failure); keeps its own baseline

State lives in $REPO_PULSE_HOME, else $HERMES_HOME/repo_pulse, else ~/.hermes/repo_pulse:
  watchlist.json  {"repos": ["owner/repo", ...]}
  state.json      per repo: last release tag, head SHA, CI conclusion, stars, check time (+ alert baseline)
  cache.json      ETags and bodies, so unchanged data costs no rate limit (HTTP 304)

Exit codes: 0 ok, 1 some repos could not be checked, 2 usage error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
UA = "repo-pulse/1.0 (+https://github.com/shubh-1912/athena-skills)"
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
MAX_REPOS_ANON, MAX_REPOS_TOKEN = 10, 50
# Invisible/formatting code points removed from GitHub text (soft hyphen, zero-width, bidi controls, BOM).
# Built from numbers so this file contains no invisible characters itself.
_INVISIBLE = re.compile("[" + "".join(re.escape(chr(c)) for c in
                        [0xAD, *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2065),
                         *range(0x2066, 0x206A), 0xFEFF]) + "]")


# --------------------------------------------------------------------------------------------- paths/io
def home() -> Path:
    custom = os.environ.get("REPO_PULSE_HOME")
    if custom:
        return Path(custom)
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "repo_pulse"


def _load(name: str, default):
    try:
        return json.loads((home() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(name: str, data) -> None:
    d = home()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, d / name)


def watchlist() -> list[str]:
    return list(_load("watchlist.json", {"repos": []}).get("repos", []))


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def parse_since(s: str) -> dt.datetime:
    m = re.fullmatch(r"(\d+)\s*([hd])", s.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now() - (dt.timedelta(hours=n) if unit == "h" else dt.timedelta(days=n))
    t = parse_time(s if "T" in s else s + "T00:00:00+00:00")
    if t is None:
        raise ValueError("--since must look like 24h, 7d or an ISO date")
    return t


def clean(text: str | None, limit: int = 200) -> str:
    """One safe line: no HTML/markdown noise, no invisible characters, truncated."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    t = _INVISIBLE.sub("", t)
    t = re.sub(r"[#*_`>\[\]]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def first_line(body: str | None) -> str:
    for line in (body or "").splitlines():
        line = clean(line)
        if line:
            return line
    return ""


# --------------------------------------------------------------------------------------------- github
class GitHubError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(f"GitHub API {status}: {reason}")
        self.status, self.reason = status, reason


def _request(url: str, headers: dict) -> tuple[int, dict, bytes]:
    """(status, lower-case headers, body). Separate so tests can replace it."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(2_000_000)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GitHubError(0, type(e).__name__) from None


class GitHub:
    def __init__(self, token: str | None = None):
        if token is None:
            token = os.environ.get("REPO_PULSE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        self.token = token.strip()
        self.cache: dict = _load("cache.json", {})
        self.calls = 0
        self.remaining: str | None = None

    def get(self, path: str, params: dict | None = None, *, allow_404: bool = False):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": UA,
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        cached = self.cache.get(url)
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        status, h, body = _request(url, headers)
        self.calls += 1
        self.remaining = h.get("x-ratelimit-remaining", self.remaining)
        if status == 304 and cached:
            return cached["data"], cached.get("link", "")
        if status == 404 and allow_404:
            return None, ""
        if status in (403, 429) and h.get("x-ratelimit-remaining") == "0":
            reset = h.get("x-ratelimit-reset", "")
            when = iso(dt.datetime.fromtimestamp(int(reset), dt.timezone.utc)) if reset.isdigit() else "later"
            raise GitHubError(status, f"rate limit reached; resets at {when}"
                              + ("" if self.token else " (set REPO_PULSE_GITHUB_TOKEN for 5,000/hour)"))
        if status >= 400:
            try:
                msg = json.loads(body).get("message", "")
            except ValueError:
                msg = ""
            raise GitHubError(status, clean(msg, 120) or "request failed")
        data = json.loads(body or b"null")
        if h.get("etag") and "since=" not in url:  # `since` changes every run: caching it never hits
            self.cache[url] = {"etag": h["etag"], "data": data, "link": h.get("link", "")}
        return data, h.get("link", "")

    def save_cache(self) -> None:
        if len(self.cache) > 500:  # keep it small
            self.cache = dict(list(self.cache.items())[-300:])
        _save("cache.json", self.cache)


def _last_page(link: str) -> int | None:
    m = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------------------------- core
def validate(gh: GitHub, repo: str) -> str:
    repo = repo.strip().removeprefix("https://github.com/").strip("/")
    if not REPO_RE.match(repo):
        raise ValueError(f"'{repo}' is not owner/repo")
    data, _ = gh.get(f"/repos/{repo}", allow_404=True)
    if data is None:
        raise ValueError(f"{repo}: not found (or private)")
    if data.get("private"):
        raise ValueError(f"{repo}: private repositories are not supported")
    return data["full_name"]


def check(gh: GitHub, repo: str, prev: dict, since: dt.datetime | None, light: bool = False) -> dict:
    """One repo: 5 API calls for a report, 4 for alerts (`light` skips the open-PR count); 304s are free.
    Returns the snapshot + changes vs `prev`."""
    info, _ = gh.get(f"/repos/{repo}")
    branch = info["default_branch"]
    rel, _ = gh.get(f"/repos/{repo}/releases/latest", allow_404=True)
    since = since or parse_time(prev.get("checked_at")) or (now() - dt.timedelta(days=1))
    commits, link = gh.get(f"/repos/{repo}/commits", {"sha": branch, "since": iso(since), "per_page": 100})
    # CI = the latest COMPLETED run triggered by a PUSH to the default branch (bot/comment/approval-gated
    # runs are noise: seen live on busy repos).
    runs, _ = gh.get(f"/repos/{repo}/actions/runs", {"branch": branch, "event": "push", "status": "completed",
                                                    "per_page": 1, "exclude_pull_requests": "true"})
    n_prs = None
    if not light:
        pulls, plink = gh.get(f"/repos/{repo}/pulls", {"state": "open", "per_page": 1})
        n_prs = _last_page(plink) or len(pulls or [])  # per_page=1: the "last" page number is the open-PR count

    wr = (runs or {}).get("workflow_runs") or []   # empty when no push run has completed (seen: ollama)
    run = wr[0] if wr else None
    ci = None
    if run:
        ci = {"name": clean(run.get("name"), 60), "status": run.get("status"), "conclusion": run.get("conclusion"),
              "url": run.get("html_url"), "at": run.get("updated_at")}
    release = None
    if rel:
        release = {"tag": clean(rel.get("tag_name"), 60), "name": clean(rel.get("name"), 100),
                   "at": rel.get("published_at"), "notes": first_line(rel.get("body")), "url": rel.get("html_url")}
    snap = {
        "repo": repo, "url": info.get("html_url"), "branch": branch,
        "stars": info.get("stargazers_count", 0),
        "open_prs": n_prs, "open_issues": max(0, info.get("open_issues_count", 0) - (n_prs or 0)) if n_prs is not None
        else info.get("open_issues_count", 0),
        "pushed_at": info.get("pushed_at"),
        "commits_since": len(commits or []) if not _last_page(link) else 100,  # 100 = "100+"
        "commits_more": bool(_last_page(link)),
        "head": (commits[0]["sha"][:7] if commits else prev.get("head")),
        "latest_commit": clean(commits[0]["commit"]["message"].splitlines()[0], 100) if commits else None,
        "release": release, "ci": ci, "since": iso(since), "checked_at": iso(now()),
    }
    first = not prev
    snap["first_check"] = first
    snap["new_release"] = bool(release and not first and release["tag"] != prev.get("release_tag"))
    snap["star_delta"] = 0 if first else snap["stars"] - prev.get("stars", snap["stars"])
    prev_ci = prev.get("ci_conclusion")
    cur_ci = ci["conclusion"] if ci and ci["status"] == "completed" else None
    snap["ci_broke"] = bool(not first and prev_ci == "success" and cur_ci == "failure")
    snap["ci_fixed"] = bool(not first and prev_ci == "failure" and cur_ci == "success")
    return snap


def remember(snap: dict) -> dict:
    ci = snap.get("ci") or {}
    return {"release_tag": (snap.get("release") or {}).get("tag"), "head": snap.get("head"),
            "ci_conclusion": ci.get("conclusion") if ci.get("status") == "completed" else None,
            "stars": snap.get("stars"), "checked_at": snap["checked_at"]}


def run_checks(repos: list[str], since: dt.datetime | None, key: str, save: bool, gh: GitHub | None = None):
    gh = gh or GitHub()
    state = _load("state.json", {})
    out, errors = [], []
    for r in repos:
        prev = (state.get(r) or {}).get(key, {})
        try:
            snap = check(gh, r, prev, since if key == "report" else None, light=key == "alerts")
            out.append(snap)
            state.setdefault(r, {})[key] = remember(snap)
        except GitHubError as e:
            errors.append({"repo": r, "error": str(e)})
            if e.status in (403, 429) and "rate limit" in e.reason:
                errors += [{"repo": x, "error": "skipped: rate limit"} for x in repos[repos.index(r) + 1:]]
                break
        except (KeyError, IndexError, TypeError, ValueError) as e:  # unexpected API shape: report, keep going
            errors.append({"repo": r, "error": f"unexpected GitHub response ({type(e).__name__})"})
    if save:
        _save("state.json", state)
    gh.save_cache()
    return out, errors, gh


# --------------------------------------------------------------------------------------------- output
def _ago(ts: str | None) -> str:
    t = parse_time(ts)
    if not t:
        return "unknown"
    s = int((now() - t).total_seconds())
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 86400 else f"{s // 86400}d ago"


def digest(snaps: list[dict], errors: list[dict]) -> str:
    if not snaps and not errors:
        return "repo-pulse: the watchlist is empty. Add one with: repo_pulse.py add owner/repo"
    lines = []
    for s in snaps:
        head = f"**{s['repo']}**" + (" (first check: baseline set)" if s["first_check"] else "")
        lines.append(head)
        rel = s.get("release")
        if rel:
            mark = "🆕 NEW release" if s["new_release"] else "Latest release"
            lines.append(f"- {mark}: {rel['tag']} ({_ago(rel['at'])})" + (f" — {rel['notes']}" if rel["notes"] else ""))
        more = "+" if s["commits_more"] else ""
        lines.append(f"- {s['commits_since']}{more} commit(s) on {s['branch']} since {s['since'][:16].replace('T', ' ')}Z"
                     + (f"; latest: {s['latest_commit']}" if s.get("latest_commit") and s["commits_since"] else "")
                     + f"; last push {_ago(s['pushed_at'])}")
        ci = s.get("ci")
        if ci:
            state = ci["conclusion"] or ci["status"]
            flag = " ❌ CI turned red" if s["ci_broke"] else " ✅ CI fixed" if s["ci_fixed"] else ""
            lines.append(f"- CI ({ci['name']}): {state}{flag}")
        delta = f" ({s['star_delta']:+d})" if s["star_delta"] else ""
        counts = (f"Open PRs {s['open_prs']}, open issues {s['open_issues']}" if s["open_prs"] is not None
                  else f"Open issues + PRs {s['open_issues']}")
        lines.append(f"- {counts}, stars {s['stars']:,}{delta}")
    for e in errors:
        lines.append(f"**{e['repo']}**: could not check ({e['error']})")
    return "\n".join(lines)


def alerts_of(snaps: list[dict]) -> list[dict]:
    out = []
    for s in snaps:
        if s["new_release"]:
            out.append({"repo": s["repo"], "kind": "new_release", "tag": s["release"]["tag"],
                        "notes": s["release"]["notes"], "url": s["release"]["url"]})
        if s["ci_broke"]:
            out.append({"repo": s["repo"], "kind": "ci_failed", "workflow": s["ci"]["name"], "url": s["ci"]["url"]})
    return out


# --------------------------------------------------------------------------------------------- cli
def add_repos(names: list[str], gh: GitHub | None = None) -> list[dict]:
    """Validate and add; returns [{"input", "repo", "ok", "message"}]. Used by the CLI and by Athena."""
    gh = gh or GitHub()
    repos = watchlist()
    cap = MAX_REPOS_TOKEN if gh.token else MAX_REPOS_ANON
    out = []
    for r in names:
        try:
            full = validate(gh, r)
        except (ValueError, GitHubError) as e:
            out.append({"input": r, "repo": None, "ok": False, "message": f"✗ {e}"})
            continue
        if full.lower() in (x.lower() for x in repos):
            out.append({"input": r, "repo": full, "ok": True, "message": f"= {full} is already watched"})
        elif len(repos) >= cap:
            out.append({"input": r, "repo": full, "ok": False,
                        "message": f"✗ watchlist is full ({cap} repos{'' if gh.token else ' without a GitHub token'})"})
        else:
            repos.append(full)
            out.append({"input": r, "repo": full, "ok": True, "message": f"✓ watching {full}"})
    _save("watchlist.json", {"repos": repos})
    gh.save_cache()
    return out


def remove_repo(name: str) -> bool:
    repos = watchlist()
    keep = [r for r in repos if r.lower() != name.lower().strip()]
    if len(keep) == len(repos):
        return False
    _save("watchlist.json", {"repos": keep})
    return True


def cmd_add(a) -> int:
    res = add_repos(a.repos)
    for r in res:
        print(r["message"])
    return 0 if all(r["ok"] for r in res) else 1


def cmd_remove(a) -> int:
    if not remove_repo(a.repo):
        print(f"{a.repo} was not in the watchlist")
        return 1
    print(f"✓ stopped watching {a.repo}")
    return 0


def cmd_list(a) -> int:
    repos = watchlist()
    if a.json:
        print(json.dumps({"repos": repos}))
    else:
        print("\n".join(f"- {r}" for r in repos) if repos else "The watchlist is empty.")
    return 0


def cmd_report(a) -> int:
    repos = a.repos or watchlist()
    since = parse_since(a.since) if a.since else None
    snaps, errors, gh = run_checks(repos, since, "report", save=not a.no_save)
    if a.json:
        print(json.dumps({"repos": snaps, "errors": errors, "alerts": alerts_of(snaps),
                          "api_calls": gh.calls, "rate_limit_remaining": gh.remaining}, indent=1))
    else:
        print(digest(snaps, errors))
    return 1 if errors else 0


def cmd_alerts(a) -> int:
    snaps, errors, gh = run_checks(watchlist(), None, "alerts", save=True)
    al = alerts_of(snaps)
    if a.json:
        print(json.dumps({"alerts": al, "errors": errors, "checked": len(snaps), "api_calls": gh.calls}))
    else:
        print("\n".join(f"{x['repo']}: {'new release ' + x['tag'] if x['kind'] == 'new_release' else 'CI failed on default branch'}"
                        for x in al) or "No alerts.")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="repo_pulse.py", description="Watch public GitHub repos.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add"); s.add_argument("repos", nargs="+"); s.set_defaults(fn=cmd_add)  # noqa: E702
    s = sub.add_parser("remove"); s.add_argument("repo"); s.set_defaults(fn=cmd_remove)  # noqa: E702
    s = sub.add_parser("list"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_list)  # noqa: E702
    s = sub.add_parser("report")
    s.add_argument("repos", nargs="*")
    s.add_argument("--json", action="store_true")
    s.add_argument("--since")
    s.add_argument("--no-save", action="store_true")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("alerts"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_alerts)  # noqa: E702
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
