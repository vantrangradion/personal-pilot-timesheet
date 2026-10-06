#!/usr/bin/env python3
"""
Google Calendar OAuth Harvester (US-1.3).

Deterministic script that reads the user's Google Calendar events for a target
date via the Calendar API v3 using OAuth 2.0 desktop credentials
(`credentials.json` + cached `token.json`). Expired access tokens are refreshed
with the stored refresh token and persisted automatically. Declined, cancelled,
transparent (free), all-day, and out-of-office events are filtered out, and a
chronologically sorted JSON array is written to stdout.

Uses Google's official client libraries (see requirements.txt). Run once with
`--auth` to complete the browser consent flow and create token.json.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, List, Optional

try:
    from google.auth.exceptions import GoogleAuthError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    import httplib2

    GOOGLE_IMPORT_ERROR: Optional[ImportError] = None
except ImportError as e:  # pragma: no cover - exercised only without the venv
    GOOGLE_IMPORT_ERROR = e

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDENTIALS = REPO_ROOT / "credentials.json"
DEFAULT_TOKEN = REPO_ROOT / "token.json"
DEFAULT_CALENDAR_ID = "primary"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3/"
# Only the fields the harvester needs, to keep responses small.
EVENT_FIELDS = (
    "nextPageToken,"
    "items(id,summary,status,transparency,eventType,start,end,attendees(self,responseStatus))"
)
SKIPPED_EVENT_TYPES = {"outOfOffice", "workingLocation"}
HTTP_TIMEOUT = 15
AUTH_HINT = (
    "Set up OAuth credentials: download an OAuth 2.0 Desktop client as credentials.json "
    "(Google Cloud Console > APIs & Services > Credentials, with the Calendar API enabled), "
    "then run `.venv/bin/python scripts/fetch_calendar.py --auth` to create token.json."
)
INSTALL_HINT = (
    "Google client libraries are not installed for this interpreter. Run "
    "`python3 -m venv .venv && .venv/bin/pip install --only-binary=:all: -r requirements.txt`, "
    "then use `.venv/bin/python`."
)


class CalendarError(Exception):
    """Raised when OAuth credentials are missing or invalid, or the API call fails."""


def require_google_libs() -> None:
    """Raise CalendarError with install guidance if the Google libraries are missing."""
    if GOOGLE_IMPORT_ERROR is not None:
        raise CalendarError(f"{INSTALL_HINT} ({GOOGLE_IMPORT_ERROR})")


def check_client_config(credentials_path: Path) -> None:
    """
    Verify credentials.json exists and holds an OAuth client ("installed" or "web").

    Raises CalendarError otherwise.
    """
    if not credentials_path.is_file():
        raise CalendarError(f"OAuth client file not found: {credentials_path}. {AUTH_HINT}")
    try:
        data = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CalendarError(f"Could not read {credentials_path}: {e}. {AUTH_HINT}") from e
    client = (data.get("installed") or data.get("web")) if isinstance(data, dict) else None
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise CalendarError(f"{credentials_path} is not a valid OAuth client file. {AUTH_HINT}")


def load_credentials(token_path: Path) -> "Credentials":
    """
    Load cached user credentials from token.json.

    Raises CalendarError if the file is missing or malformed.
    """
    if not token_path.is_file():
        raise CalendarError(f"Cached OAuth token not found: {token_path}. {AUTH_HINT}")
    try:
        info = json.loads(token_path.read_text(encoding="utf-8"))
        return Credentials.from_authorized_user_info(info, SCOPES)
    except (OSError, ValueError) as e:
        raise CalendarError(f"Could not load {token_path}: {e}. {AUTH_HINT}") from e


def save_credentials(token_path: Path, creds: "Credentials") -> None:
    """Atomically write token.json with owner-only permissions."""
    fd, tmp_name = tempfile.mkstemp(prefix=".token-", suffix=".json", dir=str(token_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, token_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def get_credentials(credentials_path: Path, token_path: Path, verbose: bool = False) -> "Credentials":
    """
    Return valid credentials, refreshing and persisting token.json if expired.
    """
    require_google_libs()
    check_client_config(credentials_path)
    creds = load_credentials(token_path)
    if not creds.valid:
        if not creds.refresh_token:
            raise CalendarError(f"Access token expired and token.json has no refresh_token. {AUTH_HINT}")
        if verbose:
            sys.stderr.write("Access token expired; refreshing via refresh_token.\n")
        try:
            creds.refresh(Request())
        except TransportError as e:
            raise CalendarError(f"Could not reach the OAuth token endpoint: {e}") from e
        except GoogleAuthError as e:
            raise CalendarError(f"OAuth token refresh failed: {e}. {AUTH_HINT}") from e
        save_credentials(token_path, creds)
        if verbose:
            sys.stderr.write(f"Persisted refreshed credentials to {token_path}\n")
    return creds


def day_window(target_date: date) -> tuple[datetime, datetime]:
    """Local-time [00:00:00, 23:59:59] bounds of target_date as aware datetimes."""
    start = datetime.combine(target_date, time(0, 0, 0)).astimezone()
    end = datetime.combine(target_date, time(23, 59, 59)).astimezone()
    return start, end


def list_events(
    creds: "Credentials",
    target_date: date,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Call events.list for the local day window, following pagination.

    Returns the raw API event resources.
    """
    time_min, time_max = day_window(target_date)
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
    service = build(
        "calendar",
        "v3",
        http=http,
        cache_discovery=False,
        client_options={"api_endpoint": CALENDAR_API_BASE},
    )
    events = service.events()
    request = events.list(
        calendarId=calendar_id,
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250,
        fields=EVENT_FIELDS,
    )

    items: List[Dict[str, Any]] = []
    while request is not None:
        if verbose:
            sys.stderr.write(f"GET {request.uri}\n")
        try:
            page = request.execute()
        except HttpError as e:
            status = e.resp.status
            if status in (401, 403):
                raise CalendarError(
                    f"Google Calendar API rejected the credentials (HTTP {status}): {e.reason}. {AUTH_HINT}"
                ) from e
            raise CalendarError(f"Google Calendar API error (HTTP {status}): {e.reason}") from e
        except GoogleAuthError as e:
            raise CalendarError(f"OAuth token refresh failed: {e}. {AUTH_HINT}") from e
        except (httplib2.HttpLib2Error, OSError) as e:
            raise CalendarError(f"Could not reach the Google Calendar API: {e}") from e
        items.extend(page.get("items") or [])
        request = events.list_next(request, page)
    return items


