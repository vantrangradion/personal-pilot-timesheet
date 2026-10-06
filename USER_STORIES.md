# Product Requirements Document & User Stories
## Personal Pilot Timesheet Logger

* **Author:** Senior Product Manager
* **Target User:** Intern / Pilot Engineer
* **Stakeholder:** Track Leader (Tin) & Internship Supervisors
* **Execution Runner:** Claude Code
* **Version:** 1.0.0
* **Date:** 2026-10-06
* **Status:** Approved / Ready for Sprint 1 Kickoff

---

## 1. Executive Summary & Product Vision

The **Personal Pilot Timesheet Logger** automates daily engineering work logs by aggregating verifiable data from three operational sources:
1. Local Git commits authored in the workspace.
2. GitHub Pull Requests (opened, reviewed, merged).
3. Google Calendar events (meetings, focus blocks).

Rather than manual timesheet entry or unstructured AI summarization, the system enforces a strict **deterministic script vs. AI judgment boundary**:
* **Scripts (`[Script]`):** Handle data retrieval, filtering, time-block mapping, persistence, and telemetry.
* **AI (`[AI]`):** Confined exclusively to judgment tasks: grouping micro-tasks into coherent topics and writing executive-ready descriptions.

---

## 2. Locked Technical & Architectural Decisions

Following trade-off evaluation, the product architecture is finalized as:

1. **Workspace & Multi-Repository Scope:**
   - **Workspace Root:** `/Users/van.tran/Documents/dev` — the tool scans all git repositories located in this directory (e.g. `personal-pilot-timesheet`, `devops`, and future assignments/projects) to capture commits across all active projects.
   - **Tool Repository:** `/Users/van.tran/Documents/dev/personal-pilot-timesheet`.
   - **PR Platform:** **GitHub** using `gh` CLI across all detected GitHub remote repositories.
2. **Google Calendar Integration (Option A):**
   - **Standalone Python Script (`scripts/fetch_calendar.py`)** utilizing Google Calendar API v3 with OAuth 2.0 desktop authentication (`credentials.json` and cached `token.json`).
   - Ensures zero unnecessary token consumption by pre-filtering events before Claude receives context.
3. **Timesheet Storage Architecture (Option C — Dual Model):**
   - **Source of Truth (Data Layer):** `timesheet.jsonl` — machine-readable, schema-validated, and idempotent.
   - **Presentation Layer (View Layer):** `timesheet.md` — rendered human-readable Markdown matching the timesheet "Edit entry" UI specification for Friday supervisor reviews.
4. **Packaging:**
   - Standalone Skill directory: `timesheet-skill/` containing `SKILL.md`, `workflow.md`, and supporting deterministic scripts under `scripts/`.
5. **Token Telemetry:**
   - Deterministic parser `scripts/track_tokens.py` extracting token consumption from local `~/.claude` session transcripts into `telemetry.csv`.

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
  └── US-3.2: Timesheet Markdown View Renderer [P0, Script]

EPIC 4: Skill Packaging & Daily Workflow Guardrails
  ├── US-4.1: Claude Skill & Workflow Packaging [P0, Spec]
  └── US-4.2: Daily Token Budget & Input Minimization Guardrail [P0, Spec]

EPIC 5: Telemetry & Continuous Improvement
  ├── US-5.1: Claude Session Transcript Token Parser [P0, Script]
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

* **Acceptance Criteria:**
  1. Accepts an optional `--workspace-dir` parameter (defaults to `/Users/van.tran/Documents/dev` or env `DEV_WORKSPACE_DIR`).
  2. Scans child directories for Git repositories (`.git`).
  3. Executes `git log` per repository, filtering by `--author="$(git config user.email)"` (or `--author="$(git config user.name)"`).
  4. Pulls commits between `00:00:00` and `23:59:59` of the specified date (defaults to current local date).
  5. Filters out merge commits (`--no-merges`).
  6. Returns a strictly validated JSON array to stdout containing the repository name:
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
  7. Returns exit code `0` with `[]` if no commits exist across any repositories for the date.

