#!/usr/bin/env python3
"""
Dual Runner Session Transcript Token Parser (US-5.1).

Deterministic script that reads local session transcripts from Claude Code
(`~/.claude/projects/<project>/<session>.jsonl`) and Antigravity
(`~/.gemini/antigravity-cli/brain/<session>/.system_generated/logs/transcript_full.jsonl`)
and upserts one row per (date, runner, session_id) into `telemetry.csv`:

- Claude Code: each assistant message's `usage` (`input_tokens`,
  `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`).
  A message is written once per content block with the same usage repeated,
  so usage is counted once per `message.id`.
- Antigravity: each `PLANNER_RESPONSE` step's prompt (`input_tokens`),
  candidate (`output_tokens`), and `cache_read_tokens` counts.
- `cache_tokens` is cache reads plus cache writes, and `total_tokens` is
  input + output + cache. Dates and `timestamp` (the session's last activity
  that day) are in the local timezone.
- `ai_step_tokens` is the [AI] step alone, excluding runner overhead (system
  prompt, tool definitions, skill files): for every `timesheet-skill/run.sh
  prepare` call, the payload the call printed, plus the synthesis prompt when
  that payload is non-empty; for every `run.sh save` call, the JSON reply passed
  to it. Transcripts only record usage per API request, and those requests mix
  the step with other context, so the step's texts are pulled out of the
  transcript and counted with the runner's own tokenizer: Anthropic's
  `messages.count_tokens` (for the session's model) for Claude Code, and the
  Gemini API's `countTokens` for Antigravity. Both endpoints wrap the text in a
  message, so that fixed framing (measured once per model) is subtracted.
  When a count is unavailable (no credentials, network, or an unknown model),
  or with `--estimate`, the text is estimated at ~3 characters per token like
  `prepare_payload.py`; `ai_step_method` records `counted`, `estimated`, or
  `mixed` so estimated rows are never mistaken for exact ones.

API keys come from `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` or, when those are
unset, from the macOS Keychain (generic passwords with service
`anthropic-api-key` / `gemini-api-key`), so they need not sit in a dotfile.

Auto-detected sessions are recorded only for the dates on which they ran the
skill (`run.sh prepare` or `run.sh save`); `--all-sessions` records every
session. Transcript paths given as arguments are always recorded. Sessions
listed in the exclude file (`telemetry-exclude.txt` next to the CSV; one
session id per line, `#` comments allowed), for example test runs made while
building the skill, are never recorded and their rows are removed;
`--exclude ID` / `--include ID` edit that list. Re-running
replaces a session's rows instead of duplicating them, so a session that grew
since the last run is simply updated.

Stdout is JSON: the rows recorded by this run and the daily totals over the
whole CSV (all sessions and runners summed per date), each flagging whether the
day's `ai_step_tokens` exceeds the 1,200-token ceiling from workflow.md (US-4.2).
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import date, datetime
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple
import urllib.error
import urllib.parse
import urllib.request

try:
    from scripts.prepare_payload import estimate_tokens
    from scripts.render_timesheet import write_text_atomic
except ImportError:  # run as `python scripts/track_tokens.py`
    from prepare_payload import estimate_tokens
    from render_timesheet import write_text_atomic

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = "telemetry.csv"
DEFAULT_PROMPT = REPO_ROOT / "timesheet-skill" / "prompts" / "synthesize_blocks.md"
DEFAULT_CLAUDE_DIRS = [Path("~/.claude/projects")]
DEFAULT_ANTIGRAVITY_DIRS = [Path("~/.gemini/antigravity-cli/brain"), Path("~/.gemini/antigravity-ide/brain")]
# Antigravity transcripts name the model only in prose, so its tokenizer is configured.
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
# The model Antigravity runs (`agy models`: gemini-3.8-flash-high); pinned rather than
# `gemini-flash-latest` so counts cannot shift when Google repoints the alias.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_COUNT_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:countTokens"
# Environment variable, then macOS Keychain service, for each API key.
API_KEYS = {"anthropic": ("ANTHROPIC_API_KEY", "anthropic-api-key"), "gemini": ("GEMINI_API_KEY", "gemini-api-key")}
COUNT_TIMEOUT = 20.0
GEMINI_RETRIES = 2  # matches the Anthropic SDK's default max_retries
DAILY_TOKEN_BUDGET = 1200  # [AI] step ceiling per day, declared in timesheet-skill/workflow.md (US-4.2)

CLAUDE, ANTIGRAVITY = "claude", "antigravity"
COUNTED, ESTIMATED, MIXED = "counted", "estimated", "mixed"
COLUMNS = (
    "date",
    "runner",
    "session_id",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "total_tokens",
    "ai_step_tokens",
    "ai_step_method",
    "timestamp",
)
COUNTS = ("input_tokens", "output_tokens", "cache_tokens", "total_tokens", "ai_step_tokens")

# A `run.sh prepare|save` invocation at the start of a shell command (not one
# quoted inside grep, echo, or a heredoc body).
INVOCATION_RE = re.compile(
    r"(?:^|[;&|(]|\b(?:bash|sh|then|do|time)\b)\s*(?:[\w.~/${}-]*/)?run\.sh\s+(prepare|save)\b"
)
HEREDOC_RE = re.compile(r"<<(?!<)(-?)\s*(['\"]?)([\w.-]+)\2")
QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
SHELL_TOOLS = ("command", "CommandLine")


class TelemetryError(Exception):
    """Raised when a transcript or the telemetry CSV cannot be read."""


class CountUnavailable(Exception):
    """Raised when a tokenizer API cannot count; `scope` is 'runner' (give up) or 'model'."""

    def __init__(self, message: str, scope: str = "runner") -> None:
        super().__init__(message)
        self.scope = scope


@dataclass
class Run:
    """One run.sh call: when it ran, the model that read it, and the [AI] step texts it carried."""

    when: datetime
    model: Optional[str]
    texts: List[str]


@dataclass
class Session:
    """Token usage and timesheet runs collected from one session's transcript(s)."""

    runner: str
    session_id: str
    # (time, input, output, cache) per API call.
    usage: List[Tuple[datetime, int, int, int]] = field(default_factory=list)
    runs: List[Run] = field(default_factory=list)


