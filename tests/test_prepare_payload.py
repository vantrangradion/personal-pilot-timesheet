#!/usr/bin/env python3
"""
Unit tests for scripts/prepare_payload.py (US-2.1).

Timestamps are built in the local timezone so assertions on HH:MM block
times hold regardless of the machine's TZ.
"""

from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List
import unittest
from unittest.mock import patch

from scripts.prepare_payload import (
    build_ai_payload,
    build_blocks,
    dump_compact,
    estimate_tokens,
    main,
)

DAY = date(2026, 10, 6)


def local(hh: int, mm: int, day: date = DAY) -> str:
    """ISO timestamp for hh:mm local time on day."""
    return datetime(day.year, day.month, day.day, hh, mm).astimezone().isoformat(timespec="seconds")


def commit(hh: int, mm: int, sha: str, message: str = "feat: work", repo: str = "pilot") -> Dict[str, Any]:
    """Build a fetch_commits.py record."""
    return {"repo": repo, "hash": sha, "timestamp": local(hh, mm), "message": message, "files_changed_count": 1}


def pr(hh: int, mm: int, number: int, title: str = "feat: thing", status: str = "opened") -> Dict[str, Any]:
    """Build a fetch_prs.py record."""
    return {
        "repo": "pilot",
        "number": number,
        "title": title,
        "status": status,
        "url": f"https://github.com/o/pilot/pull/{number}",
        "updated_at": local(hh, mm),
    }


def event(event_id: str, title: str, start: tuple, end: tuple) -> Dict[str, Any]:
    """Build a fetch_calendar.py record."""
    return {"id": event_id, "title": title, "start": local(*start), "end": local(*end)}


def spans(blocks: List[Dict[str, Any]]) -> List[tuple]:
    """(block_id, start_time, end_time) for each block."""
    return [(b["block_id"], b["start_time"], b["end_time"]) for b in blocks]