---

#### US-1.2: GitHub Pull Request Harvester (Multi-Repo)
* **Story ID:** `US-1.2`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** a script `scripts/fetch_prs.py` using GitHub CLI (`gh`) or REST API to retrieve PRs I opened, reviewed, or had merged today across my active repositories in `/Users/van.tran/Documents/dev`,  
  > **So that** my collaboration and code review efforts across all projects are captured in my timesheet.

* **Acceptance Criteria:**
  1. Identifies GitHub remotes from detected git repositories in `/Users/van.tran/Documents/dev`.
  2. Uses `gh` CLI commands (`gh pr list`, `gh search prs`) or GitHub API for each repository or user handle.
  3. Queries for:
     - PRs opened by the user today.
     - PRs reviewed by the user today (approvals, comments, changes requested).
     - User PRs merged today.
  4. Emits compact JSON schema including repository identifier:
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
  5. Detects unauthenticated `gh` status cleanly and outputs actionable error guidance.

---

#### US-1.3: Google Calendar OAuth Harvester
* **Story ID:** `US-1.3`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** a script `scripts/fetch_calendar.py` that connects to the Google Calendar API via OAuth 2.0 (`credentials.json`),  
  > **So that** my daily meetings and calendar blocks are automatically retrieved.

* **Acceptance Criteria:**
  1. Uses the official Google API Python Client (`google-api-python-client`, `google-auth-oauthlib`).
  2. Authenticates using `credentials.json` and caches authorization credentials in `token.json` for non-interactive daily runs.
  3. Fetches calendar events between `00:00:00` and `23:59:59` for the target day.
  4. Filters out declined events (`responseStatus == 'declined'`) and transparent/all-day out-of-office blocks.
  5. Emits sorted JSON array:
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
  6. Supports a fallback `--mock <path>` flag for offline testing or setup verification.

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

* **Acceptance Criteria:**
  1. Partitions the day into discrete time blocks using calendar events as primary boundaries.
  2. Fills non-calendar gaps where commit activity occurred into dedicated focus blocks (e.g., `10:00 - 12:30 | Development Block`).
  3. **Zero-Calendar Rule:** If no calendar events exist for the day, creates a single default work block (`09:00 - 18:00`) containing all daily commits and PRs.
  4. Maps commits and PRs into the time block matching their timestamps.
  5. Produces an ultra-compressed payload for Claude with:
     - `block_start`, `block_end`, `block_type` (Meeting vs. Focus).
     - Raw commit subjects and commit hashes.
     - Associated PR numbers and titles.
  6. Enforces payload budget: total input payload for the day MUST remain under 500 tokens.

---

#### US-2.2: AI Topic Clustering & Summary Synthesizer
* **Story ID:** `US-2.2`
* **Type:** `[AI]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As a** Supervisor reviewing daily timesheets,  
  > **I want** Claude to group related micro-tasks into high-level business topics and append inline PR lists,  
  > **So that** timesheets are concise, executive-friendly, and avoid raw commit spam.

* **Acceptance Criteria:**
  1. **Strict Input Boundary:** Claude only processes the pre-filtered JSON from `US-2.1`. No raw `git log` or raw calendar dumps are allowed.
  2. **Topic Grouping:** Synthesizes multiple related commits into one coherent topic (e.g., combines 4 commits on OAuth token refresh and scopes into: *"Google Calendar OAuth 2.0 integration & token refresh lifecycle"*).
  3. **Inline PR Format:** Appends PRs as a single comma-separated list at the end of the description: `PRs: #1182, #1174`.
  4. **Strict Grounding:** The AI must never invent tasks or extrapolate details not present in the script payload.
  5. Outputs a structured JSON matching the Timesheet Entry Schema for each time block.

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