def api_key(provider: str) -> Optional[str]:
    """Return a provider's API key from its environment variable, else the macOS Keychain."""
    variable, service = API_KEYS[provider]
    value = os.environ.get(variable) or (os.environ.get("GOOGLE_API_KEY") if provider == "gemini" else None)
    if value:
        return value
    try:  # `security` prints only the secret with -w; a missing item exits 44
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):  # not macOS, or no security tool
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


class TokenCounter:
    """Counts text with each runner's tokenizer API, returning None where only an estimate is possible.

    Counts are cached per (runner, model, text), so the synthesis prompt that
    every run repeats costs one request. A runner whose API fails for
    credentials or connectivity is not tried again in this process.
    """

    def __init__(self, enabled: bool = True, gemini_model: str = DEFAULT_GEMINI_MODEL) -> None:
        self.enabled = enabled
        self.gemini_model = gemini_model.removeprefix("models/")
        self.warnings: List[str] = []
        self._claude_client: Any = None
        self._down: Set[Tuple[str, Optional[str]]] = set()
        self._cache: Dict[Tuple[str, str, str], int] = {}
        self._framing: Dict[Tuple[str, str], int] = {}

    def count(self, runner: str, model: Optional[str], text: str) -> Optional[int]:
        """Exact token count of `text` for the runner's model, or None if it cannot be counted."""
        if not self.enabled:
            return None
        if runner == ANTIGRAVITY:
            model, request = self.gemini_model, self._request_gemini
        else:
            model = model if model and model.startswith("claude-") else DEFAULT_CLAUDE_MODEL
            request = self._request_claude
        if (runner, None) in self._down or (runner, model) in self._down:
            return None
        key = (runner, model, text)
        if key not in self._cache:
            try:
                if (runner, model) not in self._framing:
                    # A one-character message is the framing plus one token.
                    self._framing[(runner, model)] = max(request(model, "a") - 1, 0)
                self._cache[key] = max(request(model, text) - self._framing[(runner, model)], 0)
            except CountUnavailable as e:
                self._down.add((runner, None if e.scope == "runner" else model))
                self.warnings.append(f"cannot count {runner} tokens with {model} ({e}); using estimates")
                return None
        return self._cache[key]

    def _request_claude(self, model: str, text: str) -> int:
        """Anthropic `messages.count_tokens` for one user message."""
        try:
            import anthropic
        except ImportError as e:
            raise CountUnavailable("the anthropic package is not installed") from e
        try:
            if self._claude_client is None:
                key = api_key("anthropic")  # None lets the SDK use other credentials (e.g. `ant auth login`)
                self._claude_client = anthropic.Anthropic(api_key=key, timeout=COUNT_TIMEOUT, max_retries=2)
            response = self._claude_client.messages.count_tokens(
                model=model, messages=[{"role": "user", "content": text}]
            )
        except (anthropic.NotFoundError, anthropic.BadRequestError) as e:
            raise CountUnavailable(api_error(e), scope="model") from e
        except anthropic.APIStatusError as e:
            raise CountUnavailable(api_error(e)) from e
        except anthropic.APIConnectionError as e:
            raise CountUnavailable("connection failed") from e
        except (anthropic.AnthropicError, TypeError) as e:  # e.g. no credentials configured
            raise CountUnavailable(str(e).splitlines()[0] if str(e) else type(e).__name__) from e
        return int(response.input_tokens)

    def _request_gemini(self, model: str, text: str) -> int:
        """Gemini API `countTokens` for one user turn; the key comes from api_key("gemini")."""
        key = api_key("gemini")
        if not key:
            raise CountUnavailable("no GEMINI_API_KEY and no gemini-api-key Keychain item")
        body = json.dumps({"contents": [{"role": "user", "parts": [{"text": text}]}]}).encode("utf-8")
        request = urllib.request.Request(
            GEMINI_COUNT_URL.format(model=urllib.parse.quote(model, safe="-._")),
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            method="POST",
        )
        for attempt in range(GEMINI_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=COUNT_TIMEOUT) as response:
                    return int(json.load(response)["totalTokens"])
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < GEMINI_RETRIES:
                    time.sleep(2**attempt)  # free-tier rate limits and transient errors clear quickly
                    continue
                raise CountUnavailable(f"HTTP {e.code}", scope="model" if e.code in (400, 404) else "runner") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < GEMINI_RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise CountUnavailable("connection failed") from e
            except (ValueError, KeyError, TypeError) as e:
                raise CountUnavailable("unexpected countTokens response") from e
        raise CountUnavailable("retries exhausted")  # unreachable: the last attempt raises

    def tokens(self, runner: str, model: Optional[str], text: str) -> Tuple[int, bool]:
        """Return (tokens, counted): the exact count when available, else the estimate."""
        counted = self.count(runner, model, text)
        return (counted, True) if counted is not None else (estimate_tokens(text), False)


