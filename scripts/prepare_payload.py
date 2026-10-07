#!/usr/bin/env python3
"""
Chronological Time-Block Slicer & Pre-Filter (US-2.1).

Deterministic script that buckets the harvester outputs (calendar events from
fetch_calendar.py, commits from fetch_commits.py, PRs from fetch_prs.py) into
time blocks for one date:

- Every calendar event becomes a block keyed by its event id.
- Commits and PRs inside an event are mapped into that event's block.
- Activity outside events is clustered into focus blocks, split on inactivity
  gaps >= 90 minutes, at the lunch boundaries (12:00 and 13:00), and wherever a
  calendar event sits between two items. Focus block ids are anchored to the
  block's earliest commit (`<date>_focus_HHMM`, see `focus_block_id`), so they
  survive re-runs, amends, and rebases.

Stdout receives the compact AI payload (a JSON array, `[]` when there is no
data) trimmed to stay under the 500-token input budget. `--blocks-out` writes
the authoritative block records (date, times, source audit trail) that later
steps merge the AI descriptions into, wrapped as `{"date": ..., "failed_sources":
[...], "blocks": [...]}`. `failed_sources` lists the harvesters the workflow
reported as failed (`--failed-source`), so save_entry.py knows the run is
partial and must not prune.

With `--timesheet`, a re-run reuses the saved title and description of every
block whose sources (event, commits, PRs) are unchanged since it was logged:
the block is marked `reuse` in `--blocks-out` and left out of the AI payload,
so each block is described about once per day however often the workflow runs
(US-4.2 daily token budget).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

GAP_THRESHOLD = timedelta(minutes=90)
LUNCH_BOUNDARIES = (time(12, 0), time(13, 0))
ROUNDING = timedelta(minutes=15)
TOKEN_BUDGET = 500
SOURCES = ("commits", "prs", "calendar")
CHARS_PER_TOKEN = 3

# Progressively tighter (max text length, max items per list) until the payload fits.
TRIM_LEVELS: Tuple[Tuple[int, Optional[int]], ...] = (
    (72, None),
    (48, None),
    (48, 5),
    (32, 3),
    (24, 2),
    (16, 1),
)


class PayloadError(Exception):
    """Raised when an input file is missing or malformed."""


def parse_local(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp (with offset or `Z`) and normalize it to the local timezone."""
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone()


