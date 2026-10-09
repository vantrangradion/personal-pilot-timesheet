# Product Requirements Document & User Stories
## Personal Pilot Timesheet Logger

* **Author:** Senior Product Manager
* **Target User:** Intern / Pilot Engineer
* **Stakeholder:** Track Leader (Tin) & Internship Supervisors
* **Execution Runner:** Antigravity / Claude Code (Dual Runner Compatibility)
* **Version:** 1.0.0
* **Date:** 2026-10-06
* **Status:** Approved / Ready for Sprint 1 Kickoff
* **Milestones:**
  - **Kickoff:** Tuesday, 2026-10-06 (Start logging daily from today)
  - **Sprint 1 Live Demo:** Friday, 2026-10-09 (Working end-to-end tool demo)
  - **Final Evaluation:** Friday, 2026-10-30 (Continuous improvement review & token telemetry presentation)

---

## 1. Executive Summary & Product Vision

The **Personal Pilot Timesheet Logger** automates daily engineering work logs by aggregating verifiable data from three operational sources:
1. Local Git commits authored in the workspace.
2. GitHub Pull Requests (opened, reviewed, merged).
3. Google Calendar events (meetings, focus blocks).

Rather than manual timesheet entry or unstructured AI summarization, the system enforces a strict **deterministic script vs. AI judgment boundary**:
* **Scripts (`[Script]`):** Handle data retrieval, filtering, time-block mapping, persistence, and telemetry.
* **AI (`[AI]`):** Confined exclusively to judgment tasks: grouping micro-tasks into coherent topics and writing executive-ready descriptions.
* **Delivery Schedule:** Sprint 1 live demo delivered on Friday, 2026-10-09; final evaluation review with token telemetry optimization presentation delivered on Friday, 2026-10-30.

---

## 2. Locked Technical & Architectural Decisions

Following trade-off evaluation, the product architecture is finalized as:

1. **Workspace & Multi-Repository Scope:**
   - **Workspace Root:** `/Users/van.tran/Documents/dev` — the tool scans all git repositories located in this directory (e.g. `personal-pilot-timesheet`, `devops`, and future assignments/projects) to capture commits across all active projects.
   - **Tool Repository:** `/Users/van.tran/Documents/dev/personal-pilot-timesheet`.
   - **PR Platform:** **GitHub** using `gh` CLI across all detected GitHub remote repositories.
2. **Google Calendar Integration:**
   - **Standalone Python Script (`scripts/fetch_calendar.py`)** utilizing Google Calendar API v3 with OAuth 2.0 desktop authentication (`credentials.json` and cached `token.json`).
   - Ensures zero unnecessary token consumption by pre-filtering events before the AI runner receives context.
3. **Timesheet Storage Architecture (Dual Model):**
   - **Source of Truth (Data Layer):** `timesheet.jsonl` — machine-readable, schema-validated, and idempotent.
   - **Presentation Layer (View Layer):** `timesheet.md` — rendered human-readable Markdown matching the timesheet "Edit entry" UI specification for Friday supervisor reviews.
4. **Packaging:**
   - Standalone Skill directory: `timesheet-skill/` containing `SKILL.md`, `workflow.md`, and supporting deterministic scripts under `scripts/`. Compatible with both Antigravity skill structure and Claude Code skill invocation.
5. **Token Telemetry:**
   - Deterministic parser `scripts/track_tokens.py` extracting token consumption from local session transcripts into `telemetry.csv`, supporting both Antigravity (`~/.gemini/antigravity-cli/brain/.../transcript.jsonl`) and Claude Code (`~/.claude/projects/.../*.jsonl`).

---

## 3. High-Level Epic Map

```
EPIC 1: Deterministic Data Ingestion (Scripts)
  ├── US-1.1: Local Git Commit Harvester [P0, Script]
  ├── US-1.2: GitHub Pull Request Harvester [P0, Script]
  └── US-1.3: Google Calendar OAuth Harvester [P0, Script]

EPIC 2: Time-Block Segmentation & AI Topic Synthesis
  ├── US-2.1: Chronological Time-Block Slicer & Pre-Filter [P0, Script]
  └── US-2.2: AI Topic Clustering & Summary Synthesizer [P0, AI]

EPIC 3: Dual Persistence & Idempotency
  ├── US-3.1: JSONL State Manager & Idempotent Upsert [P0, Script]
  ├── US-3.2: Timesheet Markdown View Renderer [P0, Script]
  └── US-3.3: Gradion Workspace Timesheet Push [P1, Script]

EPIC 4: Skill Packaging & Daily Workflow Guardrails
  ├── US-4.1: Dual Runner Skill & Workflow Packaging [P0, Spec]
  └── US-4.2: Daily Token Budget & Input Minimization Guardrail [P0, Spec]

EPIC 5: Telemetry & Continuous Improvement
  ├── US-5.1: Dual Runner Session Transcript Token Parser [P0, Script]
  └── US-5.2: Continuous Improvement & Experimentation Journal [P1, Process]
```

