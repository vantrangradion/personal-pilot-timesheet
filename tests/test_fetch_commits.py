#!/usr/bin/env python3
"""
Unit tests for scripts/fetch_commits.py (US-1.1).
"""

from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set
import unittest
from unittest.mock import patch

from scripts.fetch_commits import (
    fetch_commits,
    find_git_repos,
    get_git_authors,
    get_repo_commits,
    main,
    parse_args,
)


class TestFetchCommits(unittest.TestCase):
    """Test suite for git commit harvester."""

    def setUp(self) -> None:
        """Create a temporary workspace directory for test repos."""
        self.test_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.test_dir) / "workspace"
        self.workspace_dir.mkdir()

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_repo(self, repo_name: str, user_name: str = "Test User", user_email: str = "test@example.com") -> Path:
        """Helper to create and initialize a git repo with user configuration."""
        repo_path = self.workspace_dir / repo_name
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", user_name], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", user_email], cwd=repo_path, check=True, capture_output=True)
        return repo_path

    def _create_commit(
        self,
        repo_path: Path,
        filename: str,
        content: str,
        message: str,
        commit_date: Optional[str] = None,
        author_name: Optional[str] = None,
        author_email: Optional[str] = None,
    ) -> str:
        """Helper to commit a file with optional specified author date and author."""
        file_path = repo_path / filename
        file_path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", filename], cwd=repo_path, check=True, capture_output=True)

        env = os.environ.copy()
        if commit_date:
            env["GIT_AUTHOR_DATE"] = commit_date
            env["GIT_COMMITTER_DATE"] = commit_date
        if author_name:
            env["GIT_AUTHOR_NAME"] = author_name
            env["GIT_COMMITTER_NAME"] = author_name
        if author_email:
            env["GIT_AUTHOR_EMAIL"] = author_email
            env["GIT_COMMITTER_EMAIL"] = author_email

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_path,
            check=True,
            capture_output=True,
            env=env,
        )
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()

    def test_find_git_repos(self) -> None:
        """Verify repository discovery in workspace."""
        repo1 = self._init_repo("repo1")
        repo2 = self._init_repo("repo2")

        # Non-git directory
        non_git = self.workspace_dir / "non-git"
        non_git.mkdir()
        (non_git / "README.md").write_text("not a git repo", encoding="utf-8")

        # Ignored directory
        node_modules = self.workspace_dir / "node_modules"
        node_modules.mkdir()
        subprocess.run(["git", "init"], cwd=node_modules, check=True, capture_output=True)

        discovered = find_git_repos(self.workspace_dir)
        discovered_names = [p.name for p in discovered]

        self.assertIn("repo1", discovered_names)
        self.assertIn("repo2", discovered_names)
        self.assertNotIn("non-git", discovered_names)
        self.assertNotIn("node_modules", discovered_names)

    def test_find_git_repos_nested_depth(self) -> None:
        """Verify repository discovery up to depth 2."""
        org_dir = self.workspace_dir / "group_org"
        org_dir.mkdir()
        nested_repo = org_dir / "nested_project"
        nested_repo.mkdir()
        subprocess.run(["git", "init"], cwd=nested_repo, check=True, capture_output=True)

        discovered = find_git_repos(self.workspace_dir, max_depth=2)
        self.assertIn(nested_repo.resolve(), discovered)

    def test_find_git_repos_workspace_is_repo(self) -> None:
        """Verify repository discovery when workspace_dir itself is a git repo."""
        single_repo = Path(self.test_dir) / "single_repo"
        single_repo.mkdir()
        subprocess.run(["git", "init"], cwd=single_repo, check=True, capture_output=True)

        discovered = find_git_repos(single_repo)
        self.assertEqual(discovered, [single_repo.resolve()])

    def test_get_git_authors(self) -> None:
        """Verify retrieval of git user info."""
        repo = self._init_repo("author_test", user_name="Alice", user_email="alice@example.com")
        authors = get_git_authors(repo)
        self.assertIn("alice@example.com", authors)
        self.assertIn("Alice", authors)

        # Explicit override
        explicit = get_git_authors(repo, explicit_author="custom@example.com")
        self.assertEqual(explicit, ["custom@example.com"])

    def test_scenario_1_extract_commits_target_date(self) -> None:
        """
        US-1.1 Scenario 1:
        Extract commits across child repositories for the target date.
        Valid JSON output with repo, hash, timestamp, message, files_changed_count.
        Exclude merge commits.
        """
        repo = self._init_repo("pilot-repo", user_name="Intern", user_email="intern@gradion.com")
        target_date = date(2026, 10, 6)

        # Commit 1 on target date (1 file changed)
        hash1 = self._create_commit(
            repo,
            filename="file1.txt",
            content="first commit",
            message="feat: first commit",
            commit_date="2026-10-06T10:00:00+07:00",
        )

        # Commit 2 on target date (2 files changed)
        (repo / "extra.txt").write_text("extra", encoding="utf-8")
        subprocess.run(["git", "add", "extra.txt"], cwd=repo, check=True, capture_output=True)
        hash2 = self._create_commit(
            repo,
            filename="file2.txt",
            content="second commit",
            message="feat: second commit",
            commit_date="2026-10-06T14:30:00+07:00",
        )

        # Commit 3 on a DIFFERENT date (yesterday)
        self._create_commit(
            repo,
            filename="yesterday.txt",
            content="old commit",
            message="chore: old commit",
            commit_date="2026-10-05T18:00:00+07:00",
        )

        # Create a branch and a merge commit to verify merge commits are excluded
        subprocess.run(["git", "checkout", "-b", "feature-branch"], cwd=repo, check=True, capture_output=True)
        self._create_commit(
            repo,
            filename="branch_file.txt",
            content="branch commit",
            message="feat: branch commit",
            commit_date="2026-10-06T15:00:00+07:00",
        )
        subprocess.run(["git", "checkout", "master" if subprocess.run(["git", "rev-parse", "--verify", "master"], cwd=repo, capture_output=True).returncode == 0 else "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "merge", "--no-ff", "feature-branch", "-m", "Merge branch feature-branch"], cwd=repo, check=True, capture_output=True)

        commits = fetch_commits(self.workspace_dir, target_date=target_date)

        # Should extract the 3 non-merge commits authored today
        commit_hashes = [c["hash"] for c in commits]
        self.assertIn(hash1, commit_hashes)
        self.assertIn(hash2, commit_hashes)

        # Verify merge commit is NOT in output
        messages = [c["message"] for c in commits]
        self.assertFalse(any("Merge branch" in m for m in messages))

        # Check schema of Commit 1
        c1 = next(c for c in commits if c["hash"] == hash1)
        self.assertEqual(c1["repo"], "pilot-repo")
        self.assertEqual(c1["message"], "feat: first commit")
        self.assertEqual(c1["files_changed_count"], 1)
        self.assertTrue(c1["timestamp"].startswith("2026-10-06"))

        # Check schema of Commit 2 (2 files: extra.txt and file2.txt)
        c2 = next(c for c in commits if c["hash"] == hash2)
        self.assertEqual(c2["files_changed_count"], 2)

    def test_scenario_2_no_commits_target_date(self) -> None:
        """
        US-1.1 Scenario 2:
        No commits authored on target date across repositories.
        Output should be an empty list.
        """
        repo = self._init_repo("empty-repo")
        self._create_commit(
            repo,
            filename="old.txt",
            content="old",
            message="old commit",
            commit_date="2026-10-01T09:00:00+07:00",
        )

        target_date = date(2026, 10, 6)
        commits = fetch_commits(self.workspace_dir, target_date=target_date)
        self.assertEqual(commits, [])

    def test_multi_repository_aggregation_and_ordering(self) -> None:
        """Verify commits across multiple repos are combined and sorted chronologically."""
        repo_a = self._init_repo("repo_a", user_email="intern@gradion.com")
        repo_b = self._init_repo("repo_b", user_email="intern@gradion.com")
        target_date = date(2026, 10, 6)

        # Commit A at 11:00
        self._create_commit(
            repo_a,
            filename="a.txt",
            content="a",
            message="feat: a",
            commit_date="2026-10-06T11:00:00+07:00",
        )

        # Commit B at 09:30
        self._create_commit(
            repo_b,
            filename="b.txt",
            content="b",
            message="feat: b",
            commit_date="2026-10-06T09:30:00+07:00",
        )

        # Commit A at 16:00
        self._create_commit(
            repo_a,
            filename="a2.txt",
            content="a2",
            message="feat: a2",
            commit_date="2026-10-06T16:00:00+07:00",
        )

        commits = fetch_commits(self.workspace_dir, target_date=target_date)
        self.assertEqual(len(commits), 3)

        # Check chronological order (09:30 repo_b, 11:00 repo_a, 16:00 repo_a)
        self.assertEqual(commits[0]["repo"], "repo_b")
        self.assertEqual(commits[1]["repo"], "repo_a")
        self.assertEqual(commits[2]["repo"], "repo_a")

    def test_cli_execution_success(self) -> None:
        """Verify CLI entry point runs and outputs valid JSON."""
        repo = self._init_repo("cli_repo", user_email="intern@gradion.com")
        self._create_commit(
            repo,
            filename="test.txt",
            content="test",
            message="test commit",
            commit_date="2026-10-06T12:00:00+07:00",
        )

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            exit_code = main([
                "--workspace-dir", str(self.workspace_dir),
                "--date", "2026-10-06",
            ])

        self.assertEqual(exit_code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["repo"], "cli_repo")
        self.assertEqual(data[0]["message"], "test commit")

    def test_cli_invalid_date_error(self) -> None:
        """Verify CLI exits with code 1 on malformed date string."""
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            exit_code = main(["--date", "2026-13-99"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Invalid date format", stderr.getvalue())

    def test_cli_nonexistent_workspace_error(self) -> None:
        """Verify CLI exits with code 1 on non-existent workspace directory."""
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            exit_code = main(["--workspace-dir", "/non/existent/path/for/sure"])

        self.assertEqual(exit_code, 1)
        self.assertIn("does not exist", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