def load_json_array(path: Optional[str]) -> List[Dict[str, Any]]:
    """Load a harvester output file. A missing flag (None) means no data."""
    if path is None:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8") or "[]")
    except OSError as e:
        raise PayloadError(f"Cannot read input file {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise PayloadError(f"Input file {path} is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise PayloadError(f"Input file {path} must contain a JSON array.")
    return [item for item in data if isinstance(item, dict)]


def load_saved(path: Optional[str], target_date: date) -> Dict[str, Dict[str, Any]]:
    """
    Load the target date's rows from timesheet.jsonl, keyed by block_id. A
    missing flag or file means nothing was logged yet.
    """
    if path is None or not Path(path).exists():
        return {}
    saved: Dict[str, Dict[str, Any]] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise PayloadError(f"Cannot read timesheet {path}: {e}") from e
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise PayloadError(f"Timesheet {path} line {number} is not valid JSON: {e}") from e
        if isinstance(row, dict) and row.get("date") == target_date.isoformat() and row.get("block_id"):
            saved[str(row["block_id"])] = row
    return saved


def same_sources(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """
    True when two source audit trails name the same event, commits, and PRs
    (order ignored). `repos` is ignored: it follows from the commits and PRs,
    and rows logged before it existed should still be reused.
    """
    return (
        a.get("calendar_event") == b.get("calendar_event")
        and sorted(a.get("commits") or []) == sorted(b.get("commits") or [])
        and sorted(a.get("prs") or []) == sorted(b.get("prs") or [])
    )


def mark_reusable(blocks: List[Dict[str, Any]], saved: Dict[str, Dict[str, Any]]) -> List[str]:
    """
    Attach the saved title and description (`reuse`) to every block whose
    sources match its logged row, so the AI does not describe it again.
    Returns the reused block ids.
    """
    reused: List[str] = []
    for block in blocks:
        row = saved.get(block["block_id"])
        if not row or not row.get("title") or not row.get("description"):
            continue
        if same_sources(block["sources"], row.get("sources") or {}):
            block["reuse"] = {"title": row["title"], "description": row["description"]}
            reused.append(block["block_id"])
    return reused


def first_line(text: str) -> str:
    """Return the first non-empty line of text, stripped (drops verbose commit bodies)."""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def floor_time(dt: datetime) -> datetime:
    """Round a datetime down to the ROUNDING grid."""
    step = int(ROUNDING.total_seconds() // 60)
    return dt.replace(minute=dt.minute - dt.minute % step, second=0, microsecond=0)


def ceil_time(dt: datetime) -> datetime:
    """Round a datetime up to the ROUNDING grid."""
    floored = floor_time(dt)
    return floored if floored == dt else floored + ROUNDING


def normalize_events(events: List[Dict[str, Any]], day_start: datetime, day_end: datetime) -> List[Dict[str, Any]]:
    """
    Normalize calendar events to local time, clip them to the target day, and
    drop malformed, empty, or off-day events. Sorted chronologically.
    """
    out: List[Dict[str, Any]] = []
    for ev in events:
        try:
            start = max(parse_local(ev["start"]), day_start)
            end = min(parse_local(ev["end"]), day_end)
            event_id = str(ev["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        out.append({"id": event_id, "title": first_line(ev.get("title", "")), "start": start, "end": end})
    out.sort(key=lambda e: (e["start"], e["end"], e["id"]))
    return out


def normalize_items(
    commits: List[Dict[str, Any]],
    prs: List[Dict[str, Any]],
    target_date: date,
) -> List[Dict[str, Any]]:
    """
    Merge commits and PRs into one chronological activity list, normalized to
    local time and filtered to target_date. Malformed records are skipped.
    """
    items: List[Dict[str, Any]] = []
    for c in commits:
        try:
            ts = parse_local(c["timestamp"])
            items.append({
                "kind": "commit",
                "ts": ts,
                "repo": str(c.get("repo", "")),
                "hash": str(c["hash"]),
                "text": first_line(c.get("message", "")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    for p in prs:
        try:
            ts = parse_local(p["updated_at"])
            items.append({
                "kind": "pr",
                "ts": ts,
                "repo": str(p.get("repo", "")),
                "number": int(p["number"]),
                "text": first_line(p.get("title", "")),
                "status": str(p.get("status", "")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    items = [i for i in items if i["ts"].date() == target_date]
    items.sort(key=lambda i: (i["ts"], i["kind"], i["repo"], str(i.get("hash", i.get("number")))))
    return items


def containing_event(ts: datetime, events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the event whose [start, end) contains ts; the shortest one wins on overlap."""
    matches = [e for e in events if e["start"] <= ts < e["end"]]
    if not matches:
        return None
    return min(matches, key=lambda e: (e["end"] - e["start"], e["start"], e["id"]))


def should_split(prev: datetime, nxt: datetime, events: List[Dict[str, Any]]) -> bool:
    """Decide whether two consecutive out-of-event items belong to different focus blocks."""
    if nxt - prev >= GAP_THRESHOLD:
        return True
    for boundary in LUNCH_BOUNDARIES:
        if prev.time() < boundary <= nxt.time():
            return True
    return any(e["start"] < nxt and e["end"] > prev for e in events)


def cluster_items(items: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split chronologically sorted out-of-event items into focus clusters."""
    clusters: List[List[Dict[str, Any]]] = []
    for item in items:
        if clusters and not should_split(clusters[-1][-1]["ts"], item["ts"], events):
            clusters[-1].append(item)
        else:
            clusters.append([item])
    return clusters


def focus_span(cluster: List[Dict[str, Any]], events: List[Dict[str, Any]], day_end: datetime) -> Tuple[datetime, datetime]:
    """
    Compute a focus block's start/end: the first and last activity rounded out
    to the 15-minute grid, never overlapping a neighbouring calendar event.
    """
    first, last = cluster[0]["ts"], cluster[-1]["ts"]
    start = floor_time(first)
    end = ceil_time(last)
    if end <= start:
        end = start + ROUNDING
    lower = max((e["end"] for e in events if e["end"] <= first), default=None)
    upper = min((e["start"] for e in events if e["start"] > last), default=None)
    if lower is not None:
        start = max(start, lower)
    if upper is not None:
        end = min(end, upper)
    end = min(end, day_end)
    return start, end


def focus_block_id(target_date: date, cluster: List[Dict[str, Any]]) -> str:
    """
    Derive a focus block id that stays stable across re-runs.

    Anchors on the author time of the cluster's earliest commit
    (`<date>_focus_HHMM`): author dates survive amend and rebase, and later
    activity never moves the earliest commit. A PR-only cluster anchors on its
    earliest PR (`<date>_focus_<repo>-pr<number>`), because a PR's updated_at
    changes every time it is touched.
    """
    prefix = f"{target_date.isoformat()}_focus_"
    commits = [i for i in cluster if i["kind"] == "commit"]
    if commits:
        return prefix + commits[0]["ts"].strftime("%H%M")
    first_pr = cluster[0]
    return f"{prefix}{first_pr['repo'] or 'pr'}-pr{first_pr['number']}"


def hhmm(dt: datetime, day_end: datetime) -> str:
    """Format a block boundary as HH:MM; the end of day renders as 23:59."""
    if dt >= day_end:
        return "23:59"
    return dt.strftime("%H:%M")


def build_blocks(
    target_date: date,
    events: List[Dict[str, Any]],
    commits: List[Dict[str, Any]],
    prs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Bucket events, commits, and PRs into chronological time blocks.

    Each block has `block_id`, `kind` ("calendar" or "focus"), `date`,
    `start_time`, `end_time`, internal `start`/`end` datetimes, the `items`
    mapped into it, and a `sources` audit trail.
    """
    day_start = datetime.combine(target_date, time.min).astimezone()
    day_end = datetime.combine(target_date + timedelta(days=1), time.min).astimezone()

    norm_events = normalize_events(events, day_start, day_end)
    items = normalize_items(commits, prs, target_date)

    by_event: Dict[str, List[Dict[str, Any]]] = {e["id"]: [] for e in norm_events}
    outside: List[Dict[str, Any]] = []
    for item in items:
        ev = containing_event(item["ts"], norm_events)
        if ev is None:
            outside.append(item)
        else:
            by_event[ev["id"]].append(item)

    blocks: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ev in norm_events:
        if ev["id"] in seen_ids:
            continue
        seen_ids.add(ev["id"])
        blocks.append({
            "block_id": ev["id"],
            "kind": "calendar",
            "title": ev["title"],
            "start": ev["start"],
            "end": ev["end"],
            "items": by_event[ev["id"]],
        })

    for cluster in cluster_items(outside, norm_events):
        start, end = focus_span(cluster, norm_events, day_end)
        block_id = focus_block_id(target_date, cluster)
        # Two clusters can only share an anchor minute in pathological cases
        # (e.g. an event boundary inside that minute); keep ids unique anyway.
        suffix = 2
        while block_id in seen_ids:
            block_id = f"{focus_block_id(target_date, cluster)}_{suffix}"
            suffix += 1
        seen_ids.add(block_id)
        blocks.append({
            "block_id": block_id,
            "kind": "focus",
            "title": None,
            "start": start,
            "end": end,
            "items": cluster,
        })

    blocks.sort(key=lambda b: (b["start"], b["end"], b["block_id"]))
    for b in blocks:
        b["date"] = target_date.isoformat()
        b["start_time"] = hhmm(b["start"], day_end)
        b["end_time"] = hhmm(b["end"], day_end)
        b["sources"] = {
            "calendar_event": b["title"],
            "commits": [i["hash"] for i in b["items"] if i["kind"] == "commit"],
            "prs": sorted({i["number"] for i in b["items"] if i["kind"] == "pr"}),
            "repos": sorted({i["repo"] for i in b["items"] if i["repo"]}),
        }
    return blocks


def block_record(block: Dict[str, Any]) -> Dict[str, Any]:
    """Project a block to its authoritative, JSON-serializable record (for --blocks-out)."""
    record = {
        "block_id": block["block_id"],
        "kind": block["kind"],
        "date": block["date"],
        "start_time": block["start_time"],
        "end_time": block["end_time"],
        "sources": block["sources"],
    }
    if "reuse" in block:
        record["reuse"] = block["reuse"]
    return record


def blocks_file(target_date: date, blocks: List[Dict[str, Any]], failed_sources: Sequence[str]) -> Dict[str, Any]:
    """Build the --blocks-out document: the run's date, failed harvesters, and block records."""
    return {
        "date": target_date.isoformat(),
        "failed_sources": sorted(set(failed_sources)),
        "blocks": [block_record(b) for b in blocks],
    }


def truncate(text: str, max_len: int) -> str:
    """Shorten text to max_len characters, marking the cut with '..'."""
    return text if len(text) <= max_len else text[: max_len - 2].rstrip() + ".."


def cap_list(values: List[str], max_items: Optional[int]) -> List[str]:
    """Keep at most max_items values, summarizing the rest as '+N more'."""
    if max_items is None or len(values) <= max_items:
        return values
    return values[:max_items] + [f"+{len(values) - max_items} more"]


def compact_block(block: Dict[str, Any], max_len: int, max_items: Optional[int]) -> Dict[str, Any]:
    """
    Build the minimal AI view of one block: id, time range, event title, commit
    subjects grouped by repo (duplicates removed), and PR one-liners. No hashes,
    bodies, diffs, attendees, or timestamps beyond the block range.
    """
    out: Dict[str, Any] = {"id": block["block_id"], "time": f"{block['start_time']}-{block['end_time']}"}
    if block["title"]:
        out["event"] = truncate(block["title"], max_len)

    by_repo: Dict[str, List[str]] = {}
    for item in block["items"]:
        if item["kind"] != "commit":
            continue
        subjects = by_repo.setdefault(item["repo"] or "?", [])
        subject = truncate(item["text"], max_len)
        if subject not in subjects:
            subjects.append(subject)
    if by_repo:
        out["commits"] = {repo: cap_list(msgs, max_items) for repo, msgs in by_repo.items()}

    pr_lines: List[str] = []
    seen_prs: set[int] = set()
    for item in block["items"]:
        if item["kind"] != "pr" or item["number"] in seen_prs:
            continue
        seen_prs.add(item["number"])
        status = f" ({item['status']})" if item["status"] else ""
        pr_lines.append(f"#{item['number']} {truncate(item['text'], max_len)}{status}")
    if pr_lines:
        out["prs"] = cap_list(pr_lines, max_items)
    return out


def dump_compact(payload: Sequence[Dict[str, Any]]) -> str:
    """Serialize the payload without whitespace (whitespace costs tokens)."""
    return json.dumps(list(payload), separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~3 characters per token; compact JSON tokenizes densely)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def build_ai_payload(blocks: List[Dict[str, Any]], budget: int = TOKEN_BUDGET) -> Tuple[List[Dict[str, Any]], int]:
    """
    Build the compact AI payload, trimming text length and list sizes level by
    level until the estimated token count fits the budget. Returns the payload
    and its estimated token count (which may still exceed the budget on an
    extremely busy day, after the tightest level).
    """
    payload: List[Dict[str, Any]] = []
    tokens = 0
    for max_len, max_items in TRIM_LEVELS:
        payload = [compact_block(b, max_len, max_items) for b in blocks]
        tokens = estimate_tokens(dump_compact(payload))
        if tokens < budget:
            break
    return payload, tokens


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Bucket calendar events, commits, and PRs into time blocks and emit a compact AI payload."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (default: today in local timezone).",
    )
    parser.add_argument("--commits", type=str, default=None, help="JSON output of fetch_commits.py.")
    parser.add_argument("--prs", type=str, default=None, help="JSON output of fetch_prs.py.")
    parser.add_argument("--calendar", type=str, default=None, help="JSON output of fetch_calendar.py.")
    parser.add_argument(
        "--blocks-out",
        type=str,
        default=None,
        help="Write the authoritative block records (times and sources) to this JSON file.",
    )
    parser.add_argument(
        "--failed-source",
        action="append",
        choices=SOURCES,
        default=[],
        help="A harvester that failed this run (repeatable); recorded in --blocks-out so nothing is pruned.",
    )
    parser.add_argument(
        "--timesheet",
        type=str,
        default=None,
        help="Saved timesheet.jsonl; blocks whose sources are unchanged reuse their saved text and skip the AI.",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=TOKEN_BUDGET,
        help=f"Token budget for the AI payload (default: {TOKEN_BUDGET}).",
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

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.stderr.write(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.\n")
            return 1
    else:
        target_date = datetime.now().astimezone().date()

    try:
        commits = load_json_array(args.commits)
        prs = load_json_array(args.prs)
        events = load_json_array(args.calendar)
        saved = load_saved(args.timesheet, target_date)
    except PayloadError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    blocks = build_blocks(target_date, events, commits, prs)
    reused = mark_reusable(blocks, saved)

    if args.blocks_out:
        try:
            Path(args.blocks_out).write_text(
                json.dumps(blocks_file(target_date, blocks, args.failed_source), indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            sys.stderr.write(f"Error: Cannot write blocks file {args.blocks_out}: {e}\n")
            return 1

    payload, tokens = build_ai_payload([b for b in blocks if "reuse" not in b], budget=args.budget)
    if args.failed_source:
        sys.stderr.write(f"Warning: partial run, failed sources: {', '.join(sorted(set(args.failed_source)))}.\n")
    if args.verbose:
        sys.stderr.write(
            f"{len(blocks)} blocks ({len(reused)} unchanged, reused), ~{tokens} tokens (budget {args.budget}).\n"
        )
    if tokens >= args.budget:
        sys.stderr.write(f"Warning: AI payload is ~{tokens} tokens, over the {args.budget}-token budget.\n")

    sys.stdout.write(dump_compact(payload) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