---

## 4. Detailed User Stories

---

### Epic 1: Deterministic Data Ingestion (`[Script]`)

#### US-1.1: Multi-Repository Git Commit Harvester
* **Story ID:** `US-1.1`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** a deterministic script `scripts/fetch_commits.py` to scan the workspace directory (`/Users/van.tran/Documents/dev`) and extract all git commits authored by me today across all repositories,  
  > **So that** my engineering outputs across all active assignments and projects are collected without manual copy-pasting.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Extract commits across child repositories for the target date**
    - **Given** a workspace root directory `/Users/van.tran/Documents/dev` containing one or more Git repositories
    - **And** commits have been authored on the target date matching the configured Git user (`git config user.email` or `user.name`)
    - **When** `scripts/fetch_commits.py` is executed with `--workspace-dir /Users/van.tran/Documents/dev` (or default environment)
    - **Then** the script exits with status code `0`
    - **And** outputs a valid JSON array to stdout where each object contains `repo`, `hash`, `timestamp`, `message`, and `files_changed_count`:
      ```json
      [
        {
          "repo": "personal-pilot-timesheet",
          "hash": "8f3b21a",
          "timestamp": "2026-10-06T11:24:10+07:00",
          "message": "feat(ingest): add git log commit parser",
          "files_changed_count": 2
        }
      ]
      ```
    - **And** merge commits (`--no-merges`) are excluded.
  - **Scenario 2: No commits authored on the target date**
    - **Given** no commits were authored by the user on the target date across scanned repositories
    - **When** `scripts/fetch_commits.py` is executed
    - **Then** the script exits with status code `0`
    - **And** outputs an empty JSON array `[]` to stdout.

---

#### US-1.2: GitHub Pull Request Harvester (GitHub CLI)
* **Story ID:** `US-1.2`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** a script `scripts/fetch_prs.py` using the GitHub CLI (`gh search prs`) to retrieve PRs I opened, reviewed, or had merged today across my active repositories in `/Users/van.tran/Documents/dev`,  
  > **So that** my collaboration and code review efforts across all projects are captured in my timesheet without manual tracking.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Retrieve PRs opened, reviewed, or merged today via gh CLI**
    - **Given** active repositories in `/Users/van.tran/Documents/dev` with GitHub remotes
    - **And** the user is authenticated with GitHub CLI (`gh auth status`)
    - **When** `scripts/fetch_prs.py` executes `gh search prs` for the target date
    - **Then** it queries both authored PRs (`--author="@me"`) and reviewed PRs (`--reviewed-by="@me"`) updated on the target date
    - **And** applies field projection via `--json repository,number,title,state,url,updatedAt` to minimize payload size
    - **And** outputs a normalized, compact JSON array to stdout with schema:
      ```json
      [
        {
          "repo": "personal-pilot-timesheet",
          "number": 14,
          "title": "feat: add Google Calendar OAuth client",
          "status": "opened",
          "url": "https://github.com/org/repo/pull/14",
          "updated_at": "2026-10-06T14:10:00+07:00"
        }
      ]
      ```
  - **Scenario 2: Handle unauthenticated GitHub CLI state**
    - **Given** the GitHub CLI is unauthenticated (`gh auth status` returns error)
    - **When** `scripts/fetch_prs.py` is executed
    - **Then** the script exits with a non-zero exit code
    - **And** prints a diagnostic error message to stderr explaining how to authenticate (`gh auth login`).

---

