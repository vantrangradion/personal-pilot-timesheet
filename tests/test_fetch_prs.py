#!/usr/bin/env python3
"""
Unit tests for scripts/fetch_prs.py (US-1.2).

A fake `gh` executable is placed first on PATH so the script runs real
subprocesses without needing network access or GitHub authentication.
"""

from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Dict, List
import unittest
from unittest.mock import patch

from scripts.fetch_prs import derive_status, fetch_prs, GhError, main, normalize_pr

FAKE_GH = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["auth", "status"]:
    if os.environ.get("FAKE_GH_AUTH") == "fail":
        sys.stderr.write("You are not logged into any GitHub hosts. To log in, run: gh auth login\\n")
        sys.exit(1)
    sys.exit(0)
if args[:2] == ["search", "prs"]:
    key = "FAKE_GH_AUTHORED" if "--author=@me" in args else "FAKE_GH_REVIEWED"
    sys.stdout.write(open(os.environ[key]).read())
    sys.exit(0)
sys.exit(2)
"""


def _local_iso(year: int, month: int, day: int, hour: int, minute: int = 0) -> str:
    """Build a UTC 'Z' timestamp for a given local wall-clock time."""
    local_dt = datetime(year, month, day, hour, minute).astimezone()
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(repo: str, number: int, state: str, updated_at: str, title: str = "PR") -> Dict[str, Any]:
    """Build a raw gh search record."""
    return {
        "repository": {"name": repo, "nameWithOwner": f"vantran/{repo}"},
        "number": number,
        "title": title,
        "state": state,
        "url": f"https://github.com/vantran/{repo}/pull/{number}",
        "updatedAt": updated_at,
    }


class TestFetchPrs(unittest.TestCase):
    """Test suite for the GitHub PR harvester."""

    def setUp(self) -> None:
        """Install a fake gh on PATH backed by fixture files."""
        self.test_dir = Path(tempfile.mkdtemp())
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir()
        gh_path = bin_dir / "gh"
        gh_path.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)

        self.log_path = self.test_dir / "gh.log"
        self.authored_path = self.test_dir / "authored.json"
        self.reviewed_path = self.test_dir / "reviewed.json"
        self._set_fixtures([], [])

        self.env_patch = patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_LOG": str(self.log_path),
            "FAKE_GH_AUTHORED": str(self.authored_path),
            "FAKE_GH_REVIEWED": str(self.reviewed_path),
        })
        self.env_patch.start()

    def tearDown(self) -> None:
        """Restore environment and remove temp files."""
        self.env_patch.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _set_fixtures(self, authored: List[Dict[str, Any]], reviewed: List[Dict[str, Any]]) -> None:
        self.authored_path.write_text(json.dumps(authored), encoding="utf-8")
        self.reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")

    def _gh_calls(self) -> List[List[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def test_derive_status(self) -> None:
        self.assertEqual(derive_status("open", authored=True), "opened")
        self.assertEqual(derive_status("closed", authored=True), "closed")
        self.assertEqual(derive_status("merged", authored=True), "merged")
        self.assertEqual(derive_status("open", authored=False), "reviewed")
        self.assertEqual(derive_status("merged", authored=False), "merged")

    def test_normalize_pr_malformed(self) -> None:
        self.assertIsNone(normalize_pr({"number": 1}, authored=True))

    def test_scenario_1_authored_and_reviewed_prs(self) -> None:
        """Scenario 1: queries both roles with field projection and normalizes output."""
        authored = [
            _record("personal-pilot-timesheet", 14, "open", _local_iso(2026, 10, 6, 14, 10), "feat: calendar"),
            _record("personal-pilot-timesheet", 12, "merged", _local_iso(2026, 10, 6, 9, 0)),
            # Outside target local date (window overlap) -> filtered out
            _record("personal-pilot-timesheet", 11, "open", _local_iso(2026, 10, 5, 23, 30)),
        ]
        reviewed = [
            _record("devops", 7, "open", _local_iso(2026, 10, 6, 11, 0)),
            # Also authored -> deduplicated, authored status wins
            _record("personal-pilot-timesheet", 14, "open", _local_iso(2026, 10, 6, 14, 10)),
        ]
        self._set_fixtures(authored, reviewed)

        prs = fetch_prs(date(2026, 10, 6))

        self.assertEqual([(p["repo"], p["number"], p["status"]) for p in prs], [
            ("personal-pilot-timesheet", 12, "merged"),
            ("devops", 7, "reviewed"),
            ("personal-pilot-timesheet", 14, "opened"),
        ])
        first = prs[2]
        self.assertEqual(set(first), {"repo", "number", "title", "status", "url", "updated_at"})
        self.assertEqual(first["title"], "feat: calendar")
        self.assertEqual(first["url"], "https://github.com/vantran/personal-pilot-timesheet/pull/14")
        self.assertEqual(
            first["updated_at"],
            datetime(2026, 10, 6, 14, 10).astimezone().isoformat(timespec="seconds"),
        )

        search_calls = [c for c in self._gh_calls() if c[:2] == ["search", "prs"]]
        self.assertEqual(len(search_calls), 2)
        self.assertIn("--author=@me", search_calls[0])
        self.assertIn("--reviewed-by=@me", search_calls[1])
        for call in search_calls:
            self.assertIn("--json=repository,number,title,state,url,updatedAt", call)
            self.assertIn("--updated=2026-10-05..2026-10-07", call)

    def test_no_prs_outputs_empty_array(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            exit_code = main(["--date", "2026-10-06"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), [])

    def test_scenario_2_unauthenticated(self) -> None:
        """Scenario 2: unauthenticated gh exits non-zero with a gh auth login hint."""
        stderr = io.StringIO()
        with patch.dict(os.environ, {"FAKE_GH_AUTH": "fail"}), patch("sys.stderr", stderr):
            exit_code = main(["--date", "2026-10-06"])
        self.assertNotEqual(exit_code, 0)
        self.assertIn("gh auth login", stderr.getvalue())
        self.assertFalse(any(c[:2] == ["search", "prs"] for c in self._gh_calls()))

    def test_gh_not_installed(self) -> None:
        with patch.dict(os.environ, {"PATH": str(self.test_dir / "empty")}):
            with self.assertRaises(GhError) as ctx:
                fetch_prs(date(2026, 10, 6))
        self.assertIn("cli.github.com", str(ctx.exception))

    def test_cli_invalid_date_error(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            exit_code = main(["--date", "2026-13-99"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Invalid date format", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
