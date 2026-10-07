#!/usr/bin/env python3
"""
Unit tests for scripts/merge_descriptions.py (US-2.2).
"""

import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch

from scripts.merge_descriptions import (
    MergeError,
    clean_summary,
    main,
    merge,
    parse_ai_output,
)


def block(
    block_id: str,
    kind: str = "focus",
    event: Optional[str] = None,
    commits: Optional[List[str]] = None,
    prs: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Build a `prepare_payload.py --blocks-out` record."""
    return {
        "block_id": block_id,
        "kind": kind,
        "date": "2026-10-06",
        "start_time": "10:00",
        "end_time": "11:30",
        "sources": {"calendar_event": event, "commits": commits or [], "prs": prs or []},
    }


class TestParseAiOutput(unittest.TestCase):
    """Scenario 2: the AI returns a block_id -> description mapping."""

    def test_object_values(self) -> None:
        self.assertEqual(parse_ai_output('{"b1":{"description":"Did X."}}'), {"b1": "Did X."})

    def test_code_fence_and_plain_strings_tolerated(self) -> None:
        text = '```json\n{"b1": "Did X.", "b2": {"description": "Did Y."}}\n```'
        self.assertEqual(parse_ai_output(text), {"b1": "Did X.", "b2": "Did Y."})

    def test_empty_output_is_no_descriptions(self) -> None:
        self.assertEqual(parse_ai_output("  \n"), {})

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(MergeError):
            parse_ai_output("Here are your descriptions: b1 did X")

    def test_non_object_rejected(self) -> None:
        with self.assertRaises(MergeError):
            parse_ai_output('[{"b1": "Did X."}]')


class TestCleanSummary(unittest.TestCase):
    """Scenario 1: inline PR list is authoritative and never hallucinated."""

    def test_strips_trailing_pr_list(self) -> None:
        self.assertEqual(clean_summary("Built OAuth client. PRs: #3, #4", [3, 4]), "Built OAuth client")
        self.assertEqual(clean_summary("Built OAuth client; PR: #3 and #4.", [3, 4]), "Built OAuth client")
        self.assertEqual(clean_summary("Built OAuth client. PRs: none", []), "Built OAuth client")

    def test_removes_references_to_prs_outside_block(self) -> None:
        self.assertEqual(
            clean_summary("Reviewed PR #7 and fixed #3 regression (#99).", [3]),
            "Reviewed and fixed #3 regression",
        )

    def test_collapses_whitespace_and_newlines(self) -> None:
        self.assertEqual(clean_summary("  Built\n  harvester  tests. ", []), "Built harvester tests")

    def test_caps_length(self) -> None:
        self.assertLessEqual(len(clean_summary("word " * 200, [])), 240)


class TestMerge(unittest.TestCase):
    """Scenario 2: descriptions merged into authoritative metadata."""

    def test_merges_description_and_keeps_authoritative_fields(self) -> None:
        blocks = [block("event_123", kind="calendar", event="Focus: Development", commits=["8f3b21a"], prs=[3])]
        ai = {"event_123": "Implemented Google Calendar OAuth client and telemetry parser. PRs: #3"}

        entries, fallbacks, unknown = merge(blocks, ai)

        self.assertEqual(entries, [{
            "block_id": "event_123",
            "kind": "calendar",
            "date": "2026-10-06",
            "start_time": "10:00",
            "end_time": "11:30",
            "description": "Implemented Google Calendar OAuth client and telemetry parser. PRs: #3",
            "sources": {"calendar_event": "Focus: Development", "commits": ["8f3b21a"], "prs": [3]},
        }])
        self.assertEqual((fallbacks, unknown), ([], []))

    def test_pr_list_rebuilt_from_sources(self) -> None:
        blocks = [block("b1", prs=[1182, 1174]), block("b2")]
        ai = {"b1": "Shipped billing export. PRs: #1182", "b2": "Refactored config loader. PRs: #5"}

        entries, _, _ = merge(blocks, ai)

        self.assertEqual(entries[0]["description"], "Shipped billing export. PRs: #1174, #1182")
        self.assertEqual(entries[1]["description"], "Refactored config loader.")

    def test_missing_and_empty_descriptions_fall_back(self) -> None:
        blocks = [
            block("standup", kind="calendar", event="Daily Standup"),
            block("2026-10-06_focus_1000", commits=["a1", "b2"], prs=[4]),
            block("2026-10-06_focus_pilot-pr9", prs=[9]),
        ]
        entries, fallbacks, _ = merge(blocks, {"2026-10-06_focus_1000": "  "})

        self.assertEqual(
            [e["description"] for e in entries],
            ["Daily Standup.", "Focus work (2 commits). PRs: #4", "Pull request work. PRs: #9"],
        )
        self.assertEqual(fallbacks, ["standup", "2026-10-06_focus_1000", "2026-10-06_focus_pilot-pr9"])

    def test_unknown_block_ids_ignored(self) -> None:
        entries, _, unknown = merge([block("b1")], {"b1": "Did X", "invented": "Did Y"})
        self.assertEqual([e["block_id"] for e in entries], ["b1"])
        self.assertEqual(unknown, ["invented"])


class TestMain(unittest.TestCase):
    """CLI behavior."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def write(self, name: str, data: Any) -> str:
        path = self.tmp / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return str(path)

    def run_main(self, argv: List[str], stdin: str = "") -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), patch("sys.stdout", out), patch("sys.stderr", err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end_from_stdin(self) -> None:
        blocks = self.write("blocks.json", [block("b1", prs=[2]), block("b2", commits=["c3"])])
        code, out, err = self.run_main(["--blocks", blocks], stdin='{"b1":{"description":"Wrote tests. PRs: #2"}}')

        self.assertEqual(code, 0)
        entries = json.loads(out)
        self.assertEqual([e["description"] for e in entries], ["Wrote tests. PRs: #2", "Focus work (1 commit)."])
        self.assertIn("fallback for: b2", err)

    def test_descriptions_file_and_wrapped_blocks_file(self) -> None:
        blocks = self.write("blocks.json", {"date": "2026-10-06", "failed_sources": [], "blocks": [block("b1")]})
        ai = self.write("ai.json", {"b1": {"description": "Did X"}})
        code, out, _ = self.run_main(["--blocks", blocks, "--descriptions", ai])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["description"], "Did X.")

    def test_no_blocks_outputs_empty_array(self) -> None:
        code, out, _ = self.run_main(["--blocks", self.write("blocks.json", "[]")], stdin="{}")
        self.assertEqual((code, json.loads(out)), (0, []))

    def test_malformed_ai_output_fails(self) -> None:
        blocks = self.write("blocks.json", [block("b1")])
        code, out, err = self.run_main(["--blocks", blocks], stdin="Sure! Here is the summary.")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not valid JSON", err)

    def test_missing_blocks_file_fails(self) -> None:
        code, _, err = self.run_main(["--blocks", str(self.tmp / "nope.json")], stdin="{}")
        self.assertEqual(code, 1)
        self.assertIn("Cannot read blocks file", err)


if __name__ == "__main__":
    unittest.main()
