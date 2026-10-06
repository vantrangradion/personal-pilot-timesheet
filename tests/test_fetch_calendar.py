#!/usr/bin/env python3
"""
Unit tests for scripts/fetch_calendar.py (US-1.3).

A local HTTP server stands in for both the Google OAuth token endpoint and the
Calendar API, so the Google client libraries make real HTTP requests without
network access. Run with the venv interpreter (`.venv/bin/python`).
"""

from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Dict, List
import unittest
from unittest.mock import patch
import urllib.parse

from scripts import fetch_calendar
from scripts.fetch_calendar import (
    CalendarError,
    fetch_calendar as fetch_events,
    GOOGLE_IMPORT_ERROR,
    main,
    normalize_event,
    skip_reason,
)


class FakeGoogle:
    """In-memory state shared with the fake HTTP handler."""

    def __init__(self) -> None:
        self.valid_token = "fresh-token"
        self.pages: List[Dict[str, Any]] = [{"items": []}]
        self.refresh_status = 200
        self.api_status = 200
        self.requests: List[Dict[str, Any]] = []


def _make_handler(state: FakeGoogle) -> type:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode("utf-8")))
            state.requests.append({"method": "POST", "path": self.path, "form": form})
            if state.refresh_status != 200:
                self._send(state.refresh_status, {"error": "invalid_grant"})
                return
            self._send(200, {"access_token": state.valid_token, "expires_in": 3600, "token_type": "Bearer"})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            auth = self.headers.get("Authorization")
            state.requests.append({"method": "GET", "path": parsed.path, "query": query, "auth": auth})
            if state.api_status != 200:
                self._send(state.api_status, {"error": {"code": state.api_status, "message": "Forbidden"}})
                return
            if auth != f"Bearer {state.valid_token}":
                self._send(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
                return
            page_index = int(query.get("pageToken", "0"))
            self._send(200, state.pages[page_index])

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def _google_expiry(dt: datetime) -> str:
    """Format an expiry the way google-auth writes it (naive UTC)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _local(hour: int, minute: int = 0, day: int = 6) -> str:
    """ISO datetime for 2026-10-<day> HH:MM in the local timezone."""
    return datetime(2026, 10, day, hour, minute).astimezone().isoformat()


def _event(event_id: str, start: str, end: str, **extra: Any) -> Dict[str, Any]:
    """Build a raw Calendar API event resource."""
    event = {
        "id": event_id,
        "summary": extra.pop("summary", event_id),
        "status": "confirmed",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    event.update(extra)
    return event


@unittest.skipIf(GOOGLE_IMPORT_ERROR is not None, "Google client libraries not installed (use .venv/bin/python)")
class TestFetchCalendar(unittest.TestCase):
    """Test suite for the Google Calendar harvester."""

    def setUp(self) -> None:
        """Start the fake Google server and write credentials/token fixtures."""
        self.state = FakeGoogle()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.api_patch = patch.object(fetch_calendar, "CALENDAR_API_BASE", f"{self.base}/calendar/v3/")
        self.api_patch.start()
        # google-auth ignores token.json's token_uri and always uses this constant.
        import google.oauth2.credentials
        self.token_uri_patch = patch.object(
            google.oauth2.credentials, "_GOOGLE_OAUTH2_TOKEN_ENDPOINT", f"{self.base}/token"
        )
        self.token_uri_patch.start()

        self.test_dir = Path(tempfile.mkdtemp())
        self.credentials_path = self.test_dir / "credentials.json"
        self.token_path = self.test_dir / "token.json"
        self.credentials_path.write_text(json.dumps({"installed": {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token_uri": f"{self.base}/token",
        }}), encoding="utf-8")
        self._write_token("fresh-token", datetime.now(timezone.utc) + timedelta(hours=1))

    def tearDown(self) -> None:
        """Stop the server and remove temp files."""
        self.token_uri_patch.stop()
        self.api_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_token(self, token: str, expiry: datetime, refresh: str = "refresh-1") -> None:
        self.token_path.write_text(json.dumps({
            "token": token,
            "refresh_token": refresh,
            "token_uri": f"{self.base}/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scopes": fetch_calendar.SCOPES,
            "expiry": _google_expiry(expiry),
        }), encoding="utf-8")

    def _fetch(self) -> List[Dict[str, Any]]:
        return fetch_events(date(2026, 10, 6), credentials_path=self.credentials_path, token_path=self.token_path)

    def _main(self, *extra: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--date", "2026-10-06", "--credentials", str(self.credentials_path),
                "--token", str(self.token_path), *extra]
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_scenario_1_filters_and_sorts_events(self) -> None:
        """Scenario 1: queries the local day window, filters, and outputs sorted events."""
        self.state.pages = [{"items": [
            _event("review", _local(14), _local(15), summary="Code Review"),
            _event("standup", _local(9, 30), _local(10), summary="Daily Standup",
                   attendees=[{"self": True, "responseStatus": "accepted"}]),
            _event("declined", _local(11), _local(12),
                   attendees=[{"email": "x@y", "responseStatus": "accepted"},
                              {"self": True, "responseStatus": "declined"}]),
            _event("free", _local(12), _local(13), transparency="transparent"),
            _event("ooo", _local(16), _local(18), eventType="outOfOffice"),
            {"id": "allday", "summary": "Holiday", "start": {"date": "2026-10-06"}, "end": {"date": "2026-10-07"}},
            _event("cancelled", _local(8), _local(9), status="cancelled"),
        ]}]

        events = self._fetch()

        self.assertEqual(events, [
            {"id": "standup", "title": "Daily Standup",
             "start": datetime(2026, 10, 6, 9, 30).astimezone().isoformat(timespec="seconds"),
             "end": datetime(2026, 10, 6, 10, 0).astimezone().isoformat(timespec="seconds")},
            {"id": "review", "title": "Code Review",
             "start": datetime(2026, 10, 6, 14, 0).astimezone().isoformat(timespec="seconds"),
             "end": datetime(2026, 10, 6, 15, 0).astimezone().isoformat(timespec="seconds")},
        ])

        (call,) = [r for r in self.state.requests if r["method"] == "GET"]
        self.assertEqual(call["path"], "/calendar/v3/calendars/primary/events")
        self.assertEqual(call["query"]["timeMin"], datetime(2026, 10, 6, 0, 0, 0).astimezone().isoformat())
        self.assertEqual(call["query"]["timeMax"], datetime(2026, 10, 6, 23, 59, 59).astimezone().isoformat())
        self.assertEqual(call["query"]["singleEvents"], "true")
        self.assertIn("items(", call["query"]["fields"])
        self.assertFalse(any(r["method"] == "POST" for r in self.state.requests))

    def test_utc_event_times_normalized_to_local(self) -> None:
        utc_start = datetime(2026, 10, 6, 9, 0).astimezone().astimezone(timezone.utc)
        raw = _event("e1", utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     (utc_start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        event = normalize_event(raw)
        self.assertEqual(event["start"], datetime(2026, 10, 6, 9, 0).astimezone().isoformat(timespec="seconds"))

    def test_untitled_and_malformed_events(self) -> None:
        self.assertEqual(normalize_event(_event("e", _local(9), _local(10), summary=""))["title"], "(No title)")
        self.assertIsNone(normalize_event({"id": "x", "start": {"dateTime": "nope"}, "end": {}}))
        self.assertIsNone(skip_reason(_event("e", _local(9), _local(10))))

    def test_follows_pagination(self) -> None:
        self.state.pages = [
            {"items": [_event("b", _local(13), _local(14))], "nextPageToken": "1"},
            {"items": [_event("a", _local(10), _local(11))]},
        ]
        self.assertEqual([e["id"] for e in self._fetch()], ["a", "b"])
        self.assertEqual(len([r for r in self.state.requests if r["method"] == "GET"]), 2)

    def test_expired_token_is_refreshed_and_persisted(self) -> None:
        """Scenario 1: expired access token is refreshed and token.json is updated."""
        self._write_token("stale-token", datetime.now(timezone.utc) - timedelta(minutes=5))
        self.state.pages = [{"items": [_event("standup", _local(9, 30), _local(10))]}]

        self.assertEqual([e["id"] for e in self._fetch()], ["standup"])

        (refresh,) = [r for r in self.state.requests if r["method"] == "POST"]
        self.assertEqual(refresh["form"]["grant_type"], "refresh_token")
        self.assertEqual(refresh["form"]["refresh_token"], "refresh-1")
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["token"], "fresh-token")
        self.assertEqual(saved["refresh_token"], "refresh-1")
        expiry = datetime.fromisoformat(saved["expiry"].replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        self.assertGreater(expiry, datetime.now(timezone.utc))
        self.assertEqual(self.token_path.stat().st_mode & 0o777, 0o600)

    def test_no_events_outputs_empty_array(self) -> None:
        code, out, _ = self._main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_scenario_2_missing_credentials(self) -> None:
        """Scenario 2: missing credentials.json exits non-zero with setup guidance."""
        self.credentials_path.unlink()
        code, out, err = self._main()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("credentials.json", err)
        self.assertIn("--auth", err)
        self.assertEqual(self.state.requests, [])

    def test_scenario_2_missing_token(self) -> None:
        self.token_path.unlink()
        code, _, err = self._main()
        self.assertNotEqual(code, 0)
        self.assertIn("token.json", err)

    def test_scenario_2_refresh_rejected(self) -> None:
        """Scenario 2: a revoked refresh token fails with an OAuth setup message."""
        self._write_token("stale-token", datetime.now(timezone.utc) - timedelta(minutes=5))
        self.state.refresh_status = 400
        code, _, err = self._main()
        self.assertNotEqual(code, 0)
        self.assertIn("invalid_grant", err)
        self.assertIn("--auth", err)

    def test_revoked_access_token_is_refreshed_on_401(self) -> None:
        """An unexpired but revoked access token is refreshed on 401, retried, and persisted."""
        self._write_token("revoked-token", datetime.now(timezone.utc) + timedelta(hours=1))
        self.state.pages = [{"items": [_event("standup", _local(9, 30), _local(10))]}]

        self.assertEqual([e["id"] for e in self._fetch()], ["standup"])

        self.assertEqual([r["method"] for r in self.state.requests], ["GET", "POST", "GET"])
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["token"], "fresh-token")

    def test_scenario_2_api_rejects_credentials(self) -> None:
        self.state.api_status = 403
        with self.assertRaises(CalendarError) as ctx:
            self._fetch()
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("--auth", str(ctx.exception))

    def test_cli_invalid_date_error(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            exit_code = main(["--date", "2026-13-99"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Invalid date format", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
