#!/usr/bin/env python3
"""
Tests for scripts/push_entries.py (US-3.3), against a local fake of the
workspace's Timesheet app-tool proxy.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch
import uuid

from scripts import push_entries

DATE = "2026-10-06"
TOKEN = "pat_test"


class FakeTimesheet:
    """In-memory Timesheet app: list_my_entries, log_time, and edit_time with the overlap rule."""

    def __init__(self) -> None:
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.calls: List[str] = []

    def add(self, date: str, start: str, end: str, description: str = "Manual") -> str:
        entry_id = str(uuid.uuid4())
        self.entries[entry_id] = {"date": date, "startTime": start, "endTime": end, "description": description}
        return entry_id

    def overlaps(self, entry: Dict[str, Any], skip: Optional[str] = None) -> bool:
        return any(
            other["date"] == entry["date"] and entry["startTime"] < other["endTime"] and other["startTime"] < entry["endTime"]
            for entry_id, other in self.entries.items() if entry_id != skip
        )

    def handle(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(tool)
        if tool == "list_my_entries":
            rows = sorted(
                ((i, e) for i, e in self.entries.items() if args["dateFrom"] <= e["date"] <= args["dateTo"]),
                key=lambda x: (x[1]["date"], x[1]["startTime"]), reverse=True,
            )[args.get("offset", 0):][:args.get("limit", 10)]
            lines = [f"Showing {len(rows)} of {len(self.entries)} entries"] + [
                f"{e['date']} {e['startTime']}-{e['endTime']} (1h) | Intern Academy [Internal Project] | draft | "
                f"{e['description'][:20]} · {i}" for i, e in rows
            ]
            return text_result("\n".join(lines))
        if tool == "log_time":
            entry = {k: args[k] for k in ("date", "startTime", "endTime", "description")}
            if self.overlaps(entry):
                return text_result("Entry overlaps an existing entry.", error=True)
            entry_id = self.add(**{"date": entry["date"], "start": entry["startTime"], "end": entry["endTime"],
                                   "description": entry["description"]})
            if "task" in args:
                self.entries[entry_id]["task"] = args["task"]
            result = text_result(f"Logged · {entry_id}")
            result["structuredContent"] = {"resources": [{"kind": "entry", "id": entry_id, "label": "x"}]}
            return result
        if tool == "edit_time":
            current = self.entries.get(args["entryId"])
            if current is None:
                return text_result("Entry not found.", error=True)
            editable = ("date", "startTime", "endTime", "description", "task")
            updated = {**current, **{k: v for k, v in args.items() if k in editable}}
            if self.overlaps(updated, skip=args["entryId"]):
                return text_result("Entry overlaps an existing entry.", error=True)
            self.entries[args["entryId"]] = updated
            return text_result("Updated.")
        return text_result(f"Unknown tool {tool}", error=True)


def text_result(text: str, error: bool = False) -> Dict[str, Any]:
    return {"isError": error, "content": [{"type": "text", "text": text}]}


def _make_handler(state: FakeTimesheet) -> type:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                status, payload = 401, {"error": "invalid_token"}
            else:
                tool = self.path.rsplit("/", 2)[-2]
                status, payload = 200, state.handle(tool, body["arguments"])
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def row(block_id: str, start: str, end: str, description: str = "Did X.", date: str = DATE,
        title: str = "Topic") -> Dict[str, Any]:
    return {
        "block_id": block_id, "kind": "focus", "date": date, "start_time": start, "end_time": end,
        "title": title, "description": description, "sources": {}, "created_at": "2026-10-06T18:00:00+07:00",
    }


class TestPushEntries(unittest.TestCase):
    """Test suite for the Gradion Timesheet push."""

    def setUp(self) -> None:
        self.state = FakeTimesheet()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.dir = Path(tempfile.mkdtemp())
        self.timesheet = self.dir / "timesheet.jsonl"
        self.state_file = self.dir / "timesheet-remote.jsonl"
        env = patch.dict(os.environ, {"GRADION_PAT": TOKEN})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir)

    def write_rows(self, *rows: Dict[str, Any]) -> None:
        self.timesheet.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def run_main(self, *extra: str) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--date", DATE, "--timesheet", str(self.timesheet), "--state", str(self.state_file),
                "--base-url", self.base, *extra]
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = push_entries.main(argv)
        summary = json.loads(out.getvalue()) if out.getvalue() else None
        return code, summary, err.getvalue()

    def test_creates_then_rerun_is_unchanged(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"), row("b", "10:00", "11:30"), row("other", "09:00", "10:00", date="2026-10-07"))
        code, summary, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(summary["created"], ["a", "b"])
        self.assertEqual(len(self.state.entries), 2)
        state_rows = [json.loads(l) for l in self.state_file.read_text().splitlines()]
        self.assertEqual({r["block_id"] for r in state_rows}, {"a", "b"})

        calls_before = len(self.state.calls)
        code, summary, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(summary["created"], [])
        self.assertEqual(summary["unchanged"], ["a", "b"])
        self.assertEqual(self.state.calls[calls_before:], ["list_my_entries"])

    def test_changed_block_is_edited_in_place(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"))
        self.run_main()
        self.write_rows(row("a", "09:00", "10:30", description="Did X and Y."))
        code, summary, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(summary["updated"], ["a"])
        (entry,) = self.state.entries.values()
        self.assertEqual((entry["endTime"], entry["description"]), ("10:30", "Did X and Y."))

    def test_title_is_sent_as_task(self) -> None:
        self.write_rows(row("a", "09:00", "10:00", title="Calendar & Telemetry"), row("b", "11:00", "12:00", title=""))
        self.run_main()
        tasks = {e["startTime"]: e.get("task") for e in self.state.entries.values()}
        self.assertEqual(tasks, {"09:00": "Calendar & Telemetry", "11:00": None})

        self.write_rows(row("a", "09:00", "10:00", title="Token Telemetry"), row("b", "11:00", "12:00", title=""))
        code, summary, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual((summary["updated"], summary["unchanged"]), (["a"], ["b"]))
        self.assertIn("Token Telemetry", [e.get("task") for e in self.state.entries.values()])

    def test_block_pushed_without_task_gets_one(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"))
        self.run_main()
        record = json.loads(self.state_file.read_text())
        del record["pushed"]["task"]  # state written before task was sent
        self.state_file.write_text(json.dumps(record) + "\n")
        code, summary, _ = self.run_main()
        self.assertEqual((code, summary["updated"]), (0, ["a"]))

    def test_emptied_task_is_cleared(self) -> None:
        self.write_rows(row("a", "09:00", "10:00", title="Push"))
        self.run_main()
        self.write_rows(row("a", "09:00", "10:00", title=""))
        code, summary, _ = self.run_main()
        self.assertEqual((code, summary["updated"]), (0, ["a"]))
        (entry,) = self.state.entries.values()
        self.assertIsNone(entry["task"])
        code, summary, _ = self.run_main()
        self.assertEqual((code, summary["unchanged"]), (0, ["a"]))

    def test_task_matches_heading_label(self) -> None:
        tagged = row("a", "09:00", "10:00", title="Push")
        tagged["sources"] = {"repos": ["timesheet", "devops"]}
        self.assertEqual(push_entries.desired_fields(tagged)["task"], "[devops, timesheet] Push")
        tagged["title"] = ""
        self.assertEqual(push_entries.desired_fields(tagged)["task"], "[devops, timesheet]")

    def test_task_is_cut_to_app_limit(self) -> None:
        fields = push_entries.desired_fields(row("a", "09:00", "10:00", title="x" * 200))
        self.assertEqual(len(fields["task"]), push_entries.TASK_MAX_CHARS)

    def test_dry_run_writes_nothing(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"))
        code, summary, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(summary["created"], ["a"])
        self.assertEqual(self.state.entries, {})
        self.assertEqual(self.state.calls, ["list_my_entries"])
        self.assertFalse(self.state_file.exists())

    def test_manual_entry_with_same_range_is_skipped(self) -> None:
        self.state.add(DATE, "09:00", "10:00")
        self.write_rows(row("a", "09:00", "10:00"))
        code, summary, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(summary["skipped"], ["a"])
        self.assertEqual(len(self.state.entries), 1)
        self.assertIn("not pushed by this tool", err)

    def test_overlap_fails_that_block_only(self) -> None:
        self.state.add(DATE, "09:30", "10:30")
        self.write_rows(row("a", "09:00", "10:00"), row("b", "11:00", "12:00"))
        code, summary, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(summary["created"], ["b"])
        self.assertEqual([f["block_id"] for f in summary["failed"]], ["a"])
        self.assertIn("overlaps", err)

    def test_orphaned_and_missing_entries_are_reported(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"), row("b", "11:00", "12:00"))
        self.run_main()
        id_b = json.loads(self.state_file.read_text().splitlines()[1])["entry_id"]
        del self.state.entries[id_b]  # deleted by hand in the app
        self.write_rows(row("b", "11:00", "12:00"))  # block a merged away locally
        code, summary, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(summary["orphaned"], ["a"])
        self.assertEqual(summary["missing"], ["b"])
        self.assertEqual(len(self.state.entries), 1)
        self.assertIn("edit or delete it in the Timesheet app", err)

    def test_partial_run_pushes_nothing(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"))
        blocks = self.dir / "blocks.json"
        blocks.write_text(json.dumps({"date": DATE, "failed_sources": ["calendar"], "blocks": []}))
        code, summary, err = self.run_main("--blocks", str(blocks))
        self.assertEqual(code, 0)
        self.assertTrue(summary["partial"])
        self.assertEqual(self.state.calls, [])
        self.assertIn("nothing pushed", err)

    def test_rejected_token(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"))
        with patch.dict(os.environ, {"GRADION_PAT": "pat_wrong"}):
            code, summary, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertIn("Token rejected (invalid_token)", err)

    def test_missing_token(self) -> None:
        self.write_rows(row("a", "09:00", "10:00"))
        with patch.dict(os.environ, {"GRADION_PAT": ""}), patch.object(push_entries, "find_token", return_value=None):
            code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("No personal API token", err)

    def test_list_follows_pages(self) -> None:
        for hour in range(12):
            for minute in range(0, 60, 10):
                self.state.add(DATE, f"{hour:02d}:{minute:02d}", f"{hour:02d}:{minute + 5:02d}")
        client = push_entries.TimesheetClient(self.base, TOKEN)
        self.assertEqual(len(push_entries.list_remote(client, DATE)), 72)


if __name__ == "__main__":
    unittest.main()