class TestBuildBlocks(unittest.TestCase):
    """Scenario 1 & 2: time-block segmentation."""

    def test_calendar_and_focus_blocks(self) -> None:
        events = [
            event("standup", "Daily Standup", (9, 30), (10, 0)),
            event("focus", "Focus: Development", (14, 0), (16, 30)),
        ]
        commits = [
            commit(9, 45, "a1"),       # inside standup
            commit(10, 20, "b2"),      # outside -> focus block
            commit(11, 5, "c3"),       # same focus block (gap < 90m)
            commit(15, 0, "d4"),       # inside Focus event
        ]
        prs = [pr(15, 30, 3), pr(17, 10, 4)]  # inside Focus event; outside -> focus block

        blocks = build_blocks(DAY, events, commits, prs)

        self.assertEqual(
            spans(blocks),
            [
                ("standup", "09:30", "10:00"),
                ("2026-10-06_focus_1020", "10:15", "11:15"),
                ("focus", "14:00", "16:30"),
                ("2026-10-06_focus_pilot-pr4", "17:00", "17:15"),
            ],
        )
        by_id = {b["block_id"]: b for b in blocks}
        self.assertEqual(by_id["standup"]["sources"], {"calendar_event": "Daily Standup", "commits": ["a1"], "prs": []})
        self.assertEqual(by_id["2026-10-06_focus_1020"]["sources"]["commits"], ["b2", "c3"])
        self.assertIsNone(by_id["2026-10-06_focus_1020"]["sources"]["calendar_event"])
        self.assertEqual(by_id["focus"]["sources"], {"calendar_event": "Focus: Development", "commits": ["d4"], "prs": [3]})
        self.assertEqual(by_id["2026-10-06_focus_pilot-pr4"]["sources"]["prs"], [4])

    def test_event_without_activity_is_still_a_block(self) -> None:
        blocks = build_blocks(DAY, [event("e1", "Sync", (9, 0), (9, 30))], [], [])
        self.assertEqual(spans(blocks), [("e1", "09:00", "09:30")])
        self.assertEqual(blocks[0]["sources"], {"calendar_event": "Sync", "commits": [], "prs": []})

    def test_focus_block_does_not_overlap_adjacent_event(self) -> None:
        events = [event("e1", "Review", (10, 0), (10, 10)), event("e2", "1:1", (11, 0), (11, 30))]
        blocks = build_blocks(DAY, events, [commit(10, 12, "a"), commit(10, 55, "b")], [])
        self.assertEqual(spans(blocks)[1], ("2026-10-06_focus_1012", "10:10", "11:00"))

    def test_event_between_items_splits_focus_cluster(self) -> None:
        events = [event("e1", "Standup", (10, 0), (10, 15))]
        blocks = build_blocks(DAY, events, [commit(9, 50, "a"), commit(10, 20, "b")], [])
        self.assertEqual(
            spans(blocks),
            [("2026-10-06_focus_0950", "09:45", "10:00"), ("e1", "10:00", "10:15"), ("2026-10-06_focus_1020", "10:15", "10:30")],
        )

    def test_no_events_splits_on_gap_and_lunch(self) -> None:
        commits = [
            commit(9, 0, "a"),
            commit(9, 40, "b"),
            commit(11, 10, "c"),   # 90m gap -> new block
            commit(11, 50, "d"),
            commit(12, 10, "e"),   # crosses 12:00 -> new block
            commit(13, 15, "f"),   # crosses 13:00 -> new block
            commit(14, 30, "g"),   # 75m gap -> same block
        ]
        blocks = build_blocks(DAY, [], commits, [pr(15, 0, 9)])
        self.assertEqual(
            spans(blocks),
            [
                ("2026-10-06_focus_0900", "09:00", "09:45"),
                ("2026-10-06_focus_1110", "11:00", "12:00"),
                ("2026-10-06_focus_1210", "12:00", "12:15"),
                ("2026-10-06_focus_1315", "13:15", "15:00"),
            ],
        )
        self.assertEqual(blocks[3]["sources"], {"calendar_event": None, "commits": ["f", "g"], "prs": [9]})

    def test_gap_just_under_threshold_stays_together(self) -> None:
        blocks = build_blocks(DAY, [], [commit(9, 0, "a"), commit(10, 29, "b")], [])
        self.assertEqual(len(blocks), 1)

    def test_block_ids_stable_when_later_activity_added(self) -> None:
        first = build_blocks(DAY, [], [commit(9, 0, "a")], [])
        later = build_blocks(DAY, [], [commit(9, 0, "a"), commit(16, 0, "b")], [])
        self.assertEqual(first[0]["block_id"], later[0]["block_id"])
        self.assertEqual(later[1]["block_id"], "2026-10-06_focus_1600")

    def test_block_ids_stable_when_activity_inserted_between_blocks(self) -> None:
        before = build_blocks(DAY, [], [commit(9, 0, "a"), commit(16, 0, "b")], [])
        after = build_blocks(DAY, [], [commit(9, 0, "a"), commit(14, 0, "new"), commit(16, 0, "b")], [])
        before_ids = {b["block_id"] for b in before}
        self.assertTrue(before_ids <= {b["block_id"] for b in after})
        self.assertEqual(len(after), 3)

    def test_block_id_survives_amend_and_pr_updates(self) -> None:
        before = build_blocks(DAY, [], [commit(9, 0, "orig")], [pr(9, 10, 7)])
        # Amend rewrites the hash but keeps the author date; the PR gets touched again later.
        after = build_blocks(DAY, [], [commit(9, 0, "amended")], [pr(9, 50, 7)])
        self.assertEqual(before[0]["block_id"], "2026-10-06_focus_0900")
        self.assertEqual(after[0]["block_id"], "2026-10-06_focus_0900")

    def test_timestamps_normalized_and_off_day_items_dropped(self) -> None:
        # Same instant expressed in UTC must land in the same local block.
        utc_ts = datetime.fromisoformat(local(9, 10)).astimezone(timezone.utc).isoformat()
        commits = [
            {"repo": "pilot", "hash": "utc", "timestamp": utc_ts, "message": "fix", "files_changed_count": 1},
            {"repo": "pilot", "hash": "old", "timestamp": local(9, 10, date(2026, 10, 5)), "message": "x"},
            {"repo": "pilot", "message": "malformed"},
        ]
        blocks = build_blocks(DAY, [event("e1", "Work", (9, 0), (10, 0))], commits, [])
        self.assertEqual(blocks[0]["sources"]["commits"], ["utc"])
        self.assertEqual(len(blocks), 1)

    def test_event_crossing_midnight_is_clipped(self) -> None:
        ev = {"id": "late", "title": "Deploy", "start": local(23, 0), "end": local(1, 0, date(2026, 10, 7))}
        blocks = build_blocks(DAY, [ev], [], [])
        self.assertEqual(spans(blocks), [("late", "23:00", "23:59")])

    def test_no_data(self) -> None:
        self.assertEqual(build_blocks(DAY, [], [], []), [])


