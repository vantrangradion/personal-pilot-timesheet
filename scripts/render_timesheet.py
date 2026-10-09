#!/usr/bin/env python3
"""
Timesheet Markdown View Renderer (US-3.2).

Deterministic script that renders `timesheet.md` (the human-readable view)
from `timesheet.jsonl` (the source of truth). `save_entry.py` calls it after
every upsert; it can also be run on its own to rebuild the view.

- Dates are grouped under `## <date> (<weekday>)` headers, oldest first.
- Blocks within a date are ordered by start time, then end time, then
  block_id, under `### <start> - <end> | [<repos>] <title>` headings (the
  `[<repos>]` project tag comes from `sources.repos` and is left out when the
  block has no commits or PRs), each listing the
  "Edit entry" fields: Date, Start time, End time, Description, and Sources.
- The output depends only on the rows' content, never on their order in the
  JSONL file or on when the render ran, so unchanged dates and blocks render
  byte-identically and Git diffs stay limited to what actually changed. The
  file is only rewritten (atomically) when its content differs.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, List, Optional

DEFAULT_TIMESHEET = "timesheet.jsonl"
HEADER = "# Timesheet Log"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class RenderError(Exception):
    """Raised when the timesheet is missing or malformed."""


def one_line(value: Any) -> str:
    """Collapse whitespace (including newlines) so a value cannot break the Markdown list."""
    return " ".join(str(value).split()) if value is not None else ""


def date_header(day: str) -> str:
    """Return `## <date> (<weekday>)`, or `## <date>` when the date does not parse."""
    try:
        weekday = WEEKDAYS[datetime.strptime(day, "%Y-%m-%d").weekday()]
    except ValueError:
        return f"## {day}"
    return f"## {day} ({weekday})"


def format_sources(sources: Any) -> str:
    """Render the audit trail: `Calendar: "<event>"; Commits: `a`, `b`; PRs: #1, #2`."""
    if not isinstance(sources, dict):
        return "None"
    parts: List[str] = []
    if sources.get("calendar_event"):
        parts.append(f'Calendar: "{one_line(sources["calendar_event"])}"')
    commits = [one_line(c) for c in sources.get("commits") or []]
    if commits:
        parts.append("Commits: " + ", ".join(f"`{c}`" for c in commits))
    prs = [one_line(p) for p in sources.get("prs") or []]
    if prs:
        parts.append("PRs: " + ", ".join(f"#{p}" for p in prs))
    return "; ".join(parts) or "None"


def project_tag(sources: Any) -> str:
    """Return `[repo]` (or `[repo-a, repo-b]`) for the repos a block's commits and PRs came from, or ''."""
    repos = sources.get("repos") if isinstance(sources, dict) else None
    names = sorted({one_line(r) for r in repos or [] if one_line(r)})
    return f"[{', '.join(names)}]" if names else ""


def block_label(row: Dict[str, Any]) -> str:
    """Return a row's `[<repos>] <title>` label (either part left out when empty), or ''."""
    return " ".join(p for p in (project_tag(row.get("sources")), one_line(row.get("title"))) if p)


def render_block(row: Dict[str, Any]) -> List[str]:
    """Render one timesheet row as a heading and its field list."""
    start, end = one_line(row.get("start_time")), one_line(row.get("end_time"))
    label = block_label(row)
    heading = f"### {start} - {end}" + (f" | {label}" if label else "")
    return [
        heading,
        f"- **Date:** {one_line(row.get('date'))}",
        f"- **Start time:** {start}",
        f"- **End time:** {end}",
        f"- **Description:** {one_line(row.get('description'))}",
        f"- **Sources:** {format_sources(row.get('sources'))}",
    ]


def render(rows: List[Dict[str, Any]]) -> str:
    """Render all timesheet rows as the Markdown view, grouped by date in chronological order."""
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_date.setdefault(str(row["date"]), []).append(row)

    lines = [HEADER]
    for day in sorted(by_date):
        lines += ["", date_header(day)]
        blocks = sorted(
            by_date[day],
            key=lambda r: (str(r.get("start_time", "")), str(r.get("end_time", "")), str(r["block_id"])),
        )
        for row in blocks:
            lines += [""] + render_block(row)
    return "\n".join(lines) + "\n"


def load_rows(path: Path) -> List[Dict[str, Any]]:
    """Load timesheet rows from JSON Lines; a missing file is an empty timesheet. Blank lines are skipped."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise RenderError(f"Cannot read timesheet {path}: {e}") from e
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise RenderError(f"Timesheet {path} line {number} is not valid JSON: {e}") from e
        if not isinstance(row, dict) or "date" not in row or "block_id" not in row:
            raise RenderError(f"Timesheet {path} line {number} is not a record with `date` and `block_id`.")
        rows.append(row)
    return rows


def write_text_atomic(path: Path, text: str) -> None:
    """Write text via a temporary file in the same directory, then rename over path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_markdown(path: Path, rows: List[Dict[str, Any]]) -> bool:
    """Render rows to path, rewriting it only when the content changed. Returns whether it was written."""
    text = render(rows)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    write_text_atomic(path, text)
    return True


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Render timesheet.md from timesheet.jsonl.")
    parser.add_argument(
        "--timesheet",
        type=str,
        default=DEFAULT_TIMESHEET,
        help=f"Timesheet JSON Lines file to read (default: {DEFAULT_TIMESHEET}).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Markdown file to write (default: --timesheet with a .md suffix); '-' prints to stdout.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print diagnostic information to stderr.",
    )
    return parser.parse_args(argv)


def default_markdown_path(timesheet: Path) -> Path:
    """Return the Markdown view path that sits next to a timesheet file (timesheet.jsonl -> timesheet.md)."""
    return timesheet.with_suffix(".md")


def main(argv: Optional[List[str]] = None) -> int:
    """Main execution entry point."""
    args = parse_args(argv)
    timesheet = Path(args.timesheet)
    try:
        rows = load_rows(timesheet)
    except RenderError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    if args.out == "-":
        sys.stdout.write(render(rows))
        return 0

    out = Path(args.out) if args.out else default_markdown_path(timesheet)
    try:
        written = write_markdown(out, rows)
    except OSError as e:
        sys.stderr.write(f"Error: Cannot write {out}: {e}\n")
        return 1
    if args.verbose:
        state = "rendered" if written else "unchanged"
        sys.stderr.write(f"{out}: {state} ({len(rows)} block(s)).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
