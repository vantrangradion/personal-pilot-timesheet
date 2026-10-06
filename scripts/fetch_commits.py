#!/usr/bin/env python3
"""
Multi-Repository Git Commit Harvester (US-1.1).

Deterministic script to scan a workspace root directory and extract all git commits
authored by the user on a target date across all discovered repositories.
Excludes merge commits, normalizes timestamps to local timezone, and outputs a
structured JSON array to stdout.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set

DEFAULT_WORKSPACE_DIR = "/Users/van.tran/Documents/dev"
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"

# Directories to skip when scanning workspace to ensure fast execution (<5s)
IGNORED_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".idea",
    ".vscode",
    ".tox",
    "dist",
    "build",
    ".DS_Store",
}


def get_default_workspace_dir() -> Path:
    """
    Resolve the default workspace root directory.

    Checks WORKSPACE_DIR environment variable first, then DEFAULT_WORKSPACE_DIR,
    falling back to the parent directory of this repository or current working directory.
    """
    env_dir = os.environ.get("WORKSPACE_DIR")
    if env_dir:
        return Path(env_dir)

    configured_path = Path(DEFAULT_WORKSPACE_DIR)
    if configured_path.exists():
        return configured_path

    # Fallback to parent directory of current repository
    script_repo = Path(__file__).resolve().parent.parent
    parent = script_repo.parent
    if parent.exists() and parent != script_repo:
        return parent

    return Path.cwd()


def get_git_authors(repo_path: Path, explicit_author: Optional[str] = None) -> List[str]:
    """
    Determine author search filter(s) for git log in a repository.

    If an explicit author string is passed, returns [explicit_author].
    Otherwise, checks `git config user.email` and `git config user.name`.
    """
    if explicit_author:
        return [explicit_author.strip()]

    authors: List[str] = []
    seen: Set[str] = set()

    for config_key in ("user.email", "user.name"):
        try:
            res = subprocess.run(
                ["git", "-C", str(repo_path), "config", config_key],
                capture_output=True,
                text=True,
                check=False,
            )
            val = res.stdout.strip()
            if val and val not in seen:
                seen.add(val)
                authors.append(val)
        except OSError:
            pass

    # If repo-level check yielded nothing, try global git config
    if not authors:
        for config_key in ("user.email", "user.name"):
            try:
                res = subprocess.run(
                    ["git", "config", "--global", config_key],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                val = res.stdout.strip()
                if val and val not in seen:
                    seen.add(val)
                    authors.append(val)
            except OSError:
                pass

    return authors


def find_git_repos(workspace_dir: Path, max_depth: int = 2) -> List[Path]:
    """
    Find all Git repositories within the specified workspace directory.

    Checks workspace_dir itself and subdirectories up to max_depth.
    Skips ignored directory names (.git, node_modules, etc.) for performance.
    """
    if not workspace_dir.exists():
        return []

    workspace_dir = workspace_dir.resolve()
    repos: List[Path] = []

    # Case 1: The workspace_dir itself is a git repository
    if (workspace_dir / ".git").exists():
        repos.append(workspace_dir)
        return repos

    try:
        entries = sorted(workspace_dir.iterdir(), key=lambda p: p.name)
    except (PermissionError, OSError):
        return []

    for entry in entries:
        if not entry.is_dir() or entry.name in IGNORED_DIR_NAMES or entry.name.startswith("."):
            continue

        if (entry / ".git").exists():
            repos.append(entry)
        elif max_depth > 1:
            try:
                sub_entries = sorted(entry.iterdir(), key=lambda p: p.name)
                for sub in sub_entries:
                    if (
                        sub.is_dir()
                        and sub.name not in IGNORED_DIR_NAMES
                        and not sub.name.startswith(".")
                    ):
                        if (sub / ".git").exists():
                            repos.append(sub)
            except (PermissionError, OSError):
                continue

    return repos


def get_repo_commits(
    repo_path: Path,
    target_date: date,
    author: Optional[str] = None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Extract git commits authored on target_date for a single repository.

    Excludes merge commits (--no-merges), normalizes timestamps to local timezone,
    and counts changed files. Returns a list of structured dictionaries.
    """
    repo_name = repo_path.name
    day_before = target_date - timedelta(days=1)
    day_after = target_date + timedelta(days=1)

    cmd = [
        "git",
        "-C",
        str(repo_path),
        "log",
        "--all",
        "--no-merges",
        "--fixed-strings",
        f"--since={day_before.isoformat()} 00:00:00",
        f"--until={day_after.isoformat()} 23:59:59",
    ]

    authors = get_git_authors(repo_path, explicit_author=author)
    if authors:
        for auth in authors:
            cmd.append(f"--author={auth}")
    elif verbose:
        sys.stderr.write(
            f"[{repo_name}] Warning: No git user.email or user.name configured; querying without author filter.\n"
        )

    cmd.extend([
        f"--format={RECORD_SEP}%h{FIELD_SEP}%aI{FIELD_SEP}%s",
        "--name-only",
    ])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as e:
        if verbose:
            sys.stderr.write(f"[{repo_name}] Failed to run git log: {e}\n")
        return []

    if res.returncode != 0:
        if verbose:
            sys.stderr.write(f"[{repo_name}] git log returned exit code {res.returncode}: {res.stderr.strip()}\n")
        return []

    commits: List[Dict[str, Any]] = []
    blocks = res.stdout.split(RECORD_SEP)

    for block in blocks:
        if not block.strip():
            continue

        lines = block.strip().splitlines()
        header = lines[0]
        parts = header.split(FIELD_SEP)
        if len(parts) < 3:
            continue

        short_hash, ts_str, message = parts[0].strip(), parts[1].strip(), parts[2].strip()

        try:
            commit_dt = datetime.fromisoformat(ts_str)
        except ValueError:
            continue

        # Normalize timestamp to local workspace timezone
        local_dt = commit_dt.astimezone()
        if local_dt.date() != target_date:
            continue

        # Count changed files from following lines
        changed_files = [line.strip() for line in lines[1:] if line.strip()]
        files_changed_count = len(changed_files)

        commits.append({
            "repo": repo_name,
            "hash": short_hash,
            "timestamp": local_dt.isoformat(timespec="seconds"),
            "message": message,
            "files_changed_count": files_changed_count,
        })

    return commits


