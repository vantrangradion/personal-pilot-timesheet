#!/usr/bin/env python3
"""
JSONL State Manager & Idempotent Upsert (US-3.1).

Deterministic script that persists one date's timesheet entries (the merged
output of merge_descriptions.py) into `timesheet.jsonl`, keyed on
`(date, block_id)`:

- An entry whose key already exists replaces that row in place, keeping its
  original `created_at`, so re-runs never duplicate blocks.
- An entry with a new key is appended as a new line.
- On a complete run, rows for that date the run no longer produces are pruned:
  focus blocks absorbed by a merge and meetings cancelled or declined since the
  last run. Rows for other dates are never touched.
- On a partial run (the blocks file from `prepare_payload.py --blocks-out`
  lists `failed_sources`), nothing is pruned, because an empty fetch is not
  evidence that a meeting was cancelled or that no work was done.
- A complete run that produced no blocks at all prunes nothing either, unless
  `--allow-empty` is passed: a misconfigured harvester can return `[]`
  without failing, and that must not silently wipe a logged day.

The file is rewritten atomically (temporary file in the same directory, then
rename), and a malformed existing file aborts the run without writing.

Stdout receives a JSON summary of the keys inserted, updated, unchanged, and
removed.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_TIMESHEET = "timesheet.jsonl"
PRUNABLE_KINDS = ("calendar", "focus")
REQUIRED_FIELDS = ("block_id", "date", "start_time", "end_time", "description", "sources")
FIELD_ORDER = ("block_id", "kind", "date", "start_time", "end_time", "title", "description", "sources", "created_at")


class SaveError(Exception):
    """Raised when an input file or the timesheet is missing or malformed."""


def read_json(path: str, label: str) -> Any:
    """Read a JSON document from a file, or from stdin when path is '-'."""
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise SaveError(f"Cannot read {label} file {path}: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SaveError(f"{label.capitalize()} file {path} is not valid JSON: {e}") from e


def load_run(path: str) -> Tuple[str, List[str], List[str]]:
    """
    Load the blocks file written by `prepare_payload.py --blocks-out` and
    return the run's date, its failed sources, and the block ids it produced.
    """
    data = read_json(path, "blocks")
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise SaveError(f"Blocks file {path} must be an object with `date`, `failed_sources`, and `blocks`.")
    run_date = data.get("date")
    try:
        datetime.strptime(str(run_date), "%Y-%m-%d")
    except ValueError as e:
        raise SaveError(f"Blocks file {path} has an invalid date: {run_date!r}.") from e
    failed = data.get("failed_sources", [])
    if not isinstance(failed, list):
        raise SaveError(f"Blocks file {path} has a non-list `failed_sources`.")
    block_ids = [str(b["block_id"]) for b in data["blocks"] if isinstance(b, dict) and b.get("block_id")]
    if len(block_ids) != len(data["blocks"]):
        raise SaveError(f"Blocks file {path} contains records without a block_id.")
    return str(run_date), [str(s) for s in failed], block_ids


def load_entries(path: str, run_date: str) -> List[Dict[str, Any]]:
    """Load the merged entries from merge_descriptions.py and validate them against the run date."""
    data = read_json(path, "entries")
    if not isinstance(data, list):
        raise SaveError(f"Entries file {path} must contain a JSON array.")
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise SaveError(f"Entries file {path} contains a non-object entry.")
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            raise SaveError(f"Entry {entry.get('block_id')!r} is missing fields: {', '.join(missing)}.")
        if entry["date"] != run_date:
            raise SaveError(f"Entry {entry['block_id']!r} is dated {entry['date']}, not the run date {run_date}.")
        if entry["block_id"] in seen:
            raise SaveError(f"Entries file {path} lists block {entry['block_id']!r} more than once.")
        seen.add(entry["block_id"])
    return data


def load_timesheet(path: Path) -> List[Dict[str, Any]]:
    """Load existing timesheet rows; a missing file is an empty timesheet. Blank lines are skipped."""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise SaveError(f"Cannot read timesheet {path}: {e}") from e
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise SaveError(f"Timesheet {path} line {number} is not valid JSON: {e}") from e
        if not isinstance(row, dict) or "date" not in row or "block_id" not in row:
            raise SaveError(f"Timesheet {path} line {number} is not a record with `date` and `block_id`.")
        rows.append(row)
    return rows


def to_record(entry: Dict[str, Any], created_at: str) -> Dict[str, Any]:
    """Build a timesheet row from a merged entry, in a stable field order."""
    record = {**entry, "created_at": created_at}
    ordered = {k: record[k] for k in FIELD_ORDER if k in record}
    ordered.update({k: v for k, v in record.items() if k not in ordered})
    return ordered


def upsert(
    rows: List[Dict[str, Any]],
    entries: List[Dict[str, Any]],
    run_date: str,
    keep_ids: Optional[Sequence[str]],
    now: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    """
    Upsert entries into rows keyed on (date, block_id).

    Existing rows are replaced in place (keeping `created_at`), new ones are
    appended, and duplicate keys already in the file collapse to the first.
    When keep_ids is given (a complete run), calendar and focus rows for
    run_date whose block_id is not in keep_ids are removed; pass None for a
    partial run to prune nothing. Returns the new rows and a summary of
    block ids per action.
    """
    by_id = {e["block_id"]: e for e in entries}
    keep = None if keep_ids is None else set(keep_ids) | set(by_id)
    summary: Dict[str, List[str]] = {"inserted": [], "updated": [], "unchanged": [], "removed": []}
    out: List[Dict[str, Any]] = []
    placed: set[str] = set()

    for row in rows:
        if row["date"] != run_date:
            out.append(row)
            continue
        block_id = row["block_id"]
        if block_id in placed:
            continue
        if block_id in by_id:
            record = to_record(by_id[block_id], row.get("created_at") or now)
            summary["unchanged" if record == row else "updated"].append(block_id)
            out.append(record)
            placed.add(block_id)
        elif keep is not None and block_id not in keep and row.get("kind") in PRUNABLE_KINDS:
            summary["removed"].append(block_id)
        else:
            out.append(row)
            placed.add(block_id)

    for entry in entries:
        if entry["block_id"] not in placed:
            out.append(to_record(entry, now))
            summary["inserted"].append(entry["block_id"])
    return out, summary


def write_atomic(path: Path, rows: List[Dict[str, Any]]) -> None:
    """Write rows as JSON Lines via a temporary file in the same directory, then rename over path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Upsert one date's timesheet entries into timesheet.jsonl, keyed on (date, block_id)."
    )
    parser.add_argument(
        "--blocks",
        type=str,
        required=True,
        help="Blocks file written by `prepare_payload.py --blocks-out` (date, failed_sources, blocks).",
    )
    parser.add_argument(
        "--entries",
        type=str,
        default="-",
        help="Merged entries from merge_descriptions.py; '-' reads stdin (default).",
    )
    parser.add_argument(
        "--timesheet",
        type=str,
        default=DEFAULT_TIMESHEET,
        help=f"Timesheet JSON Lines file to update (default: {DEFAULT_TIMESHEET}).",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Let a complete run that produced no blocks remove that date's rows (off by default).",
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
    timesheet = Path(args.timesheet)

    try:
        run_date, failed_sources, block_ids = load_run(args.blocks)
        entries = load_entries(args.entries, run_date)
        rows = load_timesheet(timesheet)
    except SaveError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    unknown = sorted({e["block_id"] for e in entries} - set(block_ids))
    if unknown:
        sys.stderr.write(f"Warning: entries not in the blocks file (saved anyway): {', '.join(unknown)}\n")
    unsaved = sorted(set(block_ids) - {e["block_id"] for e in entries})
    if unsaved:
        sys.stderr.write(f"Warning: blocks without an entry (left as is): {', '.join(unsaved)}\n")

    partial = bool(failed_sources)
    empty_guard = not partial and not block_ids and not args.allow_empty
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    new_rows, summary = upsert(rows, entries, run_date, None if partial or empty_guard else block_ids, now)

    if new_rows != rows or not timesheet.exists():
        try:
            write_atomic(timesheet, new_rows)
        except OSError as e:
            sys.stderr.write(f"Error: Cannot write timesheet {timesheet}: {e}\n")
            return 1

    if partial:
        sys.stderr.write(
            f"Warning: partial run for {run_date} (failed sources: {', '.join(failed_sources)}); "
            "no blocks were pruned.\n"
        )
    kept = [r["block_id"] for r in rows if r["date"] == run_date]
    if empty_guard and kept:
        sys.stderr.write(
            f"Warning: run produced no blocks for {run_date}; kept {len(kept)} existing row(s) "
            f"({', '.join(kept)}). Re-run with --allow-empty to remove them.\n"
        )
    if args.verbose:
        counts = ", ".join(f"{len(ids)} {action}" for action, ids in summary.items())
        sys.stderr.write(f"{timesheet}: {counts}.\n")

    result = {"date": run_date, "partial": partial, "failed_sources": failed_sources, **summary}
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