class TestAiPayload(unittest.TestCase):
    """Scenario 3: compact payload under the token budget."""

    def test_compact_shape_strips_verbose_fields(self) -> None:
        blocks = build_blocks(
            DAY,
            [event("e1", "Focus: Development", (14, 0), (16, 0))],
            [
                commit(14, 10, "a1", "feat: add parser\n\nLong body explaining everything"),
                commit(14, 20, "b2", "feat: add parser"),
                commit(14, 30, "c3", "test: cover parser", repo="devops"),
            ],
            [pr(15, 0, 7, "feat: parser")],
        )
        payload, tokens = build_ai_payload(blocks)
        self.assertEqual(
            payload,
            [{
                "id": "e1",
                "time": "14:00-16:00",
                "event": "Focus: Development",
                "commits": {"pilot": ["feat: add parser"], "devops": ["test: cover parser"]},
                "prs": ["#7 feat: parser (opened)"],
            }],
        )
        text = dump_compact(payload)
        self.assertNotIn("a1", text)
        self.assertNotIn("Long body", text)
        self.assertLess(tokens, 500)

    def test_busy_day_trimmed_under_budget(self) -> None:
        commits = [
            commit(8 + i // 6, (i % 6) * 10, f"h{i}", f"feat(module-{i}): implement a very long descriptive change number {i}")
            for i in range(60)
        ]
        prs = [pr(9 + i, 5, 100 + i, "feat: an equally long pull request title for testing trimming") for i in range(8)]
        payload, tokens = build_ai_payload(build_blocks(DAY, [], commits, prs))
        self.assertLess(tokens, 500)
        self.assertEqual(tokens, estimate_tokens(dump_compact(payload)))
        self.assertIn("more", dump_compact(payload))


class TestMain(unittest.TestCase):
    """CLI behaviour."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, data: Any) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return str(path)

    def _run(self, argv: List[str]) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end_with_blocks_out(self) -> None:
        blocks_out = self.tmp / "blocks.json"
        code, out, _ = self._run([
            "--date", "2026-10-06",
            "--commits", self._write("c.json", [commit(10, 0, "a1")]),
            "--prs", self._write("p.json", [pr(10, 30, 5)]),
            "--calendar", self._write("e.json", [event("e1", "Standup", (9, 30), (9, 45))]),
            "--blocks-out", str(blocks_out),
        ])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([b["id"] for b in payload], ["e1", "2026-10-06_focus_1000"])
        written = json.loads(blocks_out.read_text())
        self.assertEqual(written["date"], "2026-10-06")
        self.assertEqual(written["failed_sources"], [])
        self.assertEqual(
            written["blocks"][1],
            {
                "block_id": "2026-10-06_focus_1000",
                "kind": "focus",
                "date": "2026-10-06",
                "start_time": "10:00",
                "end_time": "10:30",
                "sources": {"calendar_event": None, "commits": ["a1"], "prs": [5]},
            },
        )

    def test_failed_sources_recorded_in_blocks_out(self) -> None:
        blocks_out = self.tmp / "blocks.json"
        code, _, err = self._run([
            "--date", "2026-10-06",
            "--commits", self._write("c.json", [commit(10, 0, "a1")]),
            "--failed-source", "prs",
            "--failed-source", "calendar",
            "--failed-source", "calendar",
            "--blocks-out", str(blocks_out),
        ])
        self.assertEqual(code, 0)
        written = json.loads(blocks_out.read_text())
        self.assertEqual(written["failed_sources"], ["calendar", "prs"])
        self.assertEqual([b["block_id"] for b in written["blocks"]], ["2026-10-06_focus_1000"])
        self.assertIn("partial run", err)

    def test_unknown_failed_source_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(["--date", "2026-10-06", "--failed-source", "jira"])

    def test_no_inputs_outputs_empty_array(self) -> None:
        code, out, _ = self._run(["--date", "2026-10-06", "--commits", self._write("c.json", [])])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_missing_file_fails(self) -> None:
        code, out, err = self._run(["--date", "2026-10-06", "--commits", str(self.tmp / "nope.json")])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot read input file", err)

    def test_non_array_input_fails(self) -> None:
        code, _, err = self._run(["--date", "2026-10-06", "--prs", self._write("p.json", {"a": 1})])
        self.assertEqual(code, 1)
        self.assertIn("JSON array", err)

    def test_invalid_date_fails(self) -> None:
        code, _, err = self._run(["--date", "06-10-2026"])
        self.assertEqual(code, 1)
        self.assertIn("Invalid date", err)


if __name__ == "__main__":
    unittest.main()