def api_error(error: Any) -> str:
    """`HTTP <status>: <message>` from an Anthropic API error, e.g. a credit-balance rejection."""
    body = error.body if isinstance(getattr(error, "body", None), dict) else {}
    message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
    return f"HTTP {error.status_code}" + (f": {message}" if message else "")


def to_int(value: Any) -> int:
    """Return a non-negative token count, treating missing or malformed values as 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def parse_time(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp (`Z` or offset) into a local-timezone datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone()


def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield the JSON object lines of a transcript, skipping blank or corrupt lines."""
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except (OSError, UnicodeDecodeError) as e:
        raise TelemetryError(f"Cannot read transcript {path}: {e}") from e


def text_of(content: Any) -> str:
    """Flatten tool-result content (a string or a list of text blocks) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(text_of(c.get("text") if isinstance(c, dict) else c) for c in content)
    return ""


def split_heredocs(command: str) -> List[Tuple[str, Optional[str]]]:
    """Split a shell command into (line, heredoc body or None), with bodies removed from the lines."""
    lines = command.split("\n")
    out: List[Tuple[str, Optional[str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        match = HEREDOC_RE.search(line)
        if not match:
            out.append((line, None))
            continue
        strip_tabs, marker = match.group(1) == "-", match.group(3)
        body: List[str] = []
        while i < len(lines):
            candidate = lines[i].lstrip("\t") if strip_tabs else lines[i]
            i += 1
            if candidate == marker:
                break
            body.append(lines[i - 1])
        out.append((line, "\n".join(body)))
    return out


def find_json(text: str, kind: type) -> Iterator[str]:
    """Yield the source text of each JSON value of `kind` starting at the beginning of a line."""
    decoder = json.JSONDecoder()
    opener = "[" if kind is list else "{"
    pos = 0
    while pos < len(text):
        start = text.find(opener, pos)
        if start < 0:
            return
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].strip():
            pos = start + 1
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(value, kind):
            yield text[start:end]
        pos = end


def is_payload(source: str) -> bool:
    """True for a prepare_payload.py AI payload: a list of blocks with an `id`, or `[]`."""
    value = json.loads(source)
    return all(isinstance(b, dict) and "id" in b for b in value)


def ai_step_texts(command: str, output: str, prompt: str) -> Optional[List[str]]:
    """Return the [AI] step texts one shell call carried, or None if it does not run the skill.

    `run.sh prepare` contributes its printed payload (plus the synthesis prompt
    when the payload is non-empty, since an empty payload means no [AI] call);
    `run.sh save` contributes the JSON reply passed to it in a heredoc or inline.
    """
    stages: List[Tuple[str, Optional[str]]] = []
    for line, body in split_heredocs(command):
        # Blank quoted strings so `grep "run.sh save"` is not an invocation.
        for match in INVOCATION_RE.finditer(QUOTED_RE.sub("''", line)):
            stages.append((match.group(1), body if body is not None else line))
    if not stages:
        return None

    texts: List[str] = []
    if any(stage == "prepare" for stage, _ in stages):
        for payload in find_json(output, list):
            if is_payload(payload):
                texts.append(payload)
                if json.loads(payload) and prompt:
                    texts.append(prompt)
    for stage, reply_source in stages:
        if stage == "save" and reply_source:
            reply = next(find_json(reply_source.strip(), dict), None)
            if reply is None:
                start = reply_source.find("{")  # inline, e.g. `echo '{...}' | run.sh save`
                reply = next(find_json(reply_source[start:], dict), None) if start >= 0 else None
            if reply:
                texts.append(reply)
    return texts


def parse_claude(path: Path, prompt: str, sessions: Dict[Tuple[str, str], Session]) -> None:
    """Add the usage and runs of one Claude Code transcript to `sessions`, keyed by sessionId."""
    messages: Dict[Tuple[str, str], Tuple[datetime, Dict[str, Any]]] = {}
    calls: Dict[str, Tuple[str, datetime, Optional[str], str]] = {}
    results: Dict[str, str] = {}
    for record in read_jsonl(path):
        session_id = str(record.get("sessionId") or path.stem)
        when = parse_time(record.get("timestamp"))
        message = record.get("message")
        if not isinstance(message, dict) or when is None:
            continue
        content = message.get("content") if isinstance(message.get("content"), list) else []
        if record.get("type") == "assistant":
            usage = message.get("usage")
            message_id = message.get("id") or record.get("requestId") or record.get("uuid")
            if isinstance(usage, dict) and message_id:
                # One line per content block repeats the usage; keep the last copy.
                first_seen = messages.get((session_id, message_id), (when, usage))[0]
                messages[(session_id, message_id)] = (first_seen, usage)
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    command = next((tool_input[k] for k in SHELL_TOOLS if isinstance(tool_input.get(k), str)), None)
                    if command is not None and block.get("id"):
                        calls[block["id"]] = (session_id, when, message.get("model"), command)
        elif record.get("type") == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                    results[block["tool_use_id"]] = text_of(block.get("content"))

    for (session_id, _), (when, usage) in messages.items():
        session = sessions.setdefault((CLAUDE, session_id), Session(CLAUDE, session_id))
        cache = to_int(usage.get("cache_read_input_tokens")) + to_int(usage.get("cache_creation_input_tokens"))
        session.usage.append((when, to_int(usage.get("input_tokens")), to_int(usage.get("output_tokens")), cache))
    for call_id, (session_id, when, model, command) in calls.items():
        texts = ai_step_texts(command, results.get(call_id, ""), prompt)
        if texts is not None:
            session = sessions.setdefault((CLAUDE, session_id), Session(CLAUDE, session_id))
            session.runs.append(Run(when, model if isinstance(model, str) else None, texts))


def antigravity_session_id(path: Path) -> str:
    """Return the brain directory name that identifies an Antigravity session."""
    for parent in path.parents:
        if parent.parent.name == "brain":
            return parent.name
    return path.parent.parent.parent.name if path.parent.name == "logs" else path.stem


def tool_arg(value: Any) -> Any:
    """Undo the extra JSON string encoding that the truncated transcript applies to tool args."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def parse_antigravity(path: Path, prompt: str, sessions: Dict[Tuple[str, str], Session]) -> None:
    """Add the usage and runs of one Antigravity transcript to `sessions`, keyed by brain directory."""
    session_id = antigravity_session_id(path)
    session = sessions.setdefault((ANTIGRAVITY, session_id), Session(ANTIGRAVITY, session_id))

    def add_run(when: datetime, command: str, output: str) -> None:
        texts = ai_step_texts(command, output, prompt)
        if texts is not None:
            session.runs.append(Run(when, None, texts))

    pending: List[Tuple[datetime, str]] = []  # shell calls waiting for their output step
    for step in read_jsonl(path):
        when = parse_time(step.get("created_at"))
        if when is None:
            continue
        if step.get("type") == "PLANNER_RESPONSE":
            for call_time, command in pending:  # calls whose output never arrived
                add_run(call_time, command, "")
            pending = []
            prompt_tokens = to_int(step.get("input_tokens", step.get("prompt_tokens")))
            candidates = to_int(step.get("output_tokens", step.get("candidates_tokens")))
            session.usage.append((when, prompt_tokens, candidates, to_int(step.get("cache_read_tokens"))))
            for call in step.get("tool_calls") or []:
                args = call.get("args") if isinstance(call, dict) and isinstance(call.get("args"), dict) else {}
                command = next((tool_arg(args[k]) for k in SHELL_TOOLS if k in args), None)
                pending.append((when, command if isinstance(command, str) else ""))
        elif step.get("type") == "GENERIC" and pending:
            call_time, command = pending.pop(0)
            add_run(call_time, command, text_of(step.get("content")))
    for call_time, command in pending:
        add_run(call_time, command, "")


