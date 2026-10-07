#!/usr/bin/env python3
"""
AI Description Merger (US-2.2, deterministic half).

The [AI] step reads the compact payload from prepare_payload.py and returns
only `{block_id: {"title": "<2-4 word topic>", "description": "<topics>. PRs:
#a, #b"}}`. This script merges those titles and descriptions into the
authoritative block records written by
`prepare_payload.py --blocks-out`, so the AI never has to reproduce dates,
times, or source audit trails.

Guardrails applied to the AI output:

- Fenced (```json) output is unwrapped; anything that is not a JSON object is
  rejected (exit 1) so the workflow can retry the AI step.
- Block ids the scripts did not produce are dropped with a warning.
- Blocks the AI skipped (or described with an empty string) get a
  deterministic fallback title and description from their sources.
- Titles are display labels only (the `timesheet.md` heading); `block_id`
  stays the upsert key. They are cut to one short line with no PR refs.
- The trailing `PRs:` list is rebuilt from the block's `sources.prs`, and
  `#N` references to PRs outside the block are removed, so the AI cannot
  invent or misattribute PRs.

Stdout receives the merged entries as a JSON array (`[]` when there are no
blocks), ready for save_entry.py.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

MAX_DESCRIPTION_LEN = 240
MAX_TITLE_LEN = 48

FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
PR_SUFFIX_RE = re.compile(
    r"[\s.;,:-]*\bPRs?\s*:\s*(?:none|n/?a|-)?\s*(?:#?\d+(?:\s*(?:,|;|&|and)\s*)?)*\.?\s*$",
    re.IGNORECASE,
)
PR_REF_RE = re.compile(r"(?:\bPRs?\s+)?(?<![\w&])#(\d+)\b", re.IGNORECASE)
EMPTY_PARENS_RE = re.compile(r"\(\s*[,;]?\s*\)")


class MergeError(Exception):
    """Raised when the blocks file or the AI output is missing or malformed."""


def load_blocks(path: str) -> List[Dict[str, Any]]:
    """
    Load the authoritative block records written by `prepare_payload.py
    --blocks-out`. Accepts a bare array or an object with a `blocks` array.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8") or "[]")
    except OSError as e:
        raise MergeError(f"Cannot read blocks file {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise MergeError(f"Blocks file {path} is not valid JSON: {e}") from e
    if isinstance(data, dict):
        data = data.get("blocks")
    if not isinstance(data, list):
        raise MergeError(f"Blocks file {path} must contain a JSON array of blocks.")
    blocks = [b for b in data if isinstance(b, dict) and b.get("block_id")]
    if len(blocks) != len(data):
        raise MergeError(f"Blocks file {path} contains records without a block_id.")
    return blocks


def parse_ai_output(text: str) -> Dict[str, Dict[str, str]]:
    """
    Parse the AI response into {block_id: {"title": ..., "description": ...}}.
    Tolerates a Markdown code fence and plain-string values (taken as the
    description); raises MergeError on anything else.
    """
    text = text.strip()
    if not text:
        return {}
    fenced = FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise MergeError(f"AI output is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise MergeError("AI output must be a JSON object mapping block_id to {\"title\": ..., \"description\": ...}.")

    out: Dict[str, Dict[str, str]] = {}
    for block_id, value in data.items():
        if isinstance(value, str):
            value = {"description": value}
        if isinstance(value, dict):
            out[str(block_id)] = {
                field: value[field] for field in ("title", "description") if isinstance(value.get(field), str)
            }
    return out


def pr_suffix(prs: List[int]) -> str:
    """Format the inline PR list, e.g. `PRs: #1174, #1182` (empty when there are none)."""
    return "PRs: " + ", ".join(f"#{n}" for n in sorted(prs)) if prs else ""


def clean_summary(text: str, allowed_prs: List[int]) -> str:
    """
    Normalize an AI summary to one line without its trailing PR list and
    without references to PRs that are not in the block.
    """
    summary = " ".join(str(text).split())
    summary = PR_SUFFIX_RE.sub("", summary)
    allowed = set(allowed_prs)
    summary = PR_REF_RE.sub(lambda m: m.group(0) if int(m.group(1)) in allowed else "", summary)
    summary = EMPTY_PARENS_RE.sub("", summary)
    summary = " ".join(summary.split()).rstrip(" .;,:-")
    if len(summary) > MAX_DESCRIPTION_LEN:
        summary = summary[: MAX_DESCRIPTION_LEN - 2].rstrip(" .;,:-") + ".."
    return summary


def clean_title(text: str) -> str:
    """Normalize an AI title to one short line with no PR references or trailing punctuation."""
    title = PR_SUFFIX_RE.sub("", " ".join(str(text).split()))
    title = EMPTY_PARENS_RE.sub("", PR_REF_RE.sub("", title))
    title = " ".join(title.split()).strip(" .;,:-\"'")
    if len(title) > MAX_TITLE_LEN:
        title = title[: MAX_TITLE_LEN - 2].rstrip(" .;,:-") + ".."
    return title


def fallback_title(block: Dict[str, Any]) -> str:
    """Deterministic title for a block the AI did not title: the meeting name, or the kind of work."""
    sources = block.get("sources") or {}
    if sources.get("calendar_event"):
        return clean_title(sources["calendar_event"]) or "Meeting"
    if not sources.get("commits") and sources.get("prs"):
        return "Pull request work"
    return "Focus work"


def fallback_summary(block: Dict[str, Any]) -> str:
    """Deterministic description for a block the AI did not describe."""
    sources = block.get("sources") or {}
    if sources.get("calendar_event"):
        return str(sources["calendar_event"])
    commits = len(sources.get("commits") or [])
    if commits:
        return f"Focus work ({commits} commit{'s' if commits != 1 else ''})"
    if sources.get("prs"):
        return "Pull request work"
    return "Untitled block"


def compose_description(summary: str, prs: List[int]) -> str:
    """Join a summary and the inline PR list: `<summary>. PRs: #a, #b`."""
    parts = [p for p in (summary, pr_suffix(prs)) if p]
    return ". ".join(parts) + ("" if prs else ".")


def merge(
    blocks: List[Dict[str, Any]], descriptions: Dict[str, Dict[str, str]]
) -> Tuple[List[Dict[str, Any]], List[str], List[str], List[str]]:
    """
    Merge AI titles and descriptions into the authoritative block records.

    Returns the entries (block order preserved), the block ids that fell back
    to a deterministic description, those that fell back to a deterministic
    title, and the unknown ids the AI returned.
    """
    entries: List[Dict[str, Any]] = []
    fallbacks: List[str] = []
    title_fallbacks: List[str] = []
    for block in blocks:
        sources = block.get("sources") or {}
        prs = sorted({int(n) for n in sources.get("prs") or []})
        ai = descriptions.get(block["block_id"], {})
        summary = clean_summary(ai.get("description", ""), prs)
        if not summary:
            summary = fallback_summary(block)
            fallbacks.append(block["block_id"])
        title = clean_title(ai.get("title", ""))
        if not title:
            title = fallback_title(block)
            title_fallbacks.append(block["block_id"])
        entries.append({
            "block_id": block["block_id"],
            "kind": block.get("kind"),
            "date": block.get("date"),
            "start_time": block.get("start_time"),
            "end_time": block.get("end_time"),
            "title": title,
            "description": compose_description(summary, prs),
            "sources": sources,
        })
    known = {b["block_id"] for b in blocks}
    unknown = sorted(set(descriptions) - known)
    return entries, fallbacks, title_fallbacks, unknown


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Merge AI block descriptions into the authoritative time blocks from prepare_payload.py."
    )
    parser.add_argument(
        "--blocks",
        type=str,
        required=True,
        help="Blocks file written by `prepare_payload.py --blocks-out`.",
    )
    parser.add_argument(
        "--descriptions",
        type=str,
        default="-",
        help="AI output JSON ({block_id: {\"title\": ..., \"description\": ...}}); '-' reads stdin (default).",
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
        blocks = load_blocks(args.blocks)
        if args.descriptions == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(args.descriptions).read_text(encoding="utf-8")
            except OSError as e:
                raise MergeError(f"Cannot read descriptions file {args.descriptions}: {e}") from e
        descriptions = parse_ai_output(raw)
    except MergeError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    entries, fallbacks, title_fallbacks, unknown = merge(blocks, descriptions)

    if unknown:
        sys.stderr.write(f"Warning: ignored descriptions for unknown block ids: {', '.join(unknown)}\n")
    if fallbacks:
        sys.stderr.write(f"Warning: no AI description, used fallback for: {', '.join(fallbacks)}\n")
    if title_fallbacks:
        sys.stderr.write(f"Warning: no AI title, used fallback for: {', '.join(title_fallbacks)}\n")
    if args.verbose:
        sys.stderr.write(f"Merged {len(entries) - len(fallbacks)}/{len(entries)} AI descriptions.\n")

    sys.stdout.write(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