#### US-1.3: Google Calendar OAuth Harvester
* **Story ID:** `US-1.3`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** a script `scripts/fetch_calendar.py` that connects to the Google Calendar API via OAuth 2.0 (`credentials.json`),  
  > **So that** my daily meetings and calendar blocks are automatically retrieved.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Authenticate and retrieve daily calendar events**
    - **Given** valid OAuth client configuration in `credentials.json` and cached user credentials in `token.json`
    - **When** `scripts/fetch_calendar.py` is executed for the target date
    - **Then** the script queries the Google Calendar API for events between `00:00:00` and `23:59:59`
    - **And** filters out declined events (`responseStatus == 'declined'`) and transparent/all-day out-of-office blocks
    - **And** if the access token in `token.json` is expired, the script automatically refreshes it via the OAuth refresh token and persists the updated credentials without user intervention
    - **And** outputs a chronologically sorted JSON array to stdout:
      ```json
      [
        {
          "id": "event_123",
          "title": "Daily Standup",
          "start": "2026-10-06T09:30:00+07:00",
          "end": "2026-10-06T10:00:00+07:00"
        }
      ]
      ```
  - **Scenario 2: Handle missing or unauthenticated Google Calendar credentials**
    - **Given** `credentials.json` is missing or OAuth authentication fails
    - **When** `scripts/fetch_calendar.py` is executed
    - **Then** the script exits with a non-zero exit code
    - **And** prints a diagnostic error message to stderr indicating that OAuth credentials must be set up.

---

### Epic 2: Time-Block Segmentation & AI Topic Synthesis

#### US-2.1: Chronological Time-Block Slicer & Pre-Filter
* **Story ID:** `US-2.1`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As a** Timesheet Engine,  
  > **I want** `scripts/prepare_payload.py` to bucket calendar events, commits, and PRs into cohesive time blocks,  
  > **So that** the AI receives a pre-organized, minimal token payload.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Segment daily events into chronological calendar and focus blocks**
    - **Given** daily calendar events, Git commits, and GitHub PR records from the harvester scripts
    - **When** `scripts/prepare_payload.py` is executed
    - **Then** it normalizes all timestamps (Git commit ISO strings, PR update dates, and Google Calendar event datetimes) to the local workspace timezone
    - **And** creates chronological time blocks matching calendar events
    - **And** maps commits and PRs into the time block containing their timestamp
    - **And** generates distinct focus blocks for commit activity occurring outside calendar events.
  - **Scenario 2: Segment activity into multiple work blocks when zero calendar events exist**
    - **Given** zero calendar events exist for the target date but commits or PRs are recorded
    - **When** `scripts/prepare_payload.py` processes the inputs
    - **Then** it normalizes all timestamps to the local workspace timezone and clusters commits and PRs into multiple distinct work blocks using an inactivity gap threshold of >= 90 minutes (with default boundary splits at the 12:00–13:00 lunch break)
    - **And** avoids collapsing the entire day into a single giant entry
    - **And** assigns a stable `block_id` to each block, anchored to the author time of the block's earliest commit (e.g. `<date>_focus_0947`), or to the PR for PR-only blocks (e.g. `<date>_focus_<repo>-pr14`), so re-runs, amends, rebases, and activity added between blocks never renumber existing blocks
    - **And** maps commits and PRs into their corresponding time block.
  - **Scenario 3: Enforce compact input token budget for AI processing**
    - **Given** the structured time-block payload
    - **When** prepared for AI consumption (Antigravity or Claude Code)
    - **Then** raw diffs, verbose commit descriptions, and attendee lists are stripped
    - **And** the total input payload size remains under 500 tokens.

---

#### US-2.2: AI Topic Clustering & Summary Synthesizer
* **Story ID:** `US-2.2`
* **Type:** `[AI]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As a** Supervisor reviewing daily timesheets,  
  > **I want** the AI agent (Antigravity or Claude Code) to group related micro-tasks into high-level business topics and append inline PR lists,  
  > **So that** timesheets are concise, executive-friendly, and avoid raw commit spam.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Cluster related commits into concise business topics**
    - **Given** the pre-filtered time-block JSON payload from `scripts/prepare_payload.py`
    - **When** the AI agent processes the block during the `[AI]` workflow step
    - **Then** it groups related micro-commits into a cohesive business topic rather than listing each commit separately
    - **And** appends an inline PR reference list at the end of the description: `PRs: #1182, #1174`
    - **And** does not invent or hallucinate tasks not present in the payload.
  - **Scenario 2: Return synthesized block descriptions by block ID**
    - **Given** pre-segmented blocks each assigned a deterministic `block_id`
    - **When** the AI completes topic synthesis
    - **Then** it returns a clean JSON mapping of `block_id` to `{ "title": "<2-4 word topic>", "description": "<summary>. PRs: #<ids>" }`
    - **And** the `title` is a human-readable display label for the block heading only; `block_id` remains the stable upsert key and is never derived from the title
    - **And** deterministic scripts merge these descriptions into authoritative metadata (`date`, `start_time`, `end_time`, `sources`) without relying on the AI to reproduce timestamps or audit records.

