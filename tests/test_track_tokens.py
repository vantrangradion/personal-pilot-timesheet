#!/usr/bin/env python3
"""
Unit tests for scripts/track_tokens.py (US-5.1).

Transcripts are written in each runner's real on-disk shape. Timestamps are
built from local times so date attribution holds regardless of the machine's TZ.
"""

import csv
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional
import unittest
import unittest.mock
from unittest.mock import patch

from scripts import track_tokens
from scripts.track_tokens import CountUnavailable, TokenCounter
from scripts.prepare_payload import estimate_tokens

PROMPT = "Summarize each block as JSON." * 10
PAYLOAD = '[{"id":"2026-10-06_focus_0900","time":"09:00-11:00","commits":{"repo":["feat: add parser"]}}]'
REPLY = '{"2026-10-06_focus_0900":{"title":"Parser","description":"Built the commit parser."}}'
SAVE_COMMAND = f"timesheet-skill/run.sh save 2026-10-06 <<'EOF'\n{REPLY}\nEOF"
RUN_TOKENS = estimate_tokens(PAYLOAD) + estimate_tokens(PROMPT) + estimate_tokens(REPLY)


def ts(hh: int, mm: int = 0, day: int = 6, utc_z: bool = True) -> str:
    """A local wall-clock time on 2026-10-<day>, serialized in UTC like the runners do."""
    local = datetime(2026, 10, day, hh, mm).astimezone()
    if not utc_z:
        return local.isoformat()
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ClaudeTranscript:
    """Builds a Claude Code session JSONL: one line per content block, usage repeated per message."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.lines: List[Dict[str, Any]] = []
        self.n = 0

    def assistant(self, when: str, usage: Dict[str, int], blocks: List[Dict[str, Any]]) -> None:
        self.n += 1
        for block in blocks or [{"type": "text", "text": "ok"}]:
            self.lines.append({
                "type": "assistant", "sessionId": self.session_id, "timestamp": when,
                "requestId": f"req_{self.n}",
                "message": {"id": f"msg_{self.n}", "role": "assistant", "model": "claude-sonnet-5-5",
                            "content": [block], "usage": usage},
            })

    def bash(self, when: str, command: str, output: str, usage: Optional[Dict[str, int]] = None) -> None:
        tool_id = f"toolu_{self.n + 1}"
        self.assistant(when, usage or usage_of(10, 5), [
            {"type": "text", "text": "Running."},
            {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}},
        ])
        self.lines.append({
            "type": "user", "sessionId": self.session_id, "timestamp": when,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": output},
            ]},
        })

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in self.lines), encoding="utf-8")
        return path


def usage_of(inp: int, out: int, read: int = 0, create: int = 0) -> Dict[str, int]:
    return {"input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": read, "cache_creation_input_tokens": create}


def antigravity_steps(commands: List[tuple], encode_args: bool = False) -> List[Dict[str, Any]]:
    """Build Antigravity steps: a PLANNER_RESPONSE per (time, command, output, in, out, cache) plus its output."""
    steps: List[Dict[str, Any]] = [{"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT",
                                    "created_at": commands[0][0], "content": "log today's work"}]
    for when, command, output, inp, out, cache in commands:
        arg = json.dumps(command) if encode_args else command
        steps.append({"step_index": len(steps), "type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": when,
                      "input_tokens": inp, "cache_read_tokens": cache, "output_tokens": out,
                      "tool_calls": [{"name": "run_command", "args": {"CommandLine": arg, "Cwd": "/repo"}}]})
        steps.append({"step_index": len(steps), "type": "GENERIC", "source": "MODEL", "created_at": when,
                      "content": f"Created At: {when}\n\nThe command exited with code 0.\nOutput:\n{output}"})
    return steps


class TrackTokensTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.claude_dir = self.tmp / "claude" / "projects"
        self.brain_dir = self.tmp / "gemini" / "brain"
        self.csv = self.tmp / "telemetry.csv"
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text(PROMPT, encoding="utf-8")

    def write_antigravity(self, session_id: str, steps: List[Dict[str, Any]], name: str = "transcript_full.jsonl") -> Path:
        path = self.brain_dir / session_id / ".system_generated" / "logs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(s) + "\n" for s in steps), encoding="utf-8")
        return path

    def run_main(self, *extra: str, estimate: bool = True) -> tuple:
        argv = ["--csv", str(self.csv), "--prompt", str(self.prompt),
                "--claude-dir", str(self.claude_dir), "--antigravity-dir", str(self.brain_dir), *extra]
        if estimate:
            argv.append("--estimate")
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = track_tokens.main(argv)
        return code, json.loads(out.getvalue()) if code == 0 else None, err.getvalue()

    def csv_rows(self) -> List[Dict[str, str]]:
        with self.csv.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def skill_session(self, session_id: str = "s1", day: int = 6) -> ClaudeTranscript:
        t = ClaudeTranscript(session_id)
        t.assistant(ts(9, 0, day), usage_of(3, 100, read=1000, create=500), [])
        t.bash(ts(9, 1, day), "timesheet-skill/run.sh prepare 2026-10-06", PAYLOAD, usage_of(2, 50, read=1500))
        t.bash(ts(9, 2, day), SAVE_COMMAND, '{"inserted": ["2026-10-06_focus_0900"]}', usage_of(1, 80, read=1600))
        return t


class TestClaudeTranscripts(TrackTokensTestCase):
    def test_extracts_usage_once_per_message(self) -> None:
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        (row,) = out["recorded"]
        # Each message spans two lines (text + tool_use) but counts once.
        self.assertEqual(row["input_tokens"], 3 + 2 + 1)
        self.assertEqual(row["output_tokens"], 100 + 50 + 80)
        self.assertEqual(row["cache_tokens"], 1000 + 500 + 1500 + 1600)
        self.assertEqual(row["total_tokens"], 6 + 230 + 4600)
        self.assertEqual((row["date"], row["runner"], row["session_id"]), ("2026-10-06", "claude", "s1"))
        self.assertEqual(row["timestamp"], ts(9, 2, utc_z=False))

    def test_appends_row_with_required_columns(self) -> None:
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        self.run_main()
        with self.csv.open(encoding="utf-8") as f:
            header = next(csv.reader(f))
        for column in ("date", "runner", "session_id", "input_tokens", "output_tokens",
                       "cache_tokens", "total_tokens", "timestamp", "ai_step_tokens", "ai_step_method"):
            self.assertIn(column, header)
        (row,) = self.csv_rows()
        self.assertEqual(row["total_tokens"], str(6 + 230 + 4600))

    def test_ai_step_counts_prompt_payload_and_reply_only(self) -> None:
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual(out["recorded"][0]["ai_step_tokens"], RUN_TOKENS)
        self.assertEqual(out["recorded"][0]["ai_step_method"], "estimated")
        self.assertLess(out["recorded"][0]["ai_step_tokens"], out["recorded"][0]["total_tokens"])

    def test_empty_payload_skips_prompt(self) -> None:
        t = ClaudeTranscript("s1")
        t.bash(ts(9), "timesheet-skill/run.sh prepare 2026-10-06", "[]")
        t.bash(ts(9, 1), "timesheet-skill/run.sh save 2026-10-06 <<'EOF'\n{}\nEOF", "{}")
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual(out["recorded"][0]["ai_step_tokens"], estimate_tokens("[]") + estimate_tokens("{}"))

    def test_payload_ignores_warnings_and_other_output(self) -> None:
        t = ClaudeTranscript("s1")
        output = f"Warning: fetch_calendar.py failed (tmp/x.err):\n  [auth] expired\n{PAYLOAD}\nexit 0"
        t.bash(ts(9), "ls; .claude/skills/timesheet-skill/run.sh prepare; echo \"exit $?\"", output)
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual(out["recorded"][0]["ai_step_tokens"], estimate_tokens(PAYLOAD) + estimate_tokens(PROMPT))

    def test_inline_reply_piped_to_save(self) -> None:
        t = ClaudeTranscript("s1")
        t.bash(ts(9), f"echo '{REPLY}' | timesheet-skill/run.sh save 2026-10-06", "{}")
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual(out["recorded"][0]["ai_step_tokens"], estimate_tokens(REPLY))

    def test_mentions_of_run_sh_are_not_runs(self) -> None:
        t = ClaudeTranscript("s1")
        t.bash(ts(9), 'grep -l "run.sh prepare\\|run.sh save" *.jsonl', "a.jsonl")
        t.bash(ts(9, 1), "python - <<'PY'\nprint('x')\ntimesheet-skill/run.sh prepare\nPY", "x")
        t.bash(ts(9, 2), "echo run.sh save", "run.sh save")
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual(out["recorded"], [])
        self.assertFalse(self.csv_rows())

    def test_auto_detect_skips_sessions_without_runs_unless_all_sessions(self) -> None:
        self.skill_session("skill").write(self.claude_dir / "proj" / "skill.jsonl")
        other = ClaudeTranscript("other")
        other.assistant(ts(10), usage_of(5, 5), [])
        other.write(self.claude_dir / "proj" / "other.jsonl")

        _, out, _ = self.run_main()
        self.assertEqual([r["session_id"] for r in out["recorded"]], ["skill"])
        _, out, _ = self.run_main("--all-sessions")
        self.assertEqual(sorted(r["session_id"] for r in out["recorded"]), ["other", "skill"])

    def test_only_dates_with_runs_are_recorded(self) -> None:
        t = self.skill_session("s1", day=7)
        early = ClaudeTranscript("s1")
        early.assistant(ts(22, 0, day=6), usage_of(7, 7), [])
        t.lines = early.lines + t.lines
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual([r["date"] for r in out["recorded"]], ["2026-10-07"])
        _, out, _ = self.run_main("--all-sessions")
        self.assertEqual([r["date"] for r in out["recorded"]], ["2026-10-06", "2026-10-07"])

    def test_explicit_path_is_recorded_without_runs(self) -> None:
        t = ClaudeTranscript("plain")
        t.assistant(ts(10), usage_of(5, 6, read=7), [])
        path = t.write(self.tmp / "elsewhere" / "plain.jsonl")
        _, out, _ = self.run_main(str(path))
        (row,) = out["recorded"]
        self.assertEqual((row["session_id"], row["total_tokens"], row["ai_step_tokens"]), ("plain", 18, 0))
        self.assertEqual(row["ai_step_method"], "")

    def test_missing_explicit_path_fails(self) -> None:
        code, _, err = self.run_main(str(self.tmp / "nope.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("Transcript not found", err)
        self.assertFalse(self.csv.exists())

    def test_corrupt_lines_are_skipped(self) -> None:
        path = self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        path.write_text(path.read_text() + "{not json\n", encoding="utf-8")
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["recorded"][0]["ai_step_tokens"], RUN_TOKENS)


def skill_steps(encode_args: bool = False) -> List[Dict[str, Any]]:
    """An Antigravity session with one prepare/save run and an unrelated command."""
    return antigravity_steps([
        (ts(14), "timesheet-skill/run.sh prepare 2026-10-06", PAYLOAD, 10000, 200, 0),
        (ts(14, 1), SAVE_COMMAND, '{"inserted": []}', 3000, 120, 9000),
        (ts(14, 2), "git status", "clean", 500, 30, 12000),
    ], encode_args)


class TestAntigravityTranscripts(TrackTokensTestCase):

    def test_extracts_prompt_candidate_and_cache_tokens(self) -> None:
        self.write_antigravity("ag-1", skill_steps())
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        (row,) = out["recorded"]
        self.assertEqual((row["runner"], row["session_id"], row["date"]), ("antigravity", "ag-1", "2026-10-06"))
        self.assertEqual(row["input_tokens"], 13500)
        self.assertEqual(row["output_tokens"], 350)
        self.assertEqual(row["cache_tokens"], 21000)
        self.assertEqual(row["total_tokens"], 34850)
        self.assertEqual(row["ai_step_tokens"], RUN_TOKENS)
        self.assertEqual(row["timestamp"], ts(14, 2, utc_z=False))

    def test_prefers_full_transcript_and_decodes_truncated_args(self) -> None:
        self.write_antigravity("ag-1", skill_steps(encode_args=True), name="transcript.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual(out["recorded"][0]["ai_step_tokens"], RUN_TOKENS)

        truncated = skill_steps()
        truncated[2]["content"] = "Output:\n[{\"id\":\"x\",\"time\":"  # cut off by the truncated log
        self.write_antigravity("ag-1", truncated, name="transcript.jsonl")
        self.write_antigravity("ag-1", skill_steps())
        _, out, _ = self.run_main()
        self.assertEqual(out["recorded"][0]["ai_step_tokens"], RUN_TOKENS)

    def test_explicit_session_directory(self) -> None:
        self.write_antigravity("ag-1", skill_steps())
        _, out, _ = self.run_main(str(self.brain_dir / "ag-1"))
        self.assertEqual([(r["runner"], r["session_id"]) for r in out["recorded"]], [("antigravity", "ag-1")])

    def test_runner_filter(self) -> None:
        self.write_antigravity("ag-1", skill_steps())
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main("--runner", "antigravity")
        self.assertEqual([r["runner"] for r in out["recorded"]], ["antigravity"])


class TestCsvAndDailyTotals(TrackTokensTestCase):
    def test_rerun_updates_rows_without_duplicates(self) -> None:
        t = self.skill_session()
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        self.run_main()
        first = self.csv.read_text()
        self.run_main()
        self.assertEqual(self.csv.read_text(), first)

        t.assistant(ts(9, 30), usage_of(4, 40), [])  # the session continued
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        self.run_main()
        (row,) = self.csv_rows()
        self.assertEqual(row["output_tokens"], str(230 + 40))

    def test_keeps_rows_of_sessions_no_longer_on_disk(self) -> None:
        self.csv.write_text(
            "date,runner,session_id,input_tokens,output_tokens,cache_tokens,total_tokens,ai_step_tokens,timestamp\n"
            "2026-10-05,antigravity,old,1,2,3,6,4,2026-10-05T10:00:00+07:00\n",  # written before ai_step_method
            encoding="utf-8",
        )
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        _, out, _ = self.run_main()
        self.assertEqual([r["session_id"] for r in self.csv_rows()], ["old", "s1"])
        self.assertEqual(self.csv_rows()[0]["ai_step_method"], "estimated")
        self.assertEqual([d["date"] for d in out["daily"]], ["2026-10-05", "2026-10-06"])

    def test_aggregates_runs_across_sessions_and_runners(self) -> None:
        self.skill_session("c1").write(self.claude_dir / "proj" / "c1.jsonl")
        self.skill_session("c2").write(self.claude_dir / "proj" / "c2.jsonl")
        self.write_antigravity("ag-1", skill_steps())
        _, out, _ = self.run_main()
        (day,) = out["daily"]
        self.assertEqual(day["date"], "2026-10-06")
        self.assertEqual(day["sessions"], 3)
        self.assertEqual(day["total_tokens"], 2 * 4836 + 34850)
        self.assertEqual(day["ai_step_tokens"], 3 * RUN_TOKENS)
        self.assertEqual(sum(r["total_tokens"] for r in out["recorded"]), day["total_tokens"])

    def test_flags_days_over_ai_budget(self) -> None:
        t = ClaudeTranscript("s1")
        big = json.dumps([{"id": f"b{i}", "time": "09:00-10:00", "event": "x" * 100} for i in range(40)],
                         separators=(",", ":"))
        t.bash(ts(9), "timesheet-skill/run.sh prepare", big)
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        self.skill_session("s2", day=7).write(self.claude_dir / "proj" / "s2.jsonl")
        _, out, err = self.run_main()
        flags = {d["date"]: d["over_budget"] for d in out["daily"]}
        self.assertEqual(flags, {"2026-10-06": True, "2026-10-07": False})
        self.assertIn("2026-10-06 [AI] step used", err)
        self.assertNotIn("2026-10-07", err)

    def test_date_filter(self) -> None:
        self.skill_session("s1", day=6).write(self.claude_dir / "proj" / "s1.jsonl")
        self.skill_session("s2", day=7).write(self.claude_dir / "proj" / "s2.jsonl")
        _, out, _ = self.run_main("--date", "2026-10-07")
        self.assertEqual([r["session_id"] for r in out["recorded"]], ["s2"])
        self.assertEqual([d["date"] for d in out["daily"]], ["2026-10-07"])
        self.assertEqual([r["session_id"] for r in self.csv_rows()], ["s2"])

    def test_malformed_csv_is_not_overwritten(self) -> None:
        self.csv.write_text("date,runner\n2026-10-06,claude\n", encoding="utf-8")
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("missing columns", err)
        self.assertEqual(self.csv.read_text(), "date,runner\n2026-10-06,claude\n")

    def test_no_transcripts_writes_header_only(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out, {"recorded": [], "daily": []})
        self.assertEqual(self.csv.read_text().count("\n"), 1)


def fake_count(model: str, text: str) -> int:
    """Stand-in tokenizer: one token per word plus 3 tokens of message framing."""
    return len(text.split()) + 3


def words(*texts: str) -> int:
    return sum(len(t.split()) for t in texts)


class TestTokenCounting(TrackTokensTestCase):
    """The [AI] step texts are counted with the runner's tokenizer API, falling back to the estimate."""

    def test_claude_counts_with_session_model_minus_framing(self) -> None:
        t = self.skill_session()
        t.bash(ts(9, 5), "timesheet-skill/run.sh prepare 2026-10-06", PAYLOAD)  # second run reuses the prompt count
        t.write(self.claude_dir / "proj" / "s1.jsonl")
        with patch.object(TokenCounter, "_request_claude", side_effect=fake_count, autospec=False) as request:
            _, out, err = self.run_main(estimate=False)
        (row,) = out["recorded"]
        self.assertEqual(row["ai_step_tokens"], words(PAYLOAD, PROMPT, REPLY, PAYLOAD, PROMPT))
        self.assertEqual(row["ai_step_method"], "counted")
        self.assertEqual(out["daily"][0]["ai_step_method"], "counted")
        self.assertEqual({c.args[0] for c in request.call_args_list}, {"claude-sonnet-5-5"})
        # framing probe + payload + prompt + reply; repeated texts come from the cache
        self.assertEqual(request.call_count, 4)
        self.assertEqual(err, "")

    def test_unavailable_api_falls_back_to_estimate_once(self) -> None:
        self.skill_session("s1").write(self.claude_dir / "proj" / "s1.jsonl")
        self.skill_session("s2").write(self.claude_dir / "proj" / "s2.jsonl")
        with patch.object(TokenCounter, "_request_claude", side_effect=CountUnavailable("no credentials")) as request:
            _, out, err = self.run_main(estimate=False)
        self.assertEqual([r["ai_step_tokens"] for r in out["recorded"]], [RUN_TOKENS, RUN_TOKENS])
        self.assertEqual({r["ai_step_method"] for r in out["recorded"]}, {"estimated"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(err.count("no credentials"), 1)

    def test_unknown_model_only_affects_that_model(self) -> None:
        old = self.skill_session("old")
        for line in old.lines:
            if line["type"] == "assistant":
                line["message"]["model"] = "claude-retired-1"
        old.write(self.claude_dir / "proj" / "old.jsonl")
        self.skill_session("new").write(self.claude_dir / "proj" / "new.jsonl")

        def request(model: str, text: str) -> int:
            if model == "claude-retired-1":
                raise CountUnavailable("HTTP 404", scope="model")
            return fake_count(model, text)

        with patch.object(TokenCounter, "_request_claude", side_effect=request):
            _, out, _ = self.run_main(estimate=False)
        methods = {r["session_id"]: r["ai_step_method"] for r in out["recorded"]}
        self.assertEqual(methods, {"old": "estimated", "new": "counted"})
        self.assertEqual(out["daily"][0]["ai_step_method"], "mixed")

    def test_partial_counts_mark_row_mixed(self) -> None:
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")

        def request(model: str, text: str) -> int:
            if text == REPLY:
                raise CountUnavailable("HTTP 404", scope="model")
            return fake_count(model, text)

        with patch.object(TokenCounter, "_request_claude", side_effect=request):
            _, out, _ = self.run_main(estimate=False)
        self.assertEqual(out["recorded"][0]["ai_step_method"], "mixed")

    def test_antigravity_counts_with_gemini_model(self) -> None:
        self.write_antigravity("ag-1", skill_steps())
        with patch.object(TokenCounter, "_request_gemini", side_effect=fake_count) as request:
            _, out, _ = self.run_main("--gemini-model", "models/gemini-test", estimate=False)
        (row,) = out["recorded"]
        self.assertEqual((row["ai_step_tokens"], row["ai_step_method"]), (words(PAYLOAD, PROMPT, REPLY), "counted"))
        self.assertEqual({c.args[0] for c in request.call_args_list}, {"gemini-test"})

    def test_estimate_flag_makes_no_requests(self) -> None:
        self.skill_session().write(self.claude_dir / "proj" / "s1.jsonl")
        with patch.object(TokenCounter, "_request_claude") as request:
            self.run_main()
        request.assert_not_called()

    def test_gemini_request_shape(self) -> None:
        response = io.BytesIO(b'{"totalTokens": 42}')
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
                patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(TokenCounter()._request_gemini("gemini-test", "hello"), 42)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:countTokens")
        self.assertEqual(request.get_header("X-goog-api-key"), "k")
        self.assertEqual(json.loads(request.data)["contents"][0]["parts"][0]["text"], "hello")

    def test_gemini_retries_rate_limits(self) -> None:
        import urllib.error
        limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), patch("time.sleep") as sleep, \
                patch("urllib.request.urlopen", side_effect=[limited, io.BytesIO(b'{"totalTokens": 7}')]):
            self.assertEqual(TokenCounter()._request_gemini("gemini-test", "hello"), 7)
        sleep.assert_called_once_with(1)

    def test_gemini_gives_up_after_retries(self) -> None:
        import urllib.error
        limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), patch("time.sleep"), \
                patch("urllib.request.urlopen", side_effect=[limited] * 3) as urlopen:
            with self.assertRaises(CountUnavailable) as raised:
                TokenCounter()._request_gemini("gemini-test", "hello")
        self.assertEqual((raised.exception.scope, urlopen.call_count), ("runner", 3))

    def test_gemini_without_key_is_unavailable(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
        missing = subprocess.CompletedProcess([], 44, "", "item not found")
        with patch.dict(os.environ, env, clear=True), patch("subprocess.run", return_value=missing):
            with self.assertRaises(CountUnavailable):
                TokenCounter()._request_gemini("gemini-test", "hello")

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic SDK not installed")
    def test_claude_request_uses_count_tokens(self) -> None:
        counter = TokenCounter()
        counter._claude_client = client = unittest.mock.MagicMock()
        client.messages.count_tokens.return_value.input_tokens = 17
        self.assertEqual(counter._request_claude("claude-opus-5-5", "hello"), 17)
        client.messages.count_tokens.assert_called_once_with(
            model="claude-opus-5-5", messages=[{"role": "user", "content": "hello"}]
        )



class TestApiKeys(unittest.TestCase):
    """Keys come from the environment first, then the macOS Keychain."""

    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_environment_wins_without_keychain_lookup(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}), patch("subprocess.run") as run:
            self.assertEqual(track_tokens.api_key("gemini"), "from-env")
        run.assert_not_called()

    def test_keychain_fallback(self) -> None:
        found = subprocess.CompletedProcess([], 0, "sk-from-keychain\n", "")
        with patch("subprocess.run", return_value=found) as run:
            self.assertEqual(track_tokens.api_key("anthropic"), "sk-from-keychain")
        self.assertEqual(run.call_args.args[0],
                         ["security", "find-generic-password", "-s", "anthropic-api-key", "-w"])

    def test_missing_item_or_no_security_tool(self) -> None:
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 44, "", "not found")):
            self.assertIsNone(track_tokens.api_key("gemini"))
        with patch("subprocess.run", side_effect=FileNotFoundError("security")):
            self.assertIsNone(track_tokens.api_key("gemini"))

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic SDK not installed")
    def test_claude_client_gets_keychain_key(self) -> None:
        found = subprocess.CompletedProcess([], 0, "sk-from-keychain\n", "")
        with patch("subprocess.run", return_value=found), patch("anthropic.Anthropic") as client_class:
            client_class.return_value.messages.count_tokens.return_value.input_tokens = 5
            self.assertEqual(TokenCounter()._request_claude("claude-opus-5-5", "hi"), 5)
        self.assertEqual(client_class.call_args.kwargs["api_key"], "sk-from-keychain")