* **Acceptance Criteria:**
  1. Maintains `timesheet.jsonl` as the canonical database of all daily records.
  2. Uses composite unique key: `date + start_time + end_time`.
  3. If an entry with the same composite key already exists, replaces it with the updated payload.
  4. Writes atomically using a temporary file rename to prevent file corruption during crashes.
  5. Stores full source traceability inside each record:
     ```json
     {
       "date": "2026-10-06",
       "start_time": "14:00",
       "end_time": "16:30",
       "description": "Implemented Google Calendar OAuth client and telemetry parser. PRs: #3",
       "sources": {
         "calendar_event": "Focus: Development",
         "commits": ["8f3b21a", "b2c3d4e"],
         "prs": [3]
       },
       "created_at": "2026-10-06T16:35:00+07:00"
     }
     ```

---

#### US-3.2: Timesheet Markdown View Renderer
* **Story ID:** `US-3.2`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As a** Track Leader,  
  > **I want** the script to automatically render `timesheet.md` from `timesheet.jsonl`,  
  > **So that** I can review clean, human-readable timesheet entries during Friday reviews.

* **Acceptance Criteria:**
  1. Triggered automatically following any write to `timesheet.jsonl`.
  2. Renders chronologically grouped entries matching the Section 9 "Edit entry" UI specification:
     ```markdown
     # Timesheet Log

     ## 2026-10-06 (Tuesday)

     ### 09:30 - 10:00 | Standup & Planning
     - **Date:** 2026-10-06
     - **Start time:** 09:30
     - **End time:** 10:00
     - **Description:** Weekly sprint kickoff and timesheet architecture review.
     - **Sources:** Calendar: "Team Standup"

     ### 10:00 - 12:30 | Core Engineering
     - **Date:** 2026-10-06
     - **Start time:** 10:00
     - **End time:** 12:30
     - **Description:** Git commit harvester and CLI test suite implementation. PRs: #1, #2
     - **Sources:** Commits: `8f3b21a`, `4d5e6f7`; PRs: #1, #2
     ```
  3. Supports clean visual diffs in Git pull requests.

---

### Epic 4: Skill Packaging & Daily Workflow Guardrails

#### US-4.1: Claude Skill & Workflow Packaging
* **Story ID:** `US-4.1`
* **Type:** `[Spec]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern using Claude Code,  
  > **I want** a packaged `timesheet-skill/SKILL.md` and `timesheet-skill/workflow.md`,  
  > **So that** Claude can run the timesheet generation autonomously upon request.

* **Acceptance Criteria:**
  1. `SKILL.md` defines:
     - Name: `personal-pilot-timesheet`
     - Description: Daily personal timesheet generator from Git, GitHub, and Google Calendar.
     - Triggers: User prompts like `"log today's timesheet"` or `"generate timesheet for today"`.
     - Required CLI prerequisites (`git`, `gh`, `python3`).
  2. `workflow.md` details every step sequentially with mandatory role tags:
     - `Step 1 [Script]: Run data collection scripts (git, gh, calendar)`
     - `Step 2 [Script]: Run prepare_payload.py to segment time blocks`
     - `Step 3 [AI]: Synthesize topic descriptions and format PR list`
     - `Step 4 [Script]: Run save_entry.py and render timesheet.md`
     - `Step 5 [Script]: Run track_tokens.py to capture session telemetry`
  3. Conforms strictly to the Gradion rule: No `[AI]` tag allowed for any deterministic work.

---

#### US-4.2: Daily Token Budget Guardrail
* **Story ID:** `US-4.2`
* **Type:** `[Spec]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As a** Track Leader,  
  > **I want** `workflow.md` to declare an explicit daily token budget target and enforce input minimization,  
  > **So that** the intern demonstrates first-principles cost efficiency in LLM operations.

* **Acceptance Criteria:**
  1. Declares target daily token budget: **$\le$ 1,200 total tokens per day** (prompt + completion).
  2. Documents input minimization rules:
     - No full git logs or file diffs passed to Claude.
     - No raw Google Calendar event dumps or attendee lists passed to Claude.
     - Only aggregated, pre-filtered block JSON from `scripts/prepare_payload.py`.
  3. Explains the rationale in `workflow.md` for Friday review evaluation.

