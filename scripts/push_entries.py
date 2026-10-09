#!/usr/bin/env python3
"""
Gradion Workspace Timesheet Push (US-3.3).

Deterministic script that pushes one date's rows from `timesheet.jsonl` to the
Gradion Workspace Timesheet app, through the workspace's app-tool proxy
(`POST /api/me/apps/timesheet/tools/<tool>/call`) authenticated with a
personal API token (`GRADION_PAT`, else the Keychain item `gradion-pat`).

Each pushed block's remote entry id is kept in `timesheet-remote.jsonl`, keyed
on `(date, block_id)`, together with the fields last sent, so re-runs are safe:

- A block never pushed is created with `log_time`.
- A pushed block whose time, description, or task (its `[<repos>] <title>`
  heading label) changed is updated with `edit_time`; a task that became empty
  is cleared.
- A pushed block that is unchanged is left alone.
- A block not pushed by this tool whose time range is already on the timesheet
  (e.g. logged by hand) is skipped, never overwritten. With `--adopt`, a single
  such entry with exactly the block's time range (e.g. one the app imported
  from the calendar) is taken over instead: its description and task are
  replaced with `edit_time` and it is recorded as pushed by this tool.
- A pushed entry whose block no longer exists locally, or that is missing
  remotely, is reported; the app has no delete tool, so it is fixed by hand.

Nothing is pushed for a partial run (the `--blocks` file lists
`failed_sources`). `--dry-run` reads the remote entries and prints the plan
without creating or editing anything. Stdout receives a JSON summary; the exit
code is 1 when the token is missing or rejected or any call fails.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

try:
    from scripts.render_timesheet import block_label, write_text_atomic
    from scripts.save_entry import DEFAULT_TIMESHEET, SaveError, load_timesheet, read_json
except ImportError:  # run as `python scripts/push_entries.py`
    from render_timesheet import block_label, write_text_atomic
    from save_entry import DEFAULT_TIMESHEET, SaveError, load_timesheet, read_json

DEFAULT_BASE_URL = "https://workspace.gradion.com"
DEFAULT_STATE = "timesheet-remote.jsonl"
DEFAULT_CLASSIFICATION = "Gradion Intern Academy 2026"
APP_SLUG = "timesheet"
TOKEN_ENV = "GRADION_PAT"
TOKEN_KEYCHAIN_SERVICE = "gradion-pat"
PAGE_SIZE = 50
TASK_MAX_CHARS = 150  # the app's limit on the free-text `task` field
TIMEOUT_SECONDS = 20
# One list_my_entries text line: "2026-10-05 09:00-12:00 (3h) | ... · <entry uuid>".
ENTRY_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})-(\d{2}:\d{2}) .*· ([0-9a-fA-F-]{36})\s*$")


class PushError(Exception):
    """Raised when the token is missing, or a request fails before any tool result."""


class ToolError(Exception):
    """Raised when a tool call returns an error result (e.g. an overlap or a policy limit)."""


def find_token() -> Optional[str]:
    """Return the personal API token from GRADION_PAT, else the macOS Keychain."""
    value = os.environ.get(TOKEN_ENV)
    if value:
        return value
    try:  # `security` prints only the secret with -w; a missing item exits 44
        result = subprocess.run(
            ["security", "find-generic-password", "-s", TOKEN_KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):  # not macOS, or no security tool
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


class TimesheetClient:
    """Calls the Timesheet app's MCP tools through the workspace proxy."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call one tool and return its result; raise ToolError on an error result."""
        request = urllib.request.Request(
            f"{self.base_url}/api/me/apps/{APP_SLUG}/tools/{tool}/call",
            data=json.dumps({"arguments": arguments}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            code = _error_code(e)
            if e.code == 401:
                raise PushError(
                    f"Token rejected ({code}): it may be expired or revoked. Create a new personal API token "
                    f"(Write action, Timesheet app) and store it with "
                    f"`security add-generic-password -U -a \"$USER\" -s {TOKEN_KEYCHAIN_SERVICE} -w`."
                ) from e
            if e.code == 403:
                raise PushError(f"Token not allowed to call {tool} ({code}): it needs the Write action and the Timesheet app.") from e
            raise ToolError(f"HTTP {e.code} {code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise PushError(f"Cannot reach {self.base_url}: {e}") from e
        except json.JSONDecodeError as e:
            raise PushError(f"{tool} returned a non-JSON response: {e}") from e
        if result.get("isError"):
            raise ToolError(result_text(result) or "tool returned an error")
        return result


def _error_code(error: urllib.error.HTTPError) -> str:
    """Return the machine `error` code of an HTTP error body, else its reason."""
    try:
        return str(json.loads(error.read().decode("utf-8")).get("error") or error.reason)
    except (ValueError, AttributeError, OSError):
        return str(error.reason)


def result_text(result: Dict[str, Any]) -> str:
    """Join the text content blocks of a tool result."""
    return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text").strip()


def created_entry_id(result: Dict[str, Any]) -> Optional[str]:
    """Return the id of the entry a log_time result refers to."""
    refs = (result.get("structuredContent") or {}).get("resources") or result.get("resources") or []
    for ref in refs:
        if ref.get("kind") == "entry" and ref.get("id"):
            return str(ref["id"])
    return None


def list_remote(client: TimesheetClient, date: str) -> Dict[str, Tuple[str, str]]:
    """Return the caller's entries on date as {entry_id: (startTime, endTime)}, following pages."""
    entries: Dict[str, Tuple[str, str]] = {}
    offset = 0
    while True:
        result = client.call("list_my_entries", {"dateFrom": date, "dateTo": date, "limit": PAGE_SIZE, "offset": offset})
        lines = [m for m in map(ENTRY_LINE.match, result_text(result).splitlines()) if m]
        for match in lines:
            if match.group(1) == date:
                entries[match.group(4)] = (match.group(2), match.group(3))
        if len(lines) < PAGE_SIZE:
            return entries
        offset += PAGE_SIZE


def load_state(path: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Load pushed-entry records keyed on (date, block_id); a missing file is empty."""
    rows = load_timesheet(path)
    for row in rows:
        if not row.get("entry_id"):
            raise SaveError(f"State file {path} has a record without `entry_id` ({row['block_id']}).")
    return {(row["date"], row["block_id"]): row for row in rows}


def save_state(path: Path, state: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    """Write the state file atomically, ordered by date and block id."""
    lines = (json.dumps(state[key], ensure_ascii=False) + "\n" for key in sorted(state))
    write_text_atomic(path, "".join(lines))


def desired_fields(row: Dict[str, Any]) -> Dict[str, str]:
    """Map a timesheet row to the remote entry fields this tool owns; the heading label becomes the task."""
    fields = {
        "date": row["date"],
        "startTime": row["start_time"],
        "endTime": row["end_time"],
        "description": row["description"],
    }
    task = block_label(row)[:TASK_MAX_CHARS]
    if task:
        fields["task"] = task
    return fields


def plan(
    rows: List[Dict[str, Any]],
    state: Dict[Tuple[str, str], Dict[str, Any]],
    remote: Dict[str, Tuple[str, str]],
    date: str,
    adopt: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Decide the action for each of date's rows. Returns lists of
    {block_id, fields[, entry_id, changes]} per action: `create`, `edit`,
    `adopt` (with `adopt`, the one entry not ours with exactly the block's
    time range, taken over), `unchanged`, `skipped` (time range already on the
    timesheet, not ours), `missing` (pushed entry no longer remote), and
    `orphaned` (pushed entry whose block is gone locally). Edits and adoptions
    come before creates, each by start time.
    """
    actions: Dict[str, List[Dict[str, Any]]] = {
        k: [] for k in ("edit", "adopt", "create", "unchanged", "skipped", "missing", "orphaned")
    }
    owned = {s["entry_id"] for (d, _), s in state.items() if d == date}
    foreign: Dict[Tuple[str, str], List[str]] = {}
    for entry_id, span in sorted(remote.items()):
        if entry_id not in owned:
            foreign.setdefault(span, []).append(entry_id)

    for row in sorted(rows, key=lambda r: (r["start_time"], r["end_time"], r["block_id"])):
        fields = desired_fields(row)
        item: Dict[str, Any] = {"block_id": row["block_id"], "fields": fields}
        record = state.get((date, row["block_id"]))
        if record:
            item["entry_id"] = record["entry_id"]
            if record["entry_id"] not in remote:
                actions["missing"].append(item)
                continue
            pushed = record.get("pushed", {})
            changes: Dict[str, Any] = {k: v for k, v in fields.items() if pushed.get(k) != v}
            if pushed.get("task") and "task" not in fields:
                changes["task"] = None  # edit_time clears the task on null
            if changes:
                item["changes"] = changes
                actions["edit"].append(item)
            else:
                actions["unchanged"].append(item)
        elif (fields["startTime"], fields["endTime"]) in foreign:
            candidates = foreign[(fields["startTime"], fields["endTime"])]
            if adopt and len(candidates) == 1:
                item["entry_id"] = candidates.pop()  # an entry is adopted by one block at most
                # The remote description and task are unknown, so both are sent; no local task clears it.
                item["changes"] = {"description": fields["description"], "task": fields.get("task")}
                actions["adopt"].append(item)
            else:
                actions["skipped"].append(item)
        else:
            actions["create"].append(item)

    local_ids = {r["block_id"] for r in rows}
    for (d, block_id), record in sorted(state.items()):
        if d == date and block_id not in local_ids:
            actions["orphaned"].append({"block_id": block_id, "entry_id": record["entry_id"]})
    return actions


def partial_run(blocks_path: Optional[str], date: str) -> List[str]:
    """Return the failed sources recorded in a `prepare_payload.py --blocks-out` file for date."""
    if not blocks_path:
        return []
    data = read_json(blocks_path, "blocks")
    if not isinstance(data, dict) or data.get("date") != date:
        raise SaveError(f"Blocks file {blocks_path} is not for {date}.")
    return [str(s) for s in data.get("failed_sources", [])]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Push one date's timesheet.jsonl rows to the Gradion Workspace Timesheet app."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Date to push, YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--timesheet",
        type=str,
        default=DEFAULT_TIMESHEET,
        help=f"Timesheet JSON Lines file to read (default: {DEFAULT_TIMESHEET}).",
    )
    parser.add_argument(
        "--state",
        type=str,
        default=DEFAULT_STATE,
        help=f"Pushed-entry ids per (date, block_id) (default: {DEFAULT_STATE}).",
    )
    parser.add_argument(
        "--blocks",
        type=str,
        default=None,
        help="Blocks file from `prepare_payload.py --blocks-out`; a partial run listed there is not pushed.",
    )
    parser.add_argument(
        "--classification",
        type=str,
        default=os.environ.get("GRADION_CLASSIFICATION", DEFAULT_CLASSIFICATION),
        help=f"Project or activity name or code for new entries (default: $GRADION_CLASSIFICATION or {DEFAULT_CLASSIFICATION!r}).",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.environ.get("GRADION_WORKSPACE_URL", DEFAULT_BASE_URL),
        help=f"Workspace URL (default: $GRADION_WORKSPACE_URL or {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--adopt",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Take over an entry this tool did not create when it has exactly a block's time range "
        "(e.g. imported from the calendar), replacing its description and task (default: off; "
        "the last of --adopt/--no-adopt wins).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the remote entries and print the plan without creating or editing anything.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print diagnostic information to stderr.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Main execution entry point."""
    args = parse_args(argv)
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        sys.stderr.write(f"Error: Invalid --date {args.date!r}; expected YYYY-MM-DD.\n")
        return 1
    state_path = Path(args.state)
    summary: Dict[str, Any] = {"date": args.date, "dry_run": args.dry_run}

    try:
        failed_sources = partial_run(args.blocks, args.date)
        rows = [r for r in load_timesheet(Path(args.timesheet)) if r["date"] == args.date]
        state = load_state(state_path)
    except SaveError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1
    if failed_sources:
        sys.stderr.write(
            f"Warning: partial run for {args.date} (failed sources: {', '.join(failed_sources)}); nothing pushed.\n"
        )
        sys.stdout.write(json.dumps({**summary, "partial": True, "failed_sources": failed_sources}, indent=2) + "\n")
        return 0

    token = find_token()
    if not token:
        sys.stderr.write(
            f"Error: No personal API token. Set {TOKEN_ENV}, or store one with "
            f"`security add-generic-password -a \"$USER\" -s {TOKEN_KEYCHAIN_SERVICE} -w`.\n"
        )
        return 1
    client = TimesheetClient(args.base_url, token)

    try:
        remote = list_remote(client, args.date)
    except (PushError, ToolError) as e:
        sys.stderr.write(f"Error: Cannot list remote entries for {args.date}: {e}\n")
        return 1
    actions = plan(rows, state, remote, args.date, adopt=args.adopt)
    if args.verbose:
        sys.stderr.write(
            f"{args.date}: {len(rows)} local block(s), {len(remote)} remote entr{'y' if len(remote) == 1 else 'ies'}.\n"
        )

    done: Dict[str, List[str]] = {"created": [], "updated": [], "adopted": []}
    outcome = {"edit": "updated", "adopt": "adopted", "create": "created"}
    failed: List[Dict[str, str]] = []
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    for action in ("edit", "adopt", "create"):
        for item in actions[action]:
            block_id, fields = item["block_id"], item["fields"]
            if args.dry_run:
                done[outcome[action]].append(block_id)
                continue
            try:
                if action in ("edit", "adopt"):
                    verb = "Re-synced" if action == "edit" else "Adopted"
                    client.call("edit_time", {
                        "entryId": item["entry_id"],
                        "reason": f"{verb} from timesheet logger block {block_id}",
                        **{k: v for k, v in item["changes"].items() if k != "date"},
                    })
                    entry_id = item["entry_id"]
                else:
                    result = client.call("log_time", {**fields, "classification": args.classification})
                    entry_id = created_entry_id(result) or ""
                    if not entry_id:
                        raise ToolError("log_time succeeded but returned no entry id; check the Timesheet app")
            except PushError as e:
                sys.stderr.write(f"Error: {e}\n")
                return 1
            except ToolError as e:
                failed.append({"block_id": block_id, "error": str(e)})
                continue
            state[(args.date, block_id)] = {
                "date": args.date, "block_id": block_id, "entry_id": entry_id, "pushed": fields, "pushed_at": now,
            }
            try:  # after every write, so a later failure never loses an entry id
                save_state(state_path, state)
            except OSError as e:
                sys.stderr.write(f"Error: Pushed {block_id} as {entry_id} but cannot write {state_path}: {e}\n")
                return 1
            done[outcome[action]].append(block_id)

    for item in actions["skipped"]:
        f = item["fields"]
        sys.stderr.write(
            f"Warning: {item['block_id']} skipped: {f['startTime']}-{f['endTime']} is already on the timesheet "
            "and was not pushed by this tool" + ("" if args.adopt else "; pass --adopt to take it over") + ".\n"
        )
    for item in actions["missing"]:
        sys.stderr.write(
            f"Warning: {item['block_id']} was pushed as {item['entry_id']}, which is no longer on the timesheet; "
            f"remove its line from {state_path} to push it again.\n"
        )
    for item in actions["orphaned"]:
        sys.stderr.write(
            f"Warning: {item['block_id']} is no longer a local block, but entry {item['entry_id']} is still on the "
            "timesheet; edit or delete it in the Timesheet app.\n"
        )
    for item in failed:
        sys.stderr.write(f"Error: {item['block_id']} not pushed: {item['error']}\n")

    summary.update({
        **done,
        "unchanged": [i["block_id"] for i in actions["unchanged"]],
        "skipped": [i["block_id"] for i in actions["skipped"]],
        "missing": [i["block_id"] for i in actions["missing"]],
        "orphaned": [i["block_id"] for i in actions["orphaned"]],
        "failed": failed,
    })
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