---

### Epic 3: Dual Persistence & Idempotency (`[Script]`)

#### US-3.1: JSONL State Manager & Idempotent Upsert
* **Story ID:** `US-3.1`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** `scripts/save_entry.py` to persist daily entries into `timesheet.jsonl` with upsert capability,  
  > **So that** re-running the tool on the same day never creates duplicate timesheet blocks.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Upsert existing time block without duplicating rows**
    - **Given** an entry identified by `date` and stable `block_id` (Google Calendar `event_id` for meetings, or deterministic `block_id` for focus sessions)
    - **And** a record with this identical `date` and `block_id` already exists in `timesheet.jsonl`
    - **When** `scripts/save_entry.py` is executed with updated data (even if timestamps have adjusted slightly due to subsequent commits)
    - **Then** the existing record in `timesheet.jsonl` is updated in-place without creating duplicate rows
    - **And** atomic file writing (via temporary file rename) guarantees data integrity during writes.
  - **Scenario 2: Append new distinct time block**
    - **Given** an entry with a new `block_id` not present in `timesheet.jsonl` for that date
    - **When** `scripts/save_entry.py` is executed
    - **Then** the record is appended as a new line in `timesheet.jsonl` preserving all source audit trails, with `kind` (`calendar` or `focus`) marking which blocks a later run may prune:
      ```json
      {
        "block_id": "event_123",
        "kind": "calendar",
        "date": "2026-10-06",
        "start_time": "14:00",
        "end_time": "16:30",
        "title": "Calendar & Telemetry",
        "description": "Implemented Google Calendar OAuth client and telemetry parser. PRs: #3",
        "sources": {
          "calendar_event": "Focus: Development",
          "commits": ["8f3b21a", "b2c3d4e"],
          "prs": [3],
          "repos": ["personal-pilot-timesheet"]
        },
        "created_at": "2026-10-06T16:35:00+07:00"
      }
      ```
  - **Scenario 3: Prune focus blocks absorbed by a merge on re-run**
    - **Given** `timesheet.jsonl` holds focus blocks `<date>_focus_0900` and `<date>_focus_1100` from an earlier run
    - **And** a later run includes new activity that fills the gap between them, so `scripts/prepare_payload.py` merges them into a single block `<date>_focus_0900`
    - **When** `scripts/save_entry.py` persists the later run's blocks for that date
    - **Then** `<date>_focus_0900` is updated in place
    - **And** focus blocks for that date that the current run no longer produces (here `<date>_focus_1100`) are removed, so no stale rows overlap the merged block
    - **And** rows for other dates are left untouched.
  - **Scenario 4: Prune calendar blocks for meetings cancelled or declined after logging**
    - **Given** `timesheet.jsonl` holds a calendar block `event_123` for a date
    - **And** the meeting is later cancelled or declined, so `scripts/fetch_calendar.py` no longer returns it
    - **When** a re-run in which `scripts/fetch_calendar.py` succeeded (exit code `0`) persists that date's blocks
    - **Then** calendar blocks for that date that the current run no longer produces (here `event_123`) are removed
    - **And** any commits and PRs that fell inside the removed meeting are carried by the focus blocks the re-run produces in its place.
  - **Scenario 5: Never prune from a partial run**
    - **Given** a re-run in which any harvester (`fetch_commits.py`, `fetch_prs.py`, or `fetch_calendar.py`) failed (non-zero exit, e.g. expired OAuth, unauthenticated `gh`, or network timeout)
    - **And** the workflow passed each failed source to `scripts/prepare_payload.py` (`--failed-source commits|prs|calendar`, repeatable), which records it in the `--blocks-out` file as `{"date": "<date>", "failed_sources": ["calendar"], "blocks": [...]}` (`failed_sources` is `[]` on a complete run)
    - **When** `scripts/save_entry.py` persists that run's blocks, reading `failed_sources` from the blocks file
    - **Then** no blocks are removed for that date (an empty result from a failed fetch must not be read as "all meetings cancelled" or "no work done")
    - **And** the run is reported as partial on stderr.
  - **Scenario 6: Never wipe a date from an empty run**
    - **Given** `timesheet.jsonl` holds blocks for a date
    - **And** a complete re-run (`failed_sources` is `[]`) produces zero blocks for that date (e.g. a misconfigured `--workspace-dir` or `--author` that returns `[]` without failing)
    - **When** `scripts/save_entry.py` persists that run
    - **Then** no blocks are removed for that date unless `--allow-empty` is passed
    - **And** a warning on stderr names the rows kept and the `--allow-empty` override.

