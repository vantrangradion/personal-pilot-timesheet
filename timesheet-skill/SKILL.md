---
name: timesheet-skill
description: Personal Pilot Timesheet Logger. Harvests today's git commits, GitHub PRs, and Google Calendar events and logs one timesheet entry per time block into timesheet.jsonl and timesheet.md, then pushes them to the Gradion Workspace Timesheet app. Use once per day, or when asked to "log today's work", "update my timesheet", "fill in the timesheet", or "log work for <date>". Safe to re-run; re-runs update entries instead of duplicating them.
compatibility: Requires git, gh (authenticated with `gh auth login`), and python3 with the repo's .venv (Google client libraries). Needs network access to GitHub and the Google Calendar API.
---

# Timesheet Logger

Turns a day of verifiable activity (local git commits under
`/Users/van.tran/Documents/dev`, GitHub PRs authored or reviewed by `@me`, and
Google Calendar events) into timesheet entries, one per time block. Scripts do
all fetching, time math, merging, and file writes; the agent only writes a
short title and description per block.

## When to use

- Once per day, scheduled or on request ("log today's work", "update my timesheet").
- For a past day when given a date ("log work for 2026-10-06").
- Again on the same day after more work lands: re-runs upsert by `(date, block_id)`.

## How to run

Follow [workflow.md](workflow.md) step by step. Every step is tagged `[Script]`
(run the command, do not reproduce its output) or `[AI]` (the only judgment
step). The whole run is two shell commands around one AI reply:

```bash
timesheet-skill/run.sh prepare [YYYY-MM-DD]          # prints the compact AI payload
timesheet-skill/run.sh save [YYYY-MM-DD] <<'EOF'     # merges, saves, renders
{"<block id>": {"title": "...", "description": "..."}}
EOF
timesheet-skill/run.sh push [YYYY-MM-DD]             # pushes the entries to the Gradion Timesheet app
```

Run from the tool repository root, `/Users/van.tran/Documents/dev/personal-pilot-timesheet`.
Output: `timesheet.jsonl` (source of truth), `timesheet.md` (review view), and
entries in the Gradion Workspace Timesheet app (ids kept in `timesheet-remote.jsonl`).

## Prerequisites

| Tool | Check | Setup |
| --- | --- | --- |
| `git` | `git config user.email` | Set the git user that authors your commits. |
| `gh` | `gh auth status` | `gh auth login` |
| `python3` | `.venv/bin/python --version` | `python3 -m venv .venv && .venv/bin/pip install --only-binary=:all: -r requirements.txt` |
| Google Calendar | `token.json` exists | Put the OAuth client in `credentials.json`, then `.venv/bin/python scripts/fetch_calendar.py --auth`. |
| Gradion Workspace | `security find-generic-password -s gradion-pat` | Create a personal API token (Read + Write, app access: Timesheet only), then `security add-generic-password -a "$USER" -s gradion-pat -w`. |

A missing prerequisite does not abort the run: that source is recorded as
failed, the other sources are still logged, and nothing is pruned.

## Discovery locations

The skill folder is `timesheet-skill/` in this repository. Each runner finds it
through a symlink named after the skill:

| Runner | Workspace (this repo) | User-wide |
| --- | --- | --- |
| Claude Code | `.claude/skills/timesheet-skill` (committed) | `~/.claude/skills/timesheet-skill` |
| Antigravity | `.agents/skills/timesheet-skill` (committed; `.agent/` also works) | `~/.gemini/config/skills/timesheet-skill` |

To install user-wide, so the skill is available from any directory:

```bash
ln -s /Users/van.tran/Documents/dev/personal-pilot-timesheet/timesheet-skill ~/.claude/skills/timesheet-skill
ln -s /Users/van.tran/Documents/dev/personal-pilot-timesheet/timesheet-skill ~/.gemini/config/skills/timesheet-skill
```

Claude Code also exposes it as the `/timesheet-skill` command. Both runners read
the same `name` and `description` frontmatter above.