---

### Epic 5: Telemetry & Continuous Improvement

#### US-5.1: Claude Session Transcript Token Parser
* **Story ID:** `US-5.1`
* **Type:** `[Script]`
* **Priority:** P0 (Must-Have)
* **User Story:**
  > **As an** Intern,  
  > **I want** a script `scripts/track_tokens.py` to extract exact token usage from local `~/.claude/*.jsonl` session files,  
  > **So that** I have auditable empirical data for my Week 4 optimization presentation.

* **Acceptance Criteria:**
  1. Scans `~/.claude/projects/` (or current session transcript directory) for the active Claude session ID.
  2. Sums:
     - `input_tokens`
     - `output_tokens`
     - `cache_read_input_tokens`
     - `cache_creation_input_tokens`
  3. Appends an entry to `telemetry.csv` with columns:
     `date,session_id,input_tokens,output_tokens,cache_tokens,total_tokens,timestamp`.
  4. Correctly aggregates multiple runs on the same day into a unified daily total.

---

#### US-5.2: Continuous Improvement & Experimentation Journal
* **Story ID:** `US-5.2`
* **Type:** `[Process]`
* **Priority:** P1 (Should-Have)
* **User Story:**
  > **As a** Track Leader,  
  > **I want** an `IMPROVEMENT.md` journal maintained daily by the intern,  
  > **So that** the intern proves the continuous improvement loop (*log $\to$ review what broke $\to$ improve $\to$ log again*).

* **Acceptance Criteria:**
  1. Establishes `IMPROVEMENT.md` with standard daily entry schema:
     - `Date`
     - `Observed Failure / Edge Case / Inefficiency`
     - `Root Cause (Script vs. Prompt)`
     - `Action Taken (Code change, rule tightening, script migration)`
     - `Token & Quality Delta`
  2. Documents the baseline kickoff entry for **Week 1 Day 1 (Tue 6 Oct)**.
  3. Provides the source narrative for the Week 4 final presentation comparison (high-token day vs. optimized day).

---

## 5. Non-Functional Requirements (NFRs)

1. **Deterministic Isolation:**
   - Any script failure (e.g., GitHub CLI offline, Calendar network timeout) must emit human-readable diagnostic messages without leaving zombie processes or corrupted files.
2. **Data Privacy & Security:**
   - Secrets (`credentials.json`, `token.json`, personal access tokens) must be included in `.gitignore` and never committed.
3. **Execution Latency:**
   - End-to-end execution of all `[Script]` steps must complete within $< 5$ seconds on local developer hardware.
4. **Git Discipline:**
   - Every feature/story branch follows the convention: `feat/<story-id>-<slug>` (e.g., `feat/us-1.1-git-commits`).
   - One task = one small branch = one focused PR with description; no direct commits to `main`.

---

## 6. Definition of Done (DoD) Checklist

- [ ] All code implemented in clean Python 3.x scripts with docstrings and type annotations.
- [ ] Deliverable packaged under `timesheet-skill/` with `SKILL.md` and `workflow.md`.
- [ ] `workflow.md` explicitly tags every single step as `[Script]` or `[AI]`.
- [ ] Pulls Git commits (scanning across workspace `/Users/van.tran/Documents/dev`), GitHub PRs, and Google Calendar events automatically without manual re-typing.
- [ ] Storage uses dual model: canonical `timesheet.jsonl` + rendered `timesheet.md`.
- [ ] Entries match the Section 3.2 schema: one entry per time block, topic-grouped descriptions, inline `PRs:` list, and source audit trails.
- [ ] Safe re-runs: re-executing updates existing blocks without duplicating entries.
- [ ] `scripts/track_tokens.py` captures daily token counts into `telemetry.csv`.
- [ ] `IMPROVEMENT.md` records daily review notes and optimizations.
- [ ] Git commit history reflects incremental, disciplined PR workflow without direct commits to `main`.