---

#### US-3.2: Timesheet Markdown View Renderer
* **Story ID:** `US-3.2`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As a** Track Leader,  
  > **I want** the script to automatically render `timesheet.md` from `timesheet.jsonl`,  
  > **So that** I can review clean, human-readable timesheet entries during Friday reviews.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Automatically render human-readable Markdown from JSONL state**
    - **Given** one or more records persisted in `timesheet.jsonl`
    - **When** `scripts/save_entry.py` finishes persisting entries (or when render script is invoked)
    - **Then** `timesheet.md` is regenerated matching the Section 9 "Edit entry" UI specification
    - **And** entries are grouped chronologically by date under Markdown headers
    - **And** each block heading shows the block's time range, a `[<project>]` tag naming the repositories its commits and PRs came from (`sources.repos`, comma-separated; omitted when the block has none, e.g. a meeting with no commits), and its `title` (`### <start> - <end> | [<project>] <title>`)
    - **And** each block clearly presents `Date`, `Start time`, `End time`, `Description` (including inline PRs), and `Sources`:
      ```markdown
      # Timesheet Log

      ## 2026-10-06 (Tuesday)

      ### 09:30 - 10:00 | Standup & Planning
      - **Date:** 2026-10-06
      - **Start time:** 09:30
      - **End time:** 10:00
      - **Description:** Weekly sprint kickoff and timesheet architecture review.
      - **Sources:** Calendar: "Team Standup"

      ### 10:00 - 12:30 | [personal-pilot-timesheet] Core Engineering
      - **Date:** 2026-10-06
      - **Start time:** 10:00
      - **End time:** 12:30
      - **Description:** Git commit harvester and CLI test suite implementation. PRs: #1, #2
      - **Sources:** Commits: `8f3b21a`, `4d5e6f7`; PRs: #1, #2
      ```
  - **Scenario 2: Maintain clean Git diffs**
    - **Given** modified or appended entries in `timesheet.jsonl`
    - **When** `timesheet.md` is re-rendered
    - **Then** unchanged past dates and blocks remain identical, producing clean, verifiable diffs in Git.

---

#### US-3.3: Gradion Workspace Timesheet Push
* **Story ID:** `US-3.3`
* **Type:** `[Script]`
* **Priority:** P1 (Should-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** `scripts/push_entries.py` to push each logged block from `timesheet.jsonl` to the Gradion Workspace Timesheet app (`https://workspace.gradion.com/app/timesheet`),  
  > **So that** my official timesheet is filled from the same verified entries without re-typing them.

* **Integration:** the app's MCP tools, called through the workspace app-tool proxy `POST /api/me/apps/timesheet/tools/<tool>/call` with body `{"arguments": {...}}`, authenticated by a personal API token (`GRADION_PAT`, else the macOS Keychain item `gradion-pat`) scoped to the Read and Write actions and the Timesheet app only. Tools used: `list_my_entries`, `log_time`, `edit_time`. The app has no delete tool.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Create entries for new blocks**
    - **Given** rows for the target date in `timesheet.jsonl` that were never pushed
    - **When** `scripts/push_entries.py --date <date>` is executed
    - **Then** each row is created with `log_time` using its `date`, `start_time` → `startTime`, `end_time` → `endTime`, `description`, the block's `timesheet.md` heading label `[<project>] <title>` → `task` (free text, with the `[<project>]` tag from `sources.repos` left out when the block has no repos, as in US-3.2; cut to the app's 150-character limit; omitted when both are empty), and the classification from `--classification` (default `Gradion Intern Academy 2026`)
    - **And** the returned entry id is recorded in `timesheet-remote.jsonl` (gitignored) keyed on `(date, block_id)` with the fields sent, written atomically after every successful call.
  - **Scenario 2: Re-runs never duplicate entries**
    - **Given** a block already pushed
    - **When** the script runs again
    - **Then** an unchanged block makes no write call
    - **And** a block whose time range, description, or task changed (including a block pushed before `task` was sent) is updated in place with `edit_time` (only the changed fields, plus an audit `reason`; a task that became empty is cleared with `task: null`).
  - **Scenario 3: Never overwrite entries the tool did not create**
    - **Given** an entry on the timesheet that is not in `timesheet-remote.jsonl` (e.g. logged by hand)
    - **When** a local block has the same time range, or overlaps it
    - **Then** the block is skipped (same range) or reported as failed with the app's overlap error, and other blocks are still pushed.
  - **Scenario 4: Report what cannot be fixed automatically**
    - **Given** a pushed entry whose block no longer exists locally (merged or pruned), or that was removed from the timesheet by hand
    - **When** the script runs
    - **Then** it is reported on stderr as `orphaned` or `missing` and left untouched.
  - **Scenario 5: Never push a partial run**
    - **Given** the run's blocks file (`--blocks`) lists `failed_sources`
    - **When** the script runs
    - **Then** no request is sent, and the run is reported as partial.
  - **Scenario 6: Dry run and failures**
    - **When** `--dry-run` is passed, **Then** only `list_my_entries` is called and the plan is printed.
    - **When** the token is missing or rejected, **Then** the script exits non-zero with a diagnostic explaining how to create and store a new token.
    - **And** stdout is a JSON summary: `created`, `updated`, `unchanged`, `skipped`, `missing`, `orphaned`, `failed`; the exit code is 1 when any block failed.

