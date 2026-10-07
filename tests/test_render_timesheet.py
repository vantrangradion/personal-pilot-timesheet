#!/usr/bin/env python3
"""
Unit tests for scripts/render_timesheet.py (US-3.2).
"""

import difflib
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch

from scripts import render_timesheet, save_entry
from scripts.render_timesheet import render

DATE = "2026-10-06"


def row(
    block_id: str,
    date: str = DATE,
    start: str = "10:00",
    end: str = "12:30",
    title: Optional[str] = "Core Engineering",
    description: str = "Did X.",
    calendar_event: Optional[str] = None,
    commits: Optional[List[str]] = None,
    prs: Optional[List[int]] = None,
    repos: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a timesheet.jsonl row."""
    return {
        "block_id": block_id,
        "kind": "calendar" if calendar_event else "focus",
        "date": date,
        "start_time": start,
        "end_time": end,
        "title": title,
        "description": description,
        "sources": {"calendar_event": calendar_event, "commits": commits or [], "prs": prs or [], "repos": repos or []},
        "created_at": f"{date}T16:35:00+07:00",
    }


class TestRender(unittest.TestCase):
    """Scenario 1: the Markdown matches the "Edit entry" layout, grouped by date."""

    def test_matches_spec_example(self) -> None:
        rows = [
            row(
                f"{DATE}_focus_1000",
                description="Git commit harvester and CLI test suite implementation. PRs: #1, #2",
                commits=["8f3b21a", "4d5e6f7"],
                prs=[1, 2],
            ),
            row(
                "event_1",
                start="09:30",
                end="10:00",
                title="Standup & Planning",
                description="Weekly sprint kickoff and timesheet architecture review.",
                calendar_event="Team Standup",
            ),
        ]
        self.assertEqual(render(rows), (
            "# Timesheet Log\n"
            "\n"
            "## 2026-10-06 (Tuesday)\n"
            "\n"
            "### 09:30 - 10:00 | Standup & Planning\n"
            "- **Date:** 2026-10-06\n"
            "- **Start time:** 09:30\n"
            "- **End time:** 10:00\n"
            "- **Description:** Weekly sprint kickoff and timesheet architecture review.\n"
            "- **Sources:** Calendar: \"Team Standup\"\n"
            "\n"
            "### 10:00 - 12:30 | Core Engineering\n"
            "- **Date:** 2026-10-06\n"
            "- **Start time:** 10:00\n"
            "- **End time:** 12:30\n"
            "- **Description:** Git commit harvester and CLI test suite implementation. PRs: #1, #2\n"
            "- **Sources:** Commits: `8f3b21a`, `4d5e6f7`; PRs: #1, #2\n"
        ))

    def test_dates_grouped_chronologically(self) -> None:
        rows = [row("b", date="2026-10-07"), row("a", date=DATE), row("c", date="2026-10-07", start="08:00")]
        text = render(rows)
        self.assertLess(text.index("## 2026-10-06 (Tuesday)"), text.index("## 2026-10-07 (Wednesday)"))
        self.assertEqual(text.count("## 2026-10-07"), 1)
        self.assertLess(text.index("### 08:00"), text.index("### 10:00 - 12:30", text.index("## 2026-10-07")))

    def test_heading_tags_the_project(self) -> None:
        self.assertIn("### 10:00 - 12:30 | [pilot] Core Engineering\n", render([row("a", repos=["pilot"])]))
        self.assertIn(
            "### 10:00 - 12:30 | [devops, pilot] Core Engineering\n",
            render([row("a", repos=["pilot", "devops"])]),
        )
        self.assertIn("### 10:00 - 12:30 | [pilot]\n", render([row("a", title=None, repos=["pilot"])]))

    def test_heading_without_repos_has_no_tag(self) -> None:
        r = row("a")
        del r["sources"]["repos"]  # rows logged before `repos` existed
        self.assertIn("### 10:00 - 12:30 | Core Engineering\n", render([r]))

    def test_meeting_with_commits_lists_all_sources(self) -> None:
        text = render([row("e", calendar_event="Review", commits=["a1"], prs=[3])])
        self.assertIn('- **Sources:** Calendar: "Review"; Commits: `a1`; PRs: #3\n', text)

    def test_missing_title_and_sources(self) -> None:
        r = row("a", title=None)
        r["sources"] = {}
        text = render([r])
        self.assertIn("### 10:00 - 12:30\n", text)
        self.assertIn("- **Sources:** None\n", text)

    def test_multiline_description_stays_on_one_line(self) -> None:
        text = render([row("a", description="Line one.\n\n- Line two.")])
        self.assertIn("- **Description:** Line one. - Line two.\n", text)

    def test_empty_timesheet(self) -> None:
        self.assertEqual(render([]), "# Timesheet Log\n")


class TestCleanDiffs(unittest.TestCase):
    """Scenario 2: unchanged dates and blocks render identically."""

    def test_row_order_does_not_change_output(self) -> None:
        rows = [row("a", start="09:00"), row("b", date="2026-10-07"), row("c", start="13:00")]
        self.assertEqual(render(rows), render(list(reversed(rows))))

    def test_appended_date_only_adds_lines(self) -> None:
        before = render([row("a")])
        after = render([row("a"), row("b", date="2026-10-07")])
        self.assertTrue(after.startswith(before))

    def test_updated_block_leaves_other_blocks_identical(self) -> None:
        old = [row("a", start="09:00", end="10:00"), row("b", start="11:00", end="12:00"), row("c", date="2026-10-07")]
        new = [old[0], {**old[1], "end_time": "12:30", "description": "Did more."}, old[2]]
        diff = difflib.unified_diff(render(old).splitlines(), render(new).splitlines(), lineterm="", n=0)
        changed = [line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        self.assertEqual(sorted(changed), sorted([
            "-### 11:00 - 12:00 | Core Engineering",
            "-- **End time:** 12:00",
            "-- **Description:** Did X.",
            "+### 11:00 - 12:30 | Core Engineering",
            "+- **End time:** 12:30",
            "+- **Description:** Did more.",
        ]))


class TestMain(unittest.TestCase):
    """The standalone CLI and the automatic render from save_entry.py."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.timesheet = self.tmp / "timesheet.jsonl"
        self.markdown = self.tmp / "timesheet.md"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, module: Any, argv: List[str], stdin: str = "") -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), patch("sys.stdin", io.StringIO(stdin)):
            code = module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _write_rows(self, rows: List[Dict[str, Any]]) -> None:
        self.timesheet.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_renders_next_to_timesheet(self) -> None:
        self._write_rows([row("a")])
        code, _, _ = self._run(render_timesheet, ["--timesheet", str(self.timesheet)])
        self.assertEqual(code, 0)
        self.assertEqual(self.markdown.read_text(), render([row("a")]))

    def test_stdout_output(self) -> None:
        self._write_rows([row("a")])
        code, out, _ = self._run(render_timesheet, ["--timesheet", str(self.timesheet), "--out", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(out, render([row("a")]))
        self.assertFalse(self.markdown.exists())

    def test_unchanged_view_not_rewritten(self) -> None:
        self._write_rows([row("a")])
        self._run(render_timesheet, ["--timesheet", str(self.timesheet)])
        mtime = self.markdown.stat().st_mtime_ns
        code, _, err = self._run(render_timesheet, ["--timesheet", str(self.timesheet), "-v"])
        self.assertEqual(code, 0)
        self.assertEqual(self.markdown.stat().st_mtime_ns, mtime)
        self.assertIn("unchanged", err)

    def test_corrupt_timesheet_aborts(self) -> None:
        self.timesheet.write_text("{not json\n")
        code, _, err = self._run(render_timesheet, ["--timesheet", str(self.timesheet)])
        self.assertEqual(code, 1)
        self.assertIn("line 1", err)
        self.assertFalse(self.markdown.exists())

    def test_save_entry_renders_markdown(self) -> None:
        blocks = self.tmp / "blocks.json"
        blocks.write_text(json.dumps({"date": DATE, "failed_sources": [], "blocks": [{"block_id": "a"}]}))
        entry = {k: v for k, v in row("a", commits=["a1"]).items() if k != "created_at"}
        argv = ["--blocks", str(blocks), "--timesheet", str(self.timesheet)]
        self.assertEqual(self._run(save_entry, argv, json.dumps([entry]))[0], 0)
        self.assertEqual(self.markdown.read_text(), render([row("a", commits=["a1"])]))

        entry["description"] = "Did more."
        custom = self.tmp / "view.md"
        self.assertEqual(self._run(save_entry, argv + ["--markdown", str(custom)], json.dumps([entry]))[0], 0)
        self.assertIn("- **Description:** Did more.\n", custom.read_text())


if __name__ == "__main__":
    unittest.main()
