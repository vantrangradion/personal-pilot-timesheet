#!/usr/bin/env python3
"""
Unit tests for scripts/install_telemetry_job.sh (US-5.1).

Only `--print` is exercised: it renders the launchd plist without touching
~/Library/LaunchAgents or launchctl.
"""

from pathlib import Path
import plistlib
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install_telemetry_job.sh"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=10)


class TestInstallTelemetryJob(unittest.TestCase):
    def test_print_renders_daily_job_for_this_repo(self) -> None:
        result = run("--print")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = plistlib.loads(result.stdout.encode())
        self.assertEqual(plist["Label"], "local.personal-pilot-timesheet.telemetry")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 23, "Minute": 30})
        self.assertEqual(plist["WorkingDirectory"], str(ROOT))
        self.assertEqual(
            plist["ProgramArguments"],
            [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "track_tokens.py"), "-v"],
        )
        # The log stays outside ~/Documents and the repo.
        self.assertTrue(plist["StandardOutPath"].endswith("Library/Logs/personal-pilot-timesheet-telemetry.log"))
        self.assertEqual(plist["StandardOutPath"], plist["StandardErrorPath"])

    def test_custom_time_drops_leading_zeros(self) -> None:
        plist = plistlib.loads(run("--print", "07:05").stdout.encode())
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 7, "Minute": 5})

    def test_invalid_arguments_exit_2(self) -> None:
        for args in (("--print", "24:00"), ("--print", "7:5"), ("--bogus",), ("--uninstall", "extra")):
            with self.subTest(args=args):
                result = run(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