def detect_runner(path: Path) -> str:
    """Tell an Antigravity transcript (steps with `step_index`) from a Claude Code one."""
    for record in read_jsonl(path):
        return ANTIGRAVITY if "step_index" in record else CLAUDE
    return CLAUDE


def antigravity_transcript(logs: Path) -> Optional[Path]:
    """Prefer the untruncated transcript of an Antigravity logs directory."""
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        if (logs / name).is_file():
            return logs / name
    return None


def discover(claude_dirs: Iterable[Path], antigravity_dirs: Iterable[Path]) -> List[Tuple[str, Path]]:
    """List (runner, transcript) pairs under the runners' default session directories."""
    found: List[Tuple[str, Path]] = []
    for root in claude_dirs:
        root = root.expanduser()
        if root.is_dir():
            found += [(CLAUDE, p) for p in sorted(root.glob("*/*.jsonl"))]
            found += [(CLAUDE, p) for p in sorted(root.glob("*/*/subagents/*.jsonl"))]
    for root in antigravity_dirs:
        root = root.expanduser()
        if root.is_dir():
            for logs in sorted(root.glob("*/.system_generated/logs")):
                transcript = antigravity_transcript(logs)
                if transcript:
                    found.append((ANTIGRAVITY, transcript))
    return found


def expand_paths(paths: Iterable[str]) -> List[Tuple[str, Path]]:
    """Resolve explicit transcript files or session directories into (runner, transcript) pairs."""
    found: List[Tuple[str, Path]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            found.append((detect_runner(path), path))
        elif path.is_dir():
            logs = [path / ".system_generated" / "logs", path]
            transcript = next((t for t in map(antigravity_transcript, logs) if t), None)
            if transcript:
                found.append((ANTIGRAVITY, transcript))
            else:
                found += [(CLAUDE, p) for p in sorted(path.rglob("*.jsonl"))]
        else:
            raise TelemetryError(f"Transcript not found: {path}")
    return found


def method_of(flags: Iterable[bool]) -> str:
    """Summarize which [AI] step texts were counted: counted, estimated, mixed, or '' for none."""
    seen = set(flags)
    if not seen:
        return ""
    return MIXED if len(seen) > 1 else (COUNTED if True in seen else ESTIMATED)


def session_rows(
    session: Session, only_run_dates: bool, counter: TokenCounter, day_filter: Optional[date] = None
) -> List[Dict[str, Any]]:
    """Total a session's usage and [AI] step tokens per local date."""
    by_date: Dict[date, Dict[str, Any]] = {}

    def row(when: datetime) -> Dict[str, Any]:
        entry = by_date.setdefault(
            when.date(),
            {"date": when.date().isoformat(), "runner": session.runner, "session_id": session.session_id,
             **{k: 0 for k in COUNTS}, "last": when, "flags": []},
        )
        entry["last"] = max(entry["last"], when)
        return entry

    for when, input_tokens, output_tokens, cache_tokens in session.usage:
        entry = row(when)
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["cache_tokens"] += cache_tokens
    run_dates = {run.when.date() for run in session.runs}
    for run in session.runs:
        entry = row(run.when)
        if day_filter and run.when.date() != day_filter:
            continue  # not recorded, so not worth a count request
        for text in run.texts:
            tokens, counted = counter.tokens(session.runner, run.model, text)
            entry["ai_step_tokens"] += tokens
            entry["flags"].append(counted)

    rows = []
    for day in sorted(by_date):
        if (only_run_dates and day not in run_dates) or (day_filter and day != day_filter):
            continue
        entry = by_date[day]
        entry["total_tokens"] = entry["input_tokens"] + entry["output_tokens"] + entry["cache_tokens"]
        entry["ai_step_method"] = method_of(entry.pop("flags"))
        entry["timestamp"] = entry.pop("last").replace(microsecond=0).isoformat()
        rows.append({k: entry[k] for k in COLUMNS})
    return rows


def collect(
    transcripts: List[Tuple[str, Path]],
    explicit: List[Tuple[str, Path]],
    prompt: str,
    all_sessions: bool,
    counter: TokenCounter,
    day_filter: Optional[date] = None,
    verbose: bool = False,
    excluded: Set[str] = frozenset(),
) -> List[Dict[str, Any]]:
    """Parse transcripts into telemetry rows; auto-detected ones only on dates that ran the skill."""
    rows: List[Dict[str, Any]] = []
    for group, only_run_dates in ((transcripts, not all_sessions), (explicit, False)):
        sessions: Dict[Tuple[str, str], Session] = {}
        for runner, path in group:
            if only_run_dates:
                try:  # cheap pre-filter: most sessions never call run.sh
                    if "run.sh" not in path.read_text(encoding="utf-8", errors="replace"):
                        continue
                except OSError as e:
                    raise TelemetryError(f"Cannot read transcript {path}: {e}") from e
            parse: Callable[[Path, str, Dict[Tuple[str, str], Session]], None] = (
                parse_antigravity if runner == ANTIGRAVITY else parse_claude
            )
            parse(path, prompt, sessions)
            if verbose:
                sys.stderr.write(f"Parsed {runner} transcript {path}\n")
        for session in sessions.values():
            if session.session_id not in excluded:  # skipped before any count request
                rows += session_rows(session, only_run_dates, counter, day_filter)
    return rows


def default_exclude_path(csv_path: Path) -> Path:
    """`telemetry.csv` -> `telemetry-exclude.txt` in the same directory."""
    return csv_path.with_name(f"{csv_path.stem}-exclude.txt")


def load_excluded(path: Path) -> List[str]:
    """Read excluded session ids: one per line, with `#` comments and blank lines ignored."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise TelemetryError(f"Cannot read {path}: {e}") from e
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def edit_excluded(path: Path, add: Iterable[str], remove: Iterable[str]) -> None:
    """Add or remove session ids in the exclude file, keeping comments and other lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remove = set(remove)
    kept = [line for line in lines if line.split("#", 1)[0].strip() not in remove]
    present = {line.split("#", 1)[0].strip() for line in kept}
    kept += [session_id for session_id in dict.fromkeys(add) if session_id not in present]
    write_text_atomic(path, "".join(f"{line}\n" for line in kept))


def load_csv(path: Path) -> List[Dict[str, Any]]:
    """Read telemetry.csv (missing file -> no rows), with token counts as ints."""
    if not path.exists():
        return []
    try:
        reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
        # Files written before ai_step_method existed hold estimates only.
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or []) and c != "ai_step_method"]
        if missing:
            raise TelemetryError(f"{path} is missing columns: {', '.join(missing)}")
        rows = []
        for line, record in enumerate(reader, start=2):
            record.setdefault("ai_step_method", ESTIMATED if record.get("ai_step_tokens") not in ("", "0") else "")
            try:
                rows.append({**{c: record[c] or "" for c in COLUMNS}, **{c: int(record[c] or 0) for c in COUNTS}})
            except ValueError as e:
                raise TelemetryError(f"{path}:{line}: invalid token count: {e}") from e
        return rows
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise TelemetryError(f"Cannot read {path}: {e}") from e


