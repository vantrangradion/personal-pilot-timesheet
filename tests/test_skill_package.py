#!/usr/bin/env python3
"""
Tests for the timesheet-skill/ package (US-4.1): SKILL.md metadata, the
[Script]/[AI] tagging of workflow.md, and the run.sh wrapper; and the daily
token budget declared in workflow.md (US-4.2).
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.prepare_payload import TOKEN_BUDGET, estimate_tokens

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "timesheet-skill"
DATE = "2026-10-06"

# Words that mark deterministic work, which must never be tagged [AI].
DETERMINISTIC_WORDS = ("fetch", "harvest", "parse", "merge", "save", "write", "render", "upsert", "prune", "slice")


def frontmatter(text: str) -> dict:
    """Parse the flat `key: value` YAML frontmatter of a SKILL.md."""
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return {}
    return dict(line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line)


def workflow_steps() -> list:
    """Return the text of each numbered step in workflow.md, continuation lines included."""
    text = (SKILL_DIR / "workflow.md").read_text(encoding="utf-8")
    body = text.split("## Steps", 1)[1].split("\n## ", 1)[0]
    return [s.strip() for s in re.split(r"^\d+\. ", body, flags=re.M)[1:]]


class TestSkillMetadata(unittest.TestCase):
    """SKILL.md registers the skill for both runners (Scenario 1)."""

    def setUp(self) -> None:
        self.text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.meta = frontmatter(self.text)

    def test_name_matches_folder(self) -> None:
        self.assertEqual(self.meta.get("name"), SKILL_DIR.name)

    def test_description_has_triggers(self) -> None:
        description = self.meta.get("description", "")
        self.assertLessEqual(len(description), 1024)
        self.assertIn("log today's work", description)
        self.assertIn("once per day", description)

    def test_prerequisites_declared(self) -> None:
        for tool in ("git", "gh", "python3"):
            self.assertRegex(self.meta.get("compatibility", ""), rf"\b{tool}\b")

    def test_discovery_locations_documented_and_linked(self) -> None:
        for location in (".claude/skills/", ".agents/skills/", "~/.claude/skills/", "~/.gemini/config/skills/"):
            self.assertIn(location, self.text)
        for link in (".claude/skills", ".agents/skills"):
            path = REPO_ROOT / link / SKILL_DIR.name
            self.assertTrue(path.is_symlink(), path)
            self.assertEqual(path.resolve(), SKILL_DIR)


class TestWorkflowTags(unittest.TestCase):
    """Every workflow.md step is tagged, and only judgment is [AI] (Scenario 2)."""

    def test_every_step_tagged(self) -> None:
        steps = workflow_steps()
        self.assertGreaterEqual(len(steps), 5)
        for step in steps:
            self.assertRegex(step, r"^\*\*\[(Script|AI)\]\*\* ")

    def test_ai_steps_are_judgment_only(self) -> None:
        ai_steps = [s for s in workflow_steps() if s.startswith("**[AI]**")]
        self.assertEqual(len(ai_steps), 1)
        first_line = ai_steps[0].splitlines()[0].lower()
        for word in DETERMINISTIC_WORDS:
            self.assertNotIn(word, first_line)
        self.assertNotRegex(ai_steps[0], r"run\.sh|scripts/\w+\.py")

    def test_referenced_scripts_exist(self) -> None:
        text = (SKILL_DIR / "workflow.md").read_text(encoding="utf-8")
        for script in set(re.findall(r"scripts/\w+\.py", text)):
            self.assertTrue((REPO_ROOT / script).is_file(), script)
        self.assertTrue((SKILL_DIR / "prompts" / "synthesize_blocks.md").is_file())


DAILY_BUDGET = 1200
PROMPT_BUDGET = 300
REPLY_TOKENS_PER_BLOCK = 40


class TestTokenBudget(unittest.TestCase):
    """workflow.md declares the daily ceiling and the input minimization rules (US-4.2)."""

    def setUp(self) -> None:
        text = (SKILL_DIR / "workflow.md").read_text(encoding="utf-8")
        self.budget = text.split("## Token budget", 1)[1].split("\n## ", 1)[0]

    def test_daily_ceiling_declared(self) -> None:
        self.assertIn(f"{DAILY_BUDGET:,} tokens per day", self.budget)
        self.assertIn(f"< {TOKEN_BUDGET}", self.budget)

    def test_input_minimization_rules(self) -> None:
        rules = " ".join(self.budget.lower().split())
        for banned in ("full `git log`", "diffs", "raw calendar json", "attendees", "earlier days"):
            self.assertIn(banned, rules)

    def test_one_run_fits_daily_ceiling(self) -> None:
        prompt = (SKILL_DIR / "prompts" / "synthesize_blocks.md").read_text(encoding="utf-8")
        self.assertLessEqual(estimate_tokens(prompt), PROMPT_BUDGET)
        # A busy day of six blocks: prompt + the payload cap + the reply.
        self.assertLessEqual(PROMPT_BUDGET + TOKEN_BUDGET + 6 * REPLY_TOKENS_PER_BLOCK, DAILY_BUDGET)


STUB_OK = """import json, sys
print(json.dumps({payload}))
"""
STUB_FAIL = """import sys
sys.stderr.write("Error: OAuth credentials must be set up.\\n")
sys.exit(1)
"""


class TestRunScript(unittest.TestCase):
    """run.sh runs the [Script] steps around the AI step, with stub harvesters."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copytree(SKILL_DIR, self.root / SKILL_DIR.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("prepare_payload", "merge_descriptions", "save_entry", "render_timesheet", "push_entries"):
            shutil.copy(REPO_ROOT / "scripts" / f"{name}.py", scripts)
        commits = [{
            "repo": "personal-pilot-timesheet",
            "hash": "8f3b21a",
            "timestamp": f"{DATE}T10:00:00+07:00",
            "message": "feat: add harvester",
            "files_changed_count": 2,
        }]
        (scripts / "fetch_commits.py").write_text(STUB_OK.format(payload=repr(commits)))
        (scripts / "fetch_prs.py").write_text(STUB_OK.format(payload="[]"))
        (scripts / "fetch_calendar.py").write_text(STUB_FAIL)
        # Run with the current interpreter so run.sh's python3 fallback is not needed.
        venv_bin = self.root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(sys.executable)
        self.run_dir = self.root / "tmp" / "timesheet" / DATE

    def run_sh(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "TIMESHEET_RUN_ROOT"}
        return subprocess.run(
            [str(self.root / SKILL_DIR.name / "run.sh"), *args],
            input=stdin, capture_output=True, text=True, env=env, cwd=tempfile.gettempdir(),
        )

    def test_prepare_then_save_partial_run(self) -> None:
        prepared = self.run_sh("prepare", DATE)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        payload = json.loads(prepared.stdout)
        self.assertEqual(len(payload), 1)
        block_id = payload[0]["id"]
        self.assertIn("fetch_calendar.py failed", prepared.stderr)
        blocks = json.loads((self.run_dir / "blocks.json").read_text())
        self.assertEqual(blocks["failed_sources"], ["calendar"])

        ai = json.dumps({block_id: {"title": "Harvester", "description": "Built the harvester."}})
        saved = self.run_sh("save", DATE, stdin=ai)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        summary = json.loads(saved.stdout)
        self.assertTrue(summary["partial"])
        self.assertEqual(summary["inserted"], [block_id])
        self.assertEqual(json.loads((self.run_dir / "ai.json").read_text()), json.loads(ai))
        self.assertIn("| [personal-pilot-timesheet] Harvester", (self.root / "timesheet.md").read_text())

        again = self.run_sh("save", DATE, stdin=ai)
        self.assertEqual(json.loads(again.stdout)["unchanged"], [block_id])
        rows = (self.root / "timesheet.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1)

        # A re-run with no new activity sends nothing to the AI and keeps the saved text.
        rerun = self.run_sh("prepare", DATE)
        self.assertEqual(json.loads(rerun.stdout), [])
        kept = self.run_sh("save", DATE, stdin="{}")
        self.assertEqual(json.loads(kept.stdout)["unchanged"], [block_id])
        self.assertEqual(len(json.loads(self.run_sh("prepare", DATE, "--refresh").stdout)), 1)

    def test_push_skips_partial_run(self) -> None:
        prepared = self.run_sh("prepare", DATE)
        block_id = json.loads(prepared.stdout)[0]["id"]
        self.run_sh("save", DATE, stdin=json.dumps({block_id: {"title": "T", "description": "D."}}))
        # The base URL is unreachable: a partial run must return before any request.
        pushed = self.run_sh("push", DATE, "--base-url", "http://127.0.0.1:9")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(json.loads(pushed.stdout)["failed_sources"], ["calendar"])
        self.assertIn("nothing pushed", pushed.stderr)
        self.assertFalse((self.root / "timesheet-remote.jsonl").exists())

    def test_save_rejects_non_json(self) -> None:
        self.assertEqual(self.run_sh("prepare", DATE).returncode, 0)
        result = self.run_sh("save", DATE, stdin="Sure! Here are the descriptions.")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "timesheet.jsonl").exists())

    def test_save_requires_prepare(self) -> None:
        result = self.run_sh("save", DATE, stdin="{}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("prepare", result.stderr)

    def test_unknown_stage(self) -> None:
        self.assertEqual(self.run_sh("bogus").returncode, 2)
        self.assertEqual(self.run_sh("prepare", DATE, "--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
