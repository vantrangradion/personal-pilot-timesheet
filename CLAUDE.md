# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal Pilot Timesheet Logger: a daily-run Skill that harvests the user's git commits, GitHub PRs, and Google Calendar events and turns them into timesheet entries (one per time block). The requirements live in two docs; read them before implementing a story:

- `assignment-1-personal-pilot-timesheet.md` is the original assignment brief (grading criteria, Definition of Done, timeline).
- `USER_STORIES.md` is the PRD with locked architecture decisions and Given/When/Then acceptance criteria per story (US-1.1 … US-5.2). Implement against these acceptance criteria exactly (output JSON shapes, file names, CLI flags).

## Commands

Python 3, preferring the standard library. The only third-party dependencies are Google's client libraries for `fetch_calendar.py`, pinned in `requirements.txt` and installed into a gitignored `.venv`. Use `.venv/bin/python` for everything (plain `python3` is MacPorts and lacks them; the calendar tests are skipped there). Run everything from the repo root, because tests import modules as `scripts.<module>`.

```bash
python3 -m venv .venv && .venv/bin/pip install --only-binary=:all: -r requirements.txt   # setup (wheels only: no Rust here)
.venv/bin/python -m unittest discover -s tests            # all tests
.venv/bin/python -m unittest tests.test_fetch_commits     # one file
.venv/bin/python -m unittest tests.test_fetch_commits.TestFetchCommits.test_find_git_repos   # one test
.venv/bin/python scripts/fetch_commits.py --date 2026-10-06 -v   # run harvester (also: --workspace-dir, --repo, --author)
.venv/bin/python scripts/fetch_calendar.py --auth         # one-time Google consent flow -> token.json
.venv/bin/python scripts/fetch_calendar.py --date 2026-10-06 -v  # (also: --credentials, --token, --calendar-id)
.venv/bin/python scripts/prepare_payload.py --date 2026-10-06 --commits c.json --prs p.json --calendar e.json --blocks-out blocks.json -v   # add --failed-source calendar (repeatable) per failed harvester; --timesheet timesheet.jsonl skips unchanged blocks
.venv/bin/python scripts/merge_descriptions.py --blocks blocks.json --descriptions ai.json -v   # AI output via file or stdin
.venv/bin/python scripts/merge_descriptions.py --blocks blocks.json --descriptions ai.json | .venv/bin/python scripts/save_entry.py --blocks blocks.json -v   # upsert into timesheet.jsonl, re-render timesheet.md (also: --timesheet, --markdown, --allow-empty)
.venv/bin/python scripts/render_timesheet.py -v   # rebuild timesheet.md on its own (also: --timesheet, --out, '-' for stdout)
timesheet-skill/run.sh prepare 2026-10-06          # skill steps 1-4: all harvesters + prepare_payload, prints the AI payload (files in tmp/timesheet/<date>/); --refresh re-sends logged blocks
timesheet-skill/run.sh save 2026-10-06 < ai.json   # skill steps 6-7: merge + save_entry (extra args go to save_entry.py)
```

## Architecture

The core design rule, which is what the assignment grades: **deterministic work is a `[Script]`; only real judgment goes to `[AI]`.** Never move parsing, filtering, time math, JSON reshaping, or file writes into the AI step. The AI step must receive a minimal payload (under 500 tokens of input, with a daily budget of 1,200 tokens) and must never receive raw git logs, diffs, or full calendar JSON.

Planned pipeline (each script is a standalone CLI that writes JSON to stdout, exits 0 with `[]` when there is no data, and exits non-zero with a stderr diagnostic on auth or config failures):

1. `scripts/fetch_commits.py` (US-1.1): scans `WORKSPACE_DIR` (default `/Users/van.tran/Documents/dev`, up to depth 2) for git repos and collects commits by the configured git user for the target date. Uses `--no-merges`, normalizes timestamps to the local timezone, and queries a ±1 day window and then filters by local date.
2. `scripts/fetch_prs.py` (US-1.2): `gh search prs`, authored + reviewed-by `@me`.
3. `scripts/fetch_calendar.py` (US-1.3): Google Calendar API v3 via the official client libraries (`credentials.json` / `token.json`, both gitignored). google-auth always overrides `token_uri` with Google's real endpoint, so tests patch `google.oauth2.credentials._GOOGLE_OAUTH2_TOKEN_ENDPOINT` to the fake server.
4. `scripts/prepare_payload.py` (US-2.1): buckets everything into time blocks. Calendar events become blocks, and commits outside events become focus blocks. Focus blocks split on a ≥90-minute inactivity gap, at the 12:00 and 13:00 lunch boundaries, and around events. Each block gets a stable `block_id`: the calendar `event_id`, or `<date>_focus_HHMM` anchored to the earliest commit's author time (`<date>_focus_<repo>-pr<N>` for PR-only blocks). Never use positional numbering: it shifts when activity lands between blocks and breaks the upsert.
5. **[AI]** (US-2.2): prompted by `timesheet-skill/prompts/synthesize_blocks.md` (kept short: it counts against the daily budget), returns only `{block_id: {"title": "<2-4 word topic>", "description": "<topics>. PRs: #a, #b"}}`. The title is a display label for the `timesheet.md` heading; `block_id` stays the upsert key. `scripts/merge_descriptions.py` merges this into the authoritative date, times, and sources: it drops unknown block ids, rebuilds the `PRs:` suffix from `sources.prs` (removing PR refs outside the block), falls back to a deterministic title and description for skipped blocks, and exits 1 on non-JSON output.
6. `scripts/save_entry.py` (US-3.1/3.2): idempotent upsert into `timesheet.jsonl` keyed on `(date, block_id)`, using an atomic temp-file rename. Then it re-renders `timesheet.md` via `scripts/render_timesheet.py` (US-3.2): dates oldest first, blocks by start time, output independent of JSONL row order, and the file is rewritten only when its content changes, so Git diffs stay limited to edited blocks. Re-runs must never duplicate blocks.
7. `scripts/track_tokens.py` (US-5.1): parses Claude Code (`~/.claude/projects/...`) and Antigravity transcripts into `telemetry.csv`.

The deliverable is packaged as `timesheet-skill/` (`SKILL.md` + `workflow.md`, where every step is tagged `[Script]` or `[AI]`) and must work with both Claude Code and Antigravity. `IMPROVEMENT.md` is the daily log → review → improve journal.

## Conventions

- Commit messages are conventional style with the story ID, e.g. `feat(ingest): ... (US-1.1)`. Commit messages are a single subject line only, with no body (no bullet lists, explanatory paragraphs, or `Co-Authored-By` trailers).
- Scripts use type annotations and docstrings, `argparse` with a `main(argv) -> int` entry point (so tests can call `main` directly), and verbose diagnostics go to stderr only.
- Tests use `unittest` and build real temporary git repos (not mocks) where git behavior matters.
- End-to-end `[Script]` steps should finish in under 5 seconds.
