#!/usr/bin/env python3
"""
Unit tests for scripts/save_entry.py (US-3.1).
"""

from datetime import datetime
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch

from scripts import merge_descriptions, prepare_payload, save_entry
from scripts.save_entry import upsert

DATE = "2026-10-06"
NOW = "2026-10-06T16:35:00+07:00"


def entry(
    block_id: str,
    kind: str = "focus",
    date: str = DATE,
    start: str = "14:00",
    end: str = "16:30",
    description: str = "Did X.",
    commits: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a merged entry as printed by merge_descriptions.py."""
    return {
        "block_id": block_id,
        "kind": kind,
        "date": date,
        "start_time": start,
        "end_time": end,
        "title": "Topic",
        "description": description,
        "sources": {"calendar_event": None, "commits": commits or [], "prs": []},
    }


def row(block_id: str, kind: str = "focus", date: str = DATE, created_at: str = "2026-10-06T12:00:00+07:00", **kw: Any) -> Dict[str, Any]:
    """Build an existing timesheet.jsonl row."""
    return {**entry(block_id, kind=kind, date=date, **kw), "created_at": created_at}


def local_ts(hour: int, minute: int) -> str:
    """ISO timestamp on DATE in the local timezone."""
    return datetime(2026, 10, 6, hour, minute).astimezone().isoformat()


class TestUpsert(unittest.TestCase):
    """Scenarios 1-3: upsert keyed on (date, block_id) and pruning of a complete run."""

    def test_existing_row_updated_in_place_keeping_created_at(self) -> None:
        rows = [row("a"), row("b"), row("c")]
        new, summary = upsert(rows, [entry("b", end="17:00", description="Did more.")], DATE, None, NOW)
        self.assertEqual([r["block_id"] for r in new], ["a", "b", "c"])
        self.assertEqual(new[1]["end_time"], "17:00")
        self.assertEqual(new[1]["description"], "Did more.")
        self.assertEqual(new[1]["created_at"], "2026-10-06T12:00:00+07:00")
        self.assertEqual(summary["updated"], ["b"])

    def test_identical_rerun_is_unchanged(self) -> None:
        rows = [row("a")]
        new, summary = upsert(rows, [entry("a")], DATE, ["a"], NOW)
        self.assertEqual(new, rows)
        self.assertEqual(summary["unchanged"], ["a"])

    def test_new_block_appended_with_created_at(self) -> None:
        new, summary = upsert([row("a")], [entry("a"), entry("b")], DATE, ["a", "b"], NOW)
        self.assertEqual([r["block_id"] for r in new], ["a", "b"])
        self.assertEqual(new[1]["created_at"], NOW)
        self.assertEqual(list(new[1]), [
            "block_id", "kind", "date", "start_time", "end_time", "title", "description", "sources", "created_at",
        ])
        self.assertEqual(summary["inserted"], ["b"])

    def test_same_block_id_on_another_date_is_a_different_key(self) -> None:
        rows = [row("event_1", kind="calendar", date="2026-10-05")]
        new, summary = upsert(rows, [entry("event_1", kind="calendar")], DATE, ["event_1"], NOW)
        self.assertEqual([(r["date"], r["block_id"]) for r in new], [("2026-10-05", "event_1"), (DATE, "event_1")])
        self.assertEqual(summary["inserted"], ["event_1"])

    def test_merged_focus_blocks_pruned_other_dates_untouched(self) -> None:
        rows = [
            row("2026-10-05_focus_1100", date="2026-10-05"),
            row(f"{DATE}_focus_0900"),
            row(f"{DATE}_focus_1100"),
        ]
        merged = entry(f"{DATE}_focus_0900", start="09:00", end="12:00")
        new, summary = upsert(rows, [merged], DATE, [merged["block_id"]], NOW)
        self.assertEqual([r["block_id"] for r in new], ["2026-10-05_focus_1100", f"{DATE}_focus_0900"])
        self.assertEqual(new[1]["end_time"], "12:00")
        self.assertEqual(summary["removed"], [f"{DATE}_focus_1100"])

    def test_cancelled_meeting_pruned(self) -> None:
        rows = [row("event_123", kind="calendar"), row(f"{DATE}_focus_1000")]
        new, summary = upsert(rows, [entry(f"{DATE}_focus_1000")], DATE, [f"{DATE}_focus_1000"], NOW)
        self.assertEqual([r["block_id"] for r in new], [f"{DATE}_focus_1000"])
        self.assertEqual(summary["removed"], ["event_123"])

    def test_partial_run_prunes_nothing(self) -> None:
        rows = [row("event_123", kind="calendar"), row(f"{DATE}_focus_1100")]
        new, summary = upsert(rows, [entry(f"{DATE}_focus_0900")], DATE, None, NOW)
        self.assertEqual([r["block_id"] for r in new], ["event_123", f"{DATE}_focus_1100", f"{DATE}_focus_0900"])
        self.assertEqual(summary["removed"], [])

    def test_rows_of_other_kinds_are_never_pruned(self) -> None:
        rows = [row("manual_1", kind="manual")]
        new, summary = upsert(rows, [], DATE, [], NOW)
        self.assertEqual(new, rows)
        self.assertEqual(summary["removed"], [])

    def test_duplicate_rows_in_file_collapse(self) -> None:
        rows = [row("a"), row("a", description="Stale.")]
        new, _ = upsert(rows, [entry("a")], DATE, ["a"], NOW)
        self.assertEqual(len(new), 1)


class TestMain(unittest.TestCase):
    """CLI behaviour, including Scenario 5 (partial runs) and the full script pipeline."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.timesheet = self.tmp / "timesheet.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, data: Any) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return str(path)

    def _blocks(self, ids: List[str], failed: Optional[List[str]] = None, date: str = DATE) -> str:
        return self._write("blocks.json", {
            "date": date,
            "failed_sources": failed or [],
            "blocks": [{"block_id": i} for i in ids],
        })

    def _rows(self) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in self.timesheet.read_text().splitlines()]

    def _run(self, module: Any, argv: List[str], stdin: str = "") -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), patch("sys.stdin", io.StringIO(stdin)):
            code = module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _save(self, blocks: str, entries: List[Dict[str, Any]]) -> tuple:
        return self._run(save_entry, ["--blocks", blocks, "--timesheet", str(self.timesheet)], json.dumps(entries))

    def test_rerun_never_duplicates(self) -> None:
        blocks = self._blocks(["event_123", f"{DATE}_focus_1000"])
        entries = [entry("event_123", kind="calendar"), entry(f"{DATE}_focus_1000")]
        code, out, _ = self._save(blocks, entries)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["inserted"], ["event_123", f"{DATE}_focus_1000"])
        first = self.timesheet.read_text()

        code, out, _ = self._save(blocks, entries)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["unchanged"], ["event_123", f"{DATE}_focus_1000"])
        self.assertEqual(self.timesheet.read_text(), first)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        self._save(self._blocks(["a"]), [entry("a")])
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])

    def test_partial_run_reported_and_nothing_pruned(self) -> None:
        self.timesheet.write_text(json.dumps(row("event_123", kind="calendar")) + "\n")
        code, out, err = self._save(self._blocks([f"{DATE}_focus_1000"], failed=["calendar"]), [entry(f"{DATE}_focus_1000")])
        self.assertEqual(code, 0)
        self.assertEqual([r["block_id"] for r in self._rows()], ["event_123", f"{DATE}_focus_1000"])
        self.assertTrue(json.loads(out)["partial"])
        self.assertIn("partial run", err)

    def test_complete_empty_run_keeps_the_date(self) -> None:
        self.timesheet.write_text(json.dumps(row("event_123", kind="calendar")) + "\n")
        before = self.timesheet.read_text()
        code, out, err = self._save(self._blocks([]), [])
        self.assertEqual(code, 0)
        self.assertEqual(self.timesheet.read_text(), before)
        self.assertEqual(json.loads(out)["removed"], [])
        self.assertIn("--allow-empty", err)
        self.assertIn("event_123", err)

    def test_allow_empty_prunes_the_date(self) -> None:
        self.timesheet.write_text(
            json.dumps(row("event_123", kind="calendar")) + "\n"
            + json.dumps(row("2026-10-05_focus_0900", date="2026-10-05")) + "\n"
        )
        code, out, _ = self._run(
            save_entry,
            ["--blocks", self._blocks([]), "--timesheet", str(self.timesheet), "--allow-empty"],
            "[]",
        )
        self.assertEqual(code, 0)
        self.assertEqual([r["block_id"] for r in self._rows()], ["2026-10-05_focus_0900"])
        self.assertEqual(json.loads(out)["removed"], ["event_123"])

    def test_empty_run_on_fresh_date_is_silent(self) -> None:
        code, _, err = self._save(self._blocks([]), [])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_corrupt_timesheet_aborts_without_writing(self) -> None:
        self.timesheet.write_text(json.dumps(row("a")) + "\n{not json\n")
        before = self.timesheet.read_text()
        code, out, err = self._save(self._blocks(["a"]), [entry("a")])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("line 2", err)
        self.assertEqual(self.timesheet.read_text(), before)

    def test_entry_for_another_date_rejected(self) -> None:
        code, _, err = self._save(self._blocks(["a"]), [entry("a", date="2026-10-05")])
        self.assertEqual(code, 1)
        self.assertIn("not the run date", err)

    def test_bare_blocks_array_rejected(self) -> None:
        code, _, err = self._save(self._write("blocks.json", []), [])
        self.assertEqual(code, 1)
        self.assertIn("failed_sources", err)

    def test_entry_missing_fields_rejected(self) -> None:
        code, _, err = self._save(self._blocks(["a"]), [{"block_id": "a", "date": DATE}])
        self.assertEqual(code, 1)
        self.assertIn("missing fields", err)

    def _pipeline(self, commits: List[Dict[str, Any]], events: List[Dict[str, Any]], failed: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Run prepare_payload -> merge_descriptions (no AI output) -> save_entry; return the timesheet rows."""
        blocks = self.tmp / "blocks.json"
        argv = [
            "--date", DATE,
            "--commits", self._write("c.json", commits),
            "--calendar", self._write("e.json", events),
            "--blocks-out", str(blocks),
        ]
        for source in failed or []:
            argv += ["--failed-source", source]
        self.assertEqual(self._run(prepare_payload, argv)[0], 0)
        code, merged, _ = self._run(merge_descriptions, ["--blocks", str(blocks)], "{}")
        self.assertEqual(code, 0)
        self.assertEqual(self._save(str(blocks), json.loads(merged))[0], 0)
        return self._rows()

    def test_pipeline_gap_fill_merges_focus_blocks(self) -> None:
        commit = lambda h, m, sha: {"repo": "r", "hash": sha, "timestamp": local_ts(h, m), "message": "m"}
        rows = self._pipeline([commit(9, 0, "a1"), commit(11, 0, "b2")], [])
        self.assertEqual([r["block_id"] for r in rows], [f"{DATE}_focus_0900", f"{DATE}_focus_1100"])

        rows = self._pipeline([commit(9, 0, "a1"), commit(10, 0, "c3"), commit(11, 0, "b2")], [])
        self.assertEqual([r["block_id"] for r in rows], [f"{DATE}_focus_0900"])
        self.assertEqual(rows[0]["sources"]["commits"], ["a1", "c3", "b2"])

    def test_pipeline_cancelled_meeting_commits_move_to_focus(self) -> None:
        commits = [{"repo": "r", "hash": "a1", "timestamp": local_ts(14, 30), "message": "m"}]
        meeting = {"id": "event_123", "title": "Review", "start": local_ts(14, 0), "end": local_ts(15, 0)}
        rows = self._pipeline(commits, [meeting])
        self.assertEqual([r["block_id"] for r in rows], ["event_123"])

        rows = self._pipeline(commits, [], failed=["calendar"])
        self.assertEqual([r["block_id"] for r in rows], ["event_123", f"{DATE}_focus_1430"])

        rows = self._pipeline(commits, [])
        self.assertEqual([r["block_id"] for r in rows], [f"{DATE}_focus_1430"])
        self.assertEqual(rows[0]["sources"]["commits"], ["a1"])


if __name__ == "__main__":
    unittest.main()
