# Daily Timesheet Workflow

Run from the repository root. `DATE` is the target day (`YYYY-MM-DD`, default
today). Each step is tagged with who does it:

- **[Script]**: a real script produces the result. Run it; never redo its work by hand.
- **[AI]**: a judgment call by the agent. Only step 5 is one.

Steps 1–4 run as one command and steps 6–7 as another (`timesheet-skill/run.sh`),
so a run is two tool calls around the [AI] step. Intermediate files go to
`tmp/timesheet/<DATE>/` (gitignored).

## Steps

1. **[Script]** Harvest commits: `scripts/fetch_commits.py --date DATE` scans the
   workspace repos for non-merge commits by the configured git user.
2. **[Script]** Harvest PRs: `scripts/fetch_prs.py --date DATE` runs `gh search prs`
   for PRs authored or reviewed by `@me`.
3. **[Script]** Harvest calendar events: `scripts/fetch_calendar.py --date DATE`
   reads Google Calendar, dropping declined, free, all-day, and out-of-office events.
   For steps 1–3, `run.sh` replaces a failed harvester's output with `[]` and
   marks that source failed, so the run continues as a partial run.
4. **[Script]** Slice into blocks: `scripts/prepare_payload.py` writes the
   authoritative blocks (times, sources, `failed_sources`) to `blocks.json` and
   prints the compact AI payload. Blocks already logged with unchanged sources
   are left out of the payload and keep their saved text.

   ```bash
   timesheet-skill/run.sh prepare DATE
   ```

   If it exits non-zero, stop and report the error. A `Warning:` that a fetch
   failed is not fatal; mention it in the final report.
5. **[AI]** Summarize each block as a `title` and a `description`, following
   [prompts/synthesize_blocks.md](prompts/synthesize_blocks.md). The input is only
   the payload printed by step 4. Do not open the harvester JSON, `blocks.json`,
   `timesheet.jsonl`, git logs, or diffs. If the payload is `[]`, skip the
   summary: the reply is `{}`.
6. **[Script]** Merge: `scripts/merge_descriptions.py` joins the reply with the
   authoritative blocks, rebuilds each `PRs:` list from the block's sources, and
   fills a fallback for any block the reply skipped.
7. **[Script]** Save and render: `scripts/save_entry.py` upserts into
   `timesheet.jsonl` keyed on `(date, block_id)`, prunes stale blocks only on a
   complete run, re-renders `timesheet.md`, and prints a JSON summary.

   ```bash
   timesheet-skill/run.sh save DATE <<'EOF'
   <step 5 JSON>
   EOF
   ```

   Exit 1 with `Error:` from the merge means the reply was not a JSON object:
   redo step 5 once, then rerun this command.
8. **[Script]** Report: relay the step 7 summary (`inserted`, `updated`,
   `unchanged`, `removed`, and `partial` with its `failed_sources`) and any
   warnings. Do not paste the timesheet back into the conversation.

## Token budget

**Target: at most 1,200 tokens per day** (input plus output of the [AI] step,
summed over every run that day). Step 5 is the only step that needs the model to
read or write data; the [Script] steps cost only their two command lines and
short outputs.

| Part of one run | Ceiling | Measured (2026-10-07) | How it is held |
| --- | --- | --- | --- |
| Instructions (`prompts/synthesize_blocks.md`) | ~300 | 251 | Short prompt, checked by `tests/test_skill_package.py`. |
| Payload (step 4 stdout) | < 500 | 212 (1 block) | `prepare_payload.py` trims subjects and lists until its estimate fits, and warns if it cannot. |
| Reply (step 5 JSON) | ~50 per block | 52 (1 block) | 2–4 word title, 8–20 word description, JSON only. |

A typical first run is about 500 tokens. Measured on 2026-10-07 (Antigravity,
one block, exact counts from the Gemini tokenizer via `track_tokens.py`):
instructions 251 + payload 212 + reply 52 = 515.
Re-runs send only new or changed blocks, so the rest of the day's budget goes to
late work instead of describing the morning again; a re-run with nothing new has
an empty payload and makes no [AI] call. Runner overhead (system prompt, tool
definitions, reading this skill) is outside this budget; the per-day total from
the session transcripts is tracked separately in `telemetry.csv` (US-5.1).
Outside a run, `scripts/track_tokens.py` records each session's `total_tokens`
next to its `ai_step_tokens` (this step alone) and flags any day whose
`ai_step_tokens` exceed 1,200.

Input minimization rules for step 5:

- The only input is the payload printed by step 4: block id, time range, event
  title, first-line commit subjects grouped by repo, and `#N title (status)` PR
  lines.
- Never put the full `git log`, commit bodies, diffs, or file lists in the
  agent's context.
- Never put the raw calendar JSON in the context: no attendees, descriptions,
  links, or event metadata beyond the title.
- Never read `blocks.json`, the harvester JSON files, `timesheet.jsonl`,
  `timesheet.md`, or earlier days' logs; the scripts carry times and sources
  around the [AI] step.
- Do not re-describe blocks that are already logged; `prepare_payload.py`
  leaves them out, and `--refresh` (below) is the only way to send them again.
- Do not paste script output back into the conversation beyond step 8's summary.

## Overrides

Extra arguments after `DATE` in step 7 go to `save_entry.py`. Use
`--allow-empty` only when a complete run with no activity should clear that
date's previously logged blocks.

`timesheet-skill/run.sh prepare DATE --refresh` sends every block to step 5
again, even unchanged ones. Use it only when asked to rewrite a day's
descriptions; it spends a full run's tokens.