class TestExcludedSessions(TrackTokensTestCase):
    def exclude_file(self) -> Path:
        return self.tmp / "telemetry-exclude.txt"

    def test_exclude_removes_rows_and_stops_recording(self) -> None:
        self.skill_session("dev").write(self.claude_dir / "proj" / "dev.jsonl")
        self.skill_session("real").write(self.claude_dir / "proj" / "real.jsonl")
        self.run_main()
        self.assertEqual(sorted(r["session_id"] for r in self.csv_rows()), ["dev", "real"])

        _, out, _ = self.run_main("--exclude", "dev")
        self.assertEqual([r["session_id"] for r in self.csv_rows()], ["real"])
        self.assertEqual([r["session_id"] for r in out["recorded"]], ["real"])
        self.assertEqual(out["daily"][0]["ai_step_tokens"], RUN_TOKENS)
        self.assertEqual(self.exclude_file().read_text(), "dev\n")

        self.run_main()  # the exclusion persists without the flag
        self.assertEqual([r["session_id"] for r in self.csv_rows()], ["real"])

    def test_excluded_sessions_are_not_counted(self) -> None:
        self.exclude_file().write_text("dev\n")
        self.skill_session("dev").write(self.claude_dir / "proj" / "dev.jsonl")
        with patch.object(TokenCounter, "_request_claude", side_effect=fake_count) as request:
            self.run_main(estimate=False)
        request.assert_not_called()

    def test_include_restores_and_keeps_comments(self) -> None:
        self.exclude_file().write_text("# test runs while building the skill\ndev  # 2026-10-07\nother\n")
        self.skill_session("dev").write(self.claude_dir / "proj" / "dev.jsonl")
        self.run_main()
        self.assertEqual(self.csv_rows(), [])
        self.run_main("--include", "dev")
        self.assertEqual([r["session_id"] for r in self.csv_rows()], ["dev"])
        self.assertEqual(self.exclude_file().read_text(), "# test runs while building the skill\nother\n")

    def test_exclude_is_idempotent_and_file_path_overridable(self) -> None:
        custom = self.tmp / "skip.txt"
        self.run_main("--exclude", "a", "--exclude", "a", "--exclude-file", str(custom))
        self.run_main("--exclude", "a", "--exclude-file", str(custom))
        self.assertEqual(custom.read_text(), "a\n")
        self.assertFalse(self.exclude_file().exists())


if __name__ == "__main__":
    unittest.main()