def fetch_commits(
    workspace_dir: Path,
    target_date: date,
    author: Optional[str] = None,
    single_repo: Optional[Path] = None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Harvest commits across all repositories in workspace_dir (or a single repository)
    for the specified target_date. Deduplicates commits and sorts chronologically.
    """
    if single_repo:
        repos = [single_repo]
    else:
        repos = find_git_repos(workspace_dir)

    all_commits: List[Dict[str, Any]] = []
    seen: Set[tuple[str, str]] = set()

    for repo in repos:
        repo_commits = get_repo_commits(
            repo_path=repo,
            target_date=target_date,
            author=author,
            verbose=verbose,
        )
        for commit in repo_commits:
            key = (commit["repo"], commit["hash"])
            if key not in seen:
                seen.add(key)
                all_commits.append(commit)

    # Sort chronologically ascending
    all_commits.sort(key=lambda c: (c["timestamp"], c["repo"], c["hash"]))
    return all_commits


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Harvest git commits authored on a target date across workspace repositories."
    )
    parser.add_argument(
        "--workspace-dir",
        type=str,
        default=None,
        help="Workspace root directory to scan for git repositories (default: /Users/van.tran/Documents/dev).",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (default: today in local timezone).",
    )
    parser.add_argument(
        "--author",
        type=str,
        default=None,
        help="Author filter (email or name). Defaults to configured Git user.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Specific repository directory to query instead of scanning workspace.",
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

    # Resolve target date
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.stderr.write(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.\n")
            return 1
    else:
        target_date = datetime.now().astimezone().date()

    # Resolve workspace directory
    if args.workspace_dir:
        workspace_dir = Path(args.workspace_dir)
        if not workspace_dir.exists():
            sys.stderr.write(f"Error: Workspace directory does not exist: {workspace_dir}\n")
            return 1
    else:
        workspace_dir = get_default_workspace_dir()

    # Resolve single repo if specified
    single_repo: Optional[Path] = None
    if args.repo:
        single_repo = Path(args.repo)
        if not single_repo.exists():
            sys.stderr.write(f"Error: Specified repo path does not exist: {single_repo}\n")
            return 1

    try:
        commits = fetch_commits(
            workspace_dir=workspace_dir,
            target_date=target_date,
            author=args.author,
            single_repo=single_repo,
            verbose=args.verbose,
        )
    except Exception as e:
        sys.stderr.write(f"Error during commit harvesting: {e}\n")
        return 1

    # Output JSON array to stdout
    json_output = json.dumps(commits, indent=2)
    sys.stdout.write(json_output + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
