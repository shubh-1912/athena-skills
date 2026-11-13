# athena-skills

Hermes Agent skills from the Athena project (CSCI 599, Assignment 3).

| Skill | What it does |
|---|---|
| [repo-pulse](skills/repo-pulse/SKILL.md) | Watch public GitHub repos and report releases, commits, CI, PRs, issues and stars since the last check. |

## Install (Hermes Agent v0.22.7+)

```bash
hermes skills tap add shubh-1912/athena-skills
hermes skills install shubh-1912/athena-skills/skills/repo-pulse
```

Then ask your agent: *"Watch NousResearch/hermes-agent and ollama/ollama, then give me a pulse report."*

repo-pulse uses only the Python standard library and needs no credentials. An optional
`REPO_PULSE_GITHUB_TOKEN` (fine-grained, read-only, public repositories) raises GitHub's API limit from 60 to
5,000 requests per hour. (Hermes keeps `GITHUB_TOKEN` out of terminal commands, so the skill uses its own name.)

## Development

```bash
python3 -m pytest tests/
```

The tests use a fake GitHub API and make no network calls.

## License

MIT