---

### Epic 4: Skill Packaging & Daily Workflow Guardrails

#### US-4.1: Dual Runner Skill & Workflow Packaging (Antigravity & Claude Code)
* **Story ID:** `US-4.1`
* **Type:** `[Spec]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern using Antigravity or Claude Code,  
  > **I want** a packaged `timesheet-skill/SKILL.md` and `timesheet-skill/workflow.md`,  
  > **So that** either agent runner can execute the timesheet generation autonomously upon request.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Configure portable runner skill metadata**
    - **Given** the `timesheet-skill/` directory exists
    - **When** Antigravity or Claude Code indexes `timesheet-skill/SKILL.md`
    - **Then** the skill name, invocation triggers, description, and required CLI prerequisites (`git`, `gh`, `python3`) are registered in a format compatible with both runners
    - **And** runner-specific discovery locations are documented.
  - **Scenario 2: Enforce deterministic script vs. AI judgment tags**
    - **Given** the step-by-step procedure in `timesheet-skill/workflow.md`
    - **When** reviewing workflow steps
    - **Then** every single step is explicitly prefixed with either `[Script]` or `[AI]`
    - **And** zero `[AI]` tags are assigned to deterministic actions such as API fetching, parsing, JSON manipulation, file writes, or math.

---

#### US-4.2: Daily Token Budget Guardrail
* **Story ID:** `US-4.2`
* **Type:** `[Spec]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As a** Track Leader,  
  > **I want** `workflow.md` to declare an explicit daily token budget target and enforce input minimization across runner sessions,  
  > **So that** the intern demonstrates first-principles cost efficiency in LLM operations.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Constrain daily prompt and completion tokens**
    - **Given** daily timesheet workflow execution in Antigravity or Claude Code
    - **When** measuring input and output tokens consumed across all runs for the day
    - **Then** total daily consumption does not exceed the target ceiling of 1,200 tokens
    - **And** `workflow.md` documents input minimization rules prohibiting full git logs, diffs, or raw calendar JSON dumps from entering the agent's context.

---

### Epic 5: Telemetry & Continuous Improvement

