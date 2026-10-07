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
   prints the compact AI payload.

   ```bash
   timesheet-skill/run.sh prepare DATE
   ```

   If it exits non-zero, stop and report the error. A `Warning:` that a fetch
   failed is not fatal; mention it in the final report.
5. **[AI]** Summarize each block as a `title` and a `description`, following
   [prompts/synthesize_blocks.md](prompts/synthesize_blocks.md). The input is only
   the payload printed by step 4. Do not open the harvester JSON, `blocks.json`,
   `timesheet.jsonl`, git logs, or diffs. If the payload is `[]`, the reply is `{}`.
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

## Overrides

Extra arguments after `DATE` in step 7 go to `save_entry.py`. Use
`--allow-empty` only when a complete run with no activity should clear that
date's previously logged blocks.