def upsert(existing: List[Dict[str, Any]], recorded: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replace rows keyed on (date, runner, session_id), add new ones, and sort for stable diffs."""
    key = lambda r: (r["date"], r["runner"], r["session_id"])  # noqa: E731
    merged = {key(r): r for r in existing}
    merged.update({key(r): r for r in recorded})
    return sorted(merged.values(), key=lambda r: (r["date"], r["timestamp"], r["runner"], r["session_id"]))


def render_csv(rows: List[Dict[str, Any]]) -> str:
    """Serialize rows with a fixed column order and `\\n` line endings."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def daily_totals(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sum every session and runner per date, flagging days over the [AI] step budget."""
    days: Dict[str, Dict[str, Any]] = {}
    methods: Dict[str, Set[str]] = {}
    for row in rows:
        day = days.setdefault(row["date"], {"date": row["date"], "sessions": 0, **{k: 0 for k in COUNTS}})
        day["sessions"] += 1
        for k in COUNTS:
            day[k] += row[k]
        if row["ai_step_method"]:
            methods.setdefault(row["date"], set()).add(row["ai_step_method"])
    for d, day in days.items():
        seen = methods.get(d, set())
        day["ai_step_method"] = (seen.pop() if len(seen) == 1 else MIXED) if seen else ""
        day["over_budget"] = day["ai_step_tokens"] > DAILY_TOKEN_BUDGET
    return [days[d] for d in sorted(days)]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Record Claude Code and Antigravity session token usage into telemetry.csv."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Transcript files or session directories to record (default: auto-detect both runners).",
    )
    parser.add_argument(
        "--runner",
        choices=(CLAUDE, ANTIGRAVITY, "all"),
        default="all",
        help="Which runner's session directories to auto-detect (default: all).",
    )
    parser.add_argument(
        "--claude-dir",
        action="append",
        default=None,
        help="Claude Code projects directory (repeatable; default: ~/.claude/projects).",
    )
    parser.add_argument(
        "--antigravity-dir",
        action="append",
        default=None,
        help="Antigravity brain directory (repeatable; default: ~/.gemini/antigravity-cli/brain "
        "and ~/.gemini/antigravity-ide/brain).",
    )
    parser.add_argument(
        "--all-sessions",
        action="store_true",
        help="Record every auto-detected session, not only the dates on which it ran the skill.",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Only record and summarize this local date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=DEFAULT_CSV,
        help=f"Telemetry CSV to update (default: {DEFAULT_CSV}).",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=str(DEFAULT_PROMPT),
        help="Synthesis prompt counted in the [AI] step (default: timesheet-skill/prompts/synthesize_blocks.md).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="SESSION_ID",
        help="Add a session to the exclude file (repeatable): it is no longer recorded and its rows are removed.",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="SESSION_ID",
        help="Remove a session from the exclude file (repeatable), so the next run records it again.",
    )
    parser.add_argument(
        "--exclude-file",
        type=str,
        default=None,
        help="Excluded session ids, one per line (default: <csv stem>-exclude.txt next to --csv).",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Estimate [AI] step tokens offline instead of calling the token-counting APIs.",
    )
    parser.add_argument(
        "--gemini-model",
        type=str,
        default=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        help=f"Gemini model whose tokenizer counts Antigravity sessions (default: $GEMINI_MODEL or "
        f"{DEFAULT_GEMINI_MODEL}).",
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
    csv_path = Path(args.csv)

    try:
        prompt = Path(args.prompt).read_text(encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"Warning: cannot read prompt {args.prompt} ({e}); counting it as 0 tokens.\n")
        prompt = ""

    counter = TokenCounter(enabled=not args.estimate, gemini_model=args.gemini_model)
    exclude_path = Path(args.exclude_file) if args.exclude_file else default_exclude_path(csv_path)
    try:
        if args.exclude or args.include:
            try:
                edit_excluded(exclude_path, args.exclude, args.include)
            except (OSError, UnicodeDecodeError) as e:
                raise TelemetryError(f"Cannot update {exclude_path}: {e}") from e
        excluded = set(load_excluded(exclude_path))
        explicit = expand_paths(args.paths)
        transcripts: List[Tuple[str, Path]] = []
        if not args.paths:
            claude_dirs = [Path(d) for d in args.claude_dir] if args.claude_dir else DEFAULT_CLAUDE_DIRS
            antigravity_dirs = (
                [Path(d) for d in args.antigravity_dir] if args.antigravity_dir else DEFAULT_ANTIGRAVITY_DIRS
            )
            transcripts = discover(
                claude_dirs if args.runner in (CLAUDE, "all") else [],
                antigravity_dirs if args.runner in (ANTIGRAVITY, "all") else [],
            )
        recorded = collect(
            transcripts, explicit, prompt, args.all_sessions, counter, args.date, args.verbose, excluded
        )
        existing = load_csv(csv_path)
    except TelemetryError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1
    for warning in counter.warnings:
        sys.stderr.write(f"Warning: {warning}\n")

    rows = upsert([r for r in existing if r["session_id"] not in excluded], recorded)
    text = render_csv(rows)
    if not csv_path.exists() or csv_path.read_text(encoding="utf-8") != text:
        try:
            write_text_atomic(csv_path, text)
        except OSError as e:
            sys.stderr.write(f"Error: Cannot write {csv_path}: {e}\n")
            return 1

    daily = daily_totals(rows)
    if args.date:
        daily = [d for d in daily if d["date"] == args.date.isoformat()]
    for day in daily:
        if day["over_budget"]:
            sys.stderr.write(
                f"Warning: {day['date']} [AI] step used {day['ai_step_tokens']} tokens, "
                f"over the {DAILY_TOKEN_BUDGET}-token daily budget.\n"
            )
    if args.verbose:
        sys.stderr.write(
            f"{datetime.now().astimezone().replace(microsecond=0).isoformat()} "
            f"Recorded {len(recorded)} session row(s) into {csv_path} ({len(rows)} total); "
            f"{len(excluded)} session(s) excluded via {exclude_path}.\n"
        )
    sys.stdout.write(json.dumps({"recorded": recorded, "daily": daily}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