def skip_reason(event: Dict[str, Any]) -> Optional[str]:
    """Return why an event is excluded from the timesheet, or None to keep it."""
    if event.get("status") == "cancelled":
        return "cancelled"
    if event.get("eventType") in SKIPPED_EVENT_TYPES:
        return str(event["eventType"])
    if event.get("transparency") == "transparent":
        return "transparent"
    if "dateTime" not in (event.get("start") or {}) or "dateTime" not in (event.get("end") or {}):
        return "all-day"
    for attendee in event.get("attendees") or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return "declined"
    return None


def normalize_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convert one API event to the output schema, with start/end in local time.
    Returns None if the event is malformed.
    """
    try:
        event_id = str(event["id"])
        start = datetime.fromisoformat(str(event["start"]["dateTime"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(event["end"]["dateTime"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "id": event_id,
        "title": (event.get("summary") or "").strip() or "(No title)",
        "start": start.astimezone().isoformat(timespec="seconds"),
        "end": end.astimezone().isoformat(timespec="seconds"),
    }


def fetch_calendar(
    target_date: date,
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Harvest the target date's timed, accepted, busy calendar events,
    sorted chronologically by start, then end, then id.
    """
    creds = get_credentials(credentials_path, token_path, verbose=verbose)
    token_before = creds.token
    raw_events = list_events(creds, target_date, calendar_id=calendar_id, verbose=verbose)
    # The HTTP layer refreshes on a 401 and retries; persist a token rotated that way too.
    if creds.token != token_before:
        save_credentials(token_path, creds)

    events: List[Dict[str, Any]] = []
    for raw in raw_events:
        reason = skip_reason(raw)
        if reason is None:
            event = normalize_event(raw)
            reason = "malformed" if event is None else None
        if reason is not None:
            if verbose:
                sys.stderr.write(f"Skipping event {raw.get('id')!r}: {reason}\n")
            continue
        events.append(event)

    events.sort(key=lambda e: (datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]), e["id"]))
    return events


def run_auth_flow(credentials_path: Path, token_path: Path) -> None:
    """
    Interactive one-time consent: open the browser, capture the code on a
    loopback redirect, and write token.json.
    """
    require_google_libs()
    check_client_config(credentials_path)
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    try:
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    except Exception as e:  # oauthlib raises a variety of error types
        raise CalendarError(f"OAuth consent failed: {e}") from e
    save_credentials(token_path, creds)
    sys.stderr.write(f"Saved OAuth credentials to {token_path}\n")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Harvest Google Calendar events for a target date via OAuth 2.0."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (default: today in local timezone).",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help=f"OAuth client file (default: {DEFAULT_CREDENTIALS}).",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=DEFAULT_TOKEN,
        help=f"Cached user credentials file (default: {DEFAULT_TOKEN}).",
    )
    parser.add_argument(
        "--calendar-id",
        type=str,
        default=DEFAULT_CALENDAR_ID,
        help=f"Calendar to read (default: {DEFAULT_CALENDAR_ID}).",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run the one-time browser consent flow to create token.json, then exit.",
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

    if args.auth:
        try:
            run_auth_flow(args.credentials, args.token)
        except CalendarError as e:
            sys.stderr.write(f"Error: {e}\n")
            return 1
        return 0

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.stderr.write(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.\n")
            return 1
    else:
        target_date = datetime.now().astimezone().date()

    try:
        events = fetch_calendar(
            target_date=target_date,
            credentials_path=args.credentials,
            token_path=args.token,
            calendar_id=args.calendar_id,
            verbose=args.verbose,
        )
    except CalendarError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    sys.stdout.write(json.dumps(events, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