#### US-5.1: Dual Runner Session Transcript Token Parser
* **Story ID:** `US-5.1`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** a script `scripts/track_tokens.py` to extract exact token usage from local session transcript files generated by Antigravity or Claude Code,  
  > **So that** I have auditable empirical data for my Week 4 optimization presentation regardless of the active runner.

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Parse token telemetry from Claude Code session transcripts**
    - **Given** completed Claude Code sessions in `~/.claude/`
    - **When** `scripts/track_tokens.py` is executed with Claude session paths or auto-detected Claude environment
    - **Then** it extracts `input_tokens`, `output_tokens`, `cache_read_input_tokens`, and `cache_creation_input_tokens`
    - **And** appends a row to `telemetry.csv` containing `date`, `runner`, `session_id`, `input_tokens`, `output_tokens`, `cache_tokens`, `total_tokens`, and `timestamp`.
  - **Scenario 2: Parse token telemetry from Antigravity session transcripts**
    - **Given** completed Antigravity sessions in `~/.gemini/antigravity-cli/brain/`
    - **When** `scripts/track_tokens.py` is executed with Antigravity transcript paths or auto-detected Antigravity environment
    - **Then** it extracts the prompt and candidate token metrics from the transcript steps
    - **And** appends a row to `telemetry.csv` containing `date`, `runner`, `session_id`, `input_tokens`, `output_tokens`, `cache_tokens`, `total_tokens`, and `timestamp`.
  - **Scenario 3: Aggregate multiple runs on the same date**
    - **Given** multiple timesheet runs recorded on the same date across one or both runners
    - **When** daily summary telemetry is generated
    - **Then** `scripts/track_tokens.py` sums all session token metrics into an aggregated daily total.
  - **Scenario 4: Report the [AI] step separately from runner overhead**
    - **Given** a session transcript containing one or more timesheet runs (`timesheet-skill/run.sh prepare` and `run.sh save` tool calls)
    - **When** `scripts/track_tokens.py` records that session
    - **Then** the row also contains `ai_step_tokens`: the [AI] step's input and output only (the synthesis prompt, the payload printed by `run.sh prepare`, and the JSON reply passed to `run.sh save`), excluding runner overhead such as the system prompt, tool definitions, and skill files
    - **And** the aggregated daily total reports `ai_step_tokens` next to `total_tokens` and flags any date where `ai_step_tokens` exceeds the 1,200-token ceiling declared in `workflow.md` (US-4.2).

---

#### US-5.2: Continuous Improvement & Experimentation Journal
* **Story ID:** `US-5.2`
* **Type:** `[Process]`
* **Priority:** P1 (Should-Have)
* **User Story:**
  > **As a** Track Leader,  
  > **I want** an `IMPROVEMENT.md` journal maintained daily by the intern,  
  > **So that** the intern proves the continuous improvement loop (*log $\to$ review what broke $\to$ improve $\to$ log again*).

* **Acceptance Criteria (Given / When / Then):**
  - **Scenario 1: Log daily reviews and optimizations**
    - **Given** a daily timesheet run has completed
    - **When** an edge case, failure, or prompt inefficiency is observed
    - **Then** a record is added to `IMPROVEMENT.md` documenting Date, Observed Issue, Root Cause, Action Taken, and Token Delta
    - **And** the baseline kickoff entry for Week 1 Day 1 (Tue 6 Oct) is initialized.

---

## 5. Non-Functional Requirements (NFRs)

1. **Deterministic Isolation:**
   - Any script failure (e.g., GitHub CLI offline, Calendar network timeout) must emit human-readable diagnostic messages without leaving zombie processes or corrupted files.
2. **Data Privacy & Security:**
   - Secrets (`credentials.json`, `token.json`, personal access tokens) must be included in `.gitignore` and never committed.
3. **Execution Latency:**
   - End-to-end execution of all `[Script]` steps must complete within $< 5$ seconds on local developer hardware.

---

## 6. Definition of Done (DoD) Checklist

- [ ] All code implemented in clean Python 3.x scripts with docstrings and type annotations.
- [ ] Deliverable packaged under `timesheet-skill/` with `SKILL.md` and `workflow.md` compatible with Antigravity and Claude Code.
- [ ] `workflow.md` explicitly tags every single step as `[Script]` or `[AI]`.
- [ ] Pulls Git commits (scanning across workspace `/Users/van.tran/Documents/dev`), GitHub PRs, and Google Calendar events automatically without manual re-typing.
- [ ] Storage uses dual model: canonical `timesheet.jsonl` + rendered `timesheet.md`.
- [ ] Entries match the Section 3.2 schema: one entry per time block, topic-grouped descriptions, inline `PRs:` list, and source audit trails.
- [ ] Safe re-runs: re-executing updates existing blocks without duplicating entries.
- [ ] `scripts/track_tokens.py` captures daily token counts into `telemetry.csv` for both Antigravity and Claude Code sessions.
- [ ] `IMPROVEMENT.md` records daily review notes and optimizations.
- [ ] Git commit history is incremental, one story at a time, with conventional commit messages that cite the story ID (e.g., `feat(ingest): ... (US-1.1)`).
