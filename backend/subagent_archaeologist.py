"""
DevHandoff — Commit Archaeologist (Subagent 2)
==============================================
Bob-assisted module: this subagent uses GitPython to dig through commit
history and git blame on files touched in the current diff, extracting
the rationale trail for every recent change.
"""

from __future__ import annotations

import re
from typing import Any

from git import InvalidGitRepositoryError, Repo


# Regex to find issue references like #123 or GH-123 in commit messages
_ISSUE_RE = re.compile(r"(?:#|GH-)(\d+)")

# How many recent commits to inspect per file
_MAX_COMMITS_PER_FILE = 8

# How many lines of blame context to include
_MAX_BLAME_LINES = 30


def _extract_issue_refs(message: str) -> list[str]:
    """Pull all #NNN issue references out of a commit message."""
    return [f"#{m}" for m in _ISSUE_RE.findall(message)]


def _blame_summary(repo: Repo, file_path: str, branch: str) -> list[dict]:
    """
    Run git blame on a file and return the most recent unique commit
    messages that touched it.  Capped at _MAX_BLAME_LINES entries.
    """
    blame_entries: list[dict] = []
    seen_hexshas: set[str] = set()
    try:
        blame = repo.blame(branch, file_path)
        for commit, lines in blame[:_MAX_BLAME_LINES]:
            if commit.hexsha not in seen_hexshas:
                seen_hexshas.add(commit.hexsha)
                blame_entries.append(
                    {
                        "sha": commit.hexsha[:8],
                        "author": str(commit.author),
                        "message": commit.message.strip()[:200],
                        "issue_refs": _extract_issue_refs(commit.message),
                    }
                )
    except Exception:
        # blame can fail on binary files or new untracked files
        pass
    return blame_entries


def _recent_commits_for_file(repo: Repo, file_path: str) -> list[dict]:
    """Return the _MAX_COMMITS_PER_FILE most recent commits that touched a file."""
    commits: list[dict] = []
    try:
        for commit in repo.iter_commits(paths=file_path, max_count=_MAX_COMMITS_PER_FILE):
            commits.append(
                {
                    "sha": commit.hexsha[:8],
                    "author": str(commit.author),
                    "date": commit.committed_datetime.isoformat(),
                    "message": commit.message.strip()[:300],
                    "issue_refs": _extract_issue_refs(commit.message),
                }
            )
    except Exception:
        pass
    return commits


async def analyze(repo_path: str, branch: str) -> dict[str, Any]:
    """
    Subagent 2 — Commit Archaeologist.

    For every file touched in the current working-tree diff, returns:
      - file: relative path
      - recent_commits: list of recent commit metadata
      - blame_trail: unique commits visible in git blame output
      - likely_rationale: the raw commit messages (not yet LLM-summarized)
    """
    result: dict[str, Any] = {"files": []}

    try:
        try:
            repo = Repo(repo_path)
        except InvalidGitRepositoryError:
            repo = Repo(repo_path, search_parent_directories=True)

        # Collect files that are changed (staged, unstaged, untracked)
        changed_paths: set[str] = set()

        try:
            for diff in repo.index.diff(None):
                changed_paths.add(diff.a_path or diff.b_path)
        except Exception:
            pass

        try:
            if repo.head.is_valid() and repo.head.commit:
                for diff in repo.index.diff("HEAD"):
                    changed_paths.add(diff.a_path or diff.b_path)
        except Exception:
            pass

        for ut in repo.untracked_files:
            changed_paths.add(ut)

        if not changed_paths:
            # No uncommitted changes — fall back to files changed in the last commit
            try:
                if repo.head.is_valid() and repo.head.commit:
                    head = repo.head.commit
                    parent = head.parents[0] if head.parents else None
                    if parent:
                        for diff in head.diff(parent):
                            changed_paths.add(diff.a_path or diff.b_path)
            except Exception:
                pass

        file_records: list[dict] = []
        for file_path in sorted(changed_paths):
            recent = _recent_commits_for_file(repo, file_path)
            blame = _blame_summary(repo, file_path, branch)

            # Combine all unique commit messages as "likely_rationale"
            all_messages: list[str] = []
            seen: set[str] = set()
            for c in recent + blame:
                msg = c.get("message", "")
                if msg and msg not in seen:
                    seen.add(msg)
                    all_messages.append(msg)

            file_records.append(
                {
                    "file": file_path,
                    "recent_commits": recent,
                    "blame_trail": blame,
                    "likely_rationale": all_messages,
                }
            )

        result["files"] = file_records

    except InvalidGitRepositoryError:
        result["error"] = f"{repo_path} is not a git repository"
    except Exception as exc:
        result["error"] = str(exc)

    return result
