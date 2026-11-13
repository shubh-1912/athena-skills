# repo-pulse JSON output

`report --json`:

```json
{
  "repos": [
    {
      "repo": "owner/repo", "url": "https://github.com/owner/repo", "branch": "main",
      "stars": 1234, "star_delta": 5, "open_prs": 12, "open_issues": 40,
      "pushed_at": "2026-11-12T10:00:00Z",
      "commits_since": 7, "commits_more": false, "head": "3f2a91c", "latest_commit": "fix: …",
      "release": {"tag": "v1.2.0", "name": "v1.2.0", "at": "2026-11-11T09:00:00Z", "notes": "first line", "url": "…"},
      "ci": {"name": "tests", "status": "completed", "conclusion": "success", "url": "…", "at": "…"},
      "first_check": false, "new_release": true, "ci_broke": false, "ci_fixed": false,
      "since": "2026-11-11T10:00:00Z", "checked_at": "2026-11-12T10:00:00Z"
    }
  ],
  "errors": [{"repo": "owner/other", "error": "GitHub API 404: Not Found"}],
  "alerts": [{"repo": "owner/repo", "kind": "new_release", "tag": "v1.2.0", "notes": "…", "url": "…"}],
  "api_calls": 5,
  "rate_limit_remaining": "54"
}
```

Field notes:

- `commits_since` caps at 100; `commits_more: true` means "100 or more".
- `ci` is the latest **completed** workflow run triggered by a **push** to the default branch (`null` if none).
- `release` is the latest published release (`null` if the repo has none); `notes` is its first non-empty line, cleaned.
- `open_prs` is `null` in `alerts` mode (skipped to save a request); `open_issues` then counts issues + PRs.

`alerts --json`: `{"alerts": [...], "errors": [...], "checked": N, "api_calls": N}` where each alert is
`{"repo", "kind": "new_release", "tag", "notes", "url"}` or `{"repo", "kind": "ci_failed", "workflow", "url"}`.
An alert fires when the latest release tag differs from the previous alerts check, or when the default
branch CI goes from `success` to `failure`. The first check of a repo only sets the baseline.
