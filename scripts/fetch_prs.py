#!/usr/bin/env python3
"""
GitHub Pull Request Harvester (US-1.2).

Deterministic script that uses the GitHub CLI (`gh search prs`) to retrieve PRs
the user authored or reviewed that were updated on a target date. Queries a
±1 day window (GitHub search dates are UTC), then filters by local date, and
outputs a normalized JSON array to stdout.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import subprocess
import sys
from typing import Any, Dict, List, Optional

JSON_FIELDS = "repository,number,title,state,url,updatedAt"
DEFAULT_LIMIT = 100
AUTH_HINT = "Run `gh auth login` to authenticate the GitHub CLI, then retry."


class GhError(Exception):
    """Raised when the GitHub CLI is missing, unauthenticated, or fails."""


def check_gh_auth() -> None:
    """
    Verify the GitHub CLI is installed and authenticated.

    Raises GhError with an actionable message otherwise.
    """
    try:
        res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    except OSError as e:
        raise GhError(
            f"GitHub CLI (`gh`) not found or not executable: {e}. Install it from https://cli.github.com/."
        ) from e
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise GhError(f"GitHub CLI is not authenticated. {AUTH_HINT}\n{detail}".rstrip())


def search_prs(
    qualifier: str,
    target_date: date,
    limit: int = DEFAULT_LIMIT,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Run `gh search prs` with one role qualifier (e.g. `--author=@me`) over a
    ±1 day updated window around target_date. Returns the raw gh JSON records.
    """
    day_before = target_date - timedelta(days=1)
    day_after = target_date + timedelta(days=1)
    cmd = [
        "gh",
        "search",
        "prs",
        qualifier,
        f"--updated={day_before.isoformat()}..{day_after.isoformat()}",
        f"--json={JSON_FIELDS}",
        f"--limit={limit}",
    ]
    if verbose:
        sys.stderr.write(f"Running: {' '.join(cmd)}\n")

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as e:
        raise GhError(f"Failed to run gh: {e}") from e

    if res.returncode != 0:
        stderr = res.stderr.strip()
        if "auth" in stderr.lower() or "login" in stderr.lower():
            raise GhError(f"GitHub CLI authentication failed. {AUTH_HINT}\n{stderr}")
        raise GhError(f"`gh search prs {qualifier}` exited with code {res.returncode}: {stderr}")

    try:
        records = json.loads(res.stdout or "[]")
    except json.JSONDecodeError as e:
        raise GhError(f"Could not parse gh JSON output: {e}") from e
    if not isinstance(records, list):
        raise GhError("Unexpected gh output: expected a JSON array.")
    return records


def derive_status(state: str, authored: bool) -> str:
    """
    Map a gh PR state and the user's role to a timesheet status.

    merged -> "merged"; authored open -> "opened"; authored closed -> "closed";
    review-only (not merged) -> "reviewed".
    """
    state = (state or "").lower()
    if state == "merged":
        return "merged"
    if not authored:
        return "reviewed"
    if state == "closed":
        return "closed"
    return "opened"


def normalize_pr(record: Dict[str, Any], authored: bool) -> Optional[Dict[str, Any]]:
    """
    Convert one raw gh record to the output schema, with updated_at in local time.
    Returns None if the record is malformed.
    """
    try:
        updated_dt = datetime.fromisoformat(str(record["updatedAt"]).replace("Z", "+00:00"))
        number = int(record["number"])
        url = str(record["url"])
    except (KeyError, TypeError, ValueError):
        return None

    repository = record.get("repository") or {}
    repo_name = repository.get("name") or repository.get("nameWithOwner", "").split("/")[-1]

    return {
        "repo": repo_name,
        "number": number,
        "title": record.get("title", ""),
        "status": derive_status(record.get("state", ""), authored),
        "url": url,
        "updated_at": updated_dt.astimezone().isoformat(timespec="seconds"),
    }


def fetch_prs(target_date: date, limit: int = DEFAULT_LIMIT, verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Harvest authored and reviewed PRs updated on target_date (local timezone).

    A PR found by both queries is kept once, with the authored status taking
    precedence. Results are sorted chronologically by updated_at.
    """
    check_gh_auth()

    by_url: Dict[str, Dict[str, Any]] = {}
    for qualifier, authored in (("--author=@me", True), ("--reviewed-by=@me", False)):
        for record in search_prs(qualifier, target_date, limit=limit, verbose=verbose):
            pr = normalize_pr(record, authored)
            if pr is None:
                if verbose:
                    sys.stderr.write(f"Skipping malformed gh record: {record!r}\n")
                continue
            if datetime.fromisoformat(pr["updated_at"]).date() != target_date:
                continue
            # Authored query runs first, so setdefault keeps the authored status.
            by_url.setdefault(pr["url"], pr)

    prs = list(by_url.values())
    prs.sort(key=lambda p: (p["updated_at"], p["repo"], p["number"]))
    return prs


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Harvest GitHub PRs authored or reviewed by @me that were updated on a target date."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (default: today in local timezone).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum results per gh search query (default: {DEFAULT_LIMIT}).",
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

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.stderr.write(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.\n")
            return 1
    else:
        target_date = datetime.now().astimezone().date()

    try:
        prs = fetch_prs(target_date=target_date, limit=args.limit, verbose=args.verbose)
    except GhError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    sys.stdout.write(json.dumps(prs, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
