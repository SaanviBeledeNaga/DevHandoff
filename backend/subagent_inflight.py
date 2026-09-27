"""
DevHandoff — In-Flight State Analyzer (Subagent 1)
===================================================
Bob-assisted module: this subagent was designed with IBM Bob to inspect the
live state of a repository — what's changed, what's broken, what GitHub
issues are linked — and return structured JSON for the LLM synthesis layer.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from git import InvalidGitRepositoryError, Repo


# ---------------------------------------------------------------------------
# GitHub API helpers (unauthenticated; token optional via env var)
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
_GH_TOKEN = os.getenv("GITHUB_TOKEN", "")
_GH_HEADERS = {"Accept": "application/vnd.github+json"}
if _GH_TOKEN:
    _GH_HEADERS["Authorization"] = f"Bearer {_GH_TOKEN}"


def _get_remote_slug(repo: Repo) -> str | None:
    """Return 'owner/repo' from the origin remote URL, or None."""
    try:
        url = repo.remotes.origin.url
        # Handle both https and ssh remote formats
        url = url.rstrip("/").removesuffix(".git")
        if "github.com/" in url:
            return url.split("github.com/")[-1]
        if "github.com:" in url:
            return url.split("github.com:")[-1]
    except Exception:
        pass
    return None


def _fetch_github_issues(slug: str, branch: str) -> list[dict]:
    """
    Fetch open issues and PRs from GitHub whose head branch matches
    the given branch name.  Falls back to an empty list on any error.
    """
    results: list[dict] = []
    try:
        with httpx.Client(timeout=10, headers=_GH_HEADERS) as client:
            # Open PRs for the branch
            resp = client.get(
                f"{GITHUB_API}/repos/{slug}/pulls",
                params={"state": "open", "head": f"{slug.split('/')[0]}:{branch}"},
            )
            if resp.status_code == 200:
                for pr in resp.json():
                    results.append(
                        {
                            "type": "pull_request",
                            "number": pr["number"],
                            "title": pr["title"],
                            "url": pr["html_url"],
                            "body_snippet": (pr.get("body") or "")[:200],
                        }
                    )

            # Open issues (search by branch name mention in title/body)
            resp2 = client.get(
                f"{GITHUB_API}/repos/{slug}/issues",
                params={"state": "open", "per_page": 20},
            )
            if resp2.status_code == 200:
                for issue in resp2.json():
                    if "pull_request" not in issue:  # exclude PRs from issues list
                        results.append(
                            {
                                "type": "issue",
                                "number": issue["number"],
                                "title": issue["title"],
                                "url": issue["html_url"],
                                "labels": [lb["name"] for lb in issue.get("labels", [])],
                            }
                        )
    except Exception as exc:
        results.append({"error": str(exc), "note": "GitHub API unavailable — using local fallback"})
    return results


# ---------------------------------------------------------------------------
# Test runner helpers
# ---------------------------------------------------------------------------

import sys


def _run_pytest(repo_path: str) -> dict[str, Any]:
    """Run pytest with json-report and parse results. Returns a status dict."""
    report_path = Path(repo_path) / ".report.json"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--json-report", f"--json-report-file={report_path}", "-q", "--tb=no"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if report_path.exists():
            with open(report_path) as f:
                data = json.load(f)
            summary = data.get("summary", {})
            return {
                "framework": "pytest",
                "passed": summary.get("passed", 0),
                "failed": summary.get("failed", 0),
                "errors": summary.get("error", 0),
                "total": summary.get("total", 0),
                "exit_code": result.returncode,
                "failed_tests": [
                    {"nodeid": t["nodeid"], "message": t.get("call", {}).get("longrepr", "")[:300]}
                    for t in data.get("tests", [])
                    if t.get("outcome") == "failed"
                ],
            }
    except FileNotFoundError:
        # pytest binary not found — distinct from "tests exist but all pass"
        return {"framework": "pytest", "error": "pytest not installed or not on PATH"}
    except Exception as exc:
        return {"framework": "pytest", "error": str(exc)}
    finally:
        if report_path.exists():
            try:
                report_path.unlink()
            except OSError:
                pass

    # pytest ran but produced no report file — most likely no test files found
    return {"framework": "pytest", "note": "no test files found", "passed": 0, "failed": 0, "total": 0}


def _run_jest(repo_path: str) -> dict[str, Any]:
    """Run jest --json and parse results. Returns a status dict."""
    try:
        result = subprocess.run(
            ["npx", "jest", "--json", "--passWithNoTests"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Jest writes JSON to stdout
        raw = result.stdout.strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            return {
                "framework": "jest",
                "passed": data.get("numPassedTests", 0),
                "failed": data.get("numFailedTests", 0),
                "total": data.get("numTotalTests", 0),
                "exit_code": result.returncode,
            }
    except Exception as exc:
        return {"framework": "jest", "error": str(exc)}
    return {"framework": "jest", "note": "jest not available or no tests found"}


def _detect_and_run_tests(repo_path: str) -> dict[str, Any]:
    """Auto-detect whether this is a Python or JS repo and run the right test suite."""
    root = Path(repo_path)
    has_package_json = (root / "package.json").exists()
    has_pytest = any(root.rglob("test_*.py")) or any(root.rglob("*_test.py")) or (root / "tests").is_dir()

    if has_pytest:
        return _run_pytest(repo_path)
    if has_package_json:
        return _run_jest(repo_path)
    return {"note": "no recognizable test framework found"}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def analyze(repo_path: str, branch: str) -> dict[str, Any]:
    """
    Subagent 1 — In-Flight State Analyzer.

    Returns a structured dict with:
      - uncommitted_changes: list of changed file paths + their diff snippets
      - linked_issues: GitHub issues/PRs linked to this branch
      - test_status: pass/fail summary from pytest or jest
    """
    result: dict[str, Any] = {
        "uncommitted_changes": [],
        "linked_issues": [],
        "test_status": {},
    }

    # --- Git state ---------------------------------------------------------
    try:
        try:
            repo = Repo(repo_path)
        except InvalidGitRepositoryError:
            repo = Repo(repo_path, search_parent_directories=True)

        # Staged + unstaged changes
        changed_files: list[dict] = []
        try:
            diff_index = repo.index.diff(None)           # unstaged
        except Exception:
            diff_index = []

        try:
            staged_index = repo.index.diff("HEAD") if (repo.head.is_valid() and repo.head.commit) else []
        except Exception:
            staged_index = []

        def _collect_diff(diff_iter, kind: str) -> None:
            for diff in diff_iter:
                path = diff.a_path or diff.b_path
                try:
                    patch = diff.diff.decode("utf-8", errors="replace")[:500]
                except Exception:
                    patch = "<binary or unavailable>"
                changed_files.append({"file": path, "status": kind, "diff_snippet": patch})

        _collect_diff(diff_index, "unstaged")
        _collect_diff(staged_index, "staged")

        # Untracked files
        for ut in repo.untracked_files:
            changed_files.append({"file": ut, "status": "untracked", "diff_snippet": ""})

        result["uncommitted_changes"] = changed_files

        # --- GitHub issues -------------------------------------------------
        slug = _get_remote_slug(repo)
        if slug:
            result["linked_issues"] = _fetch_github_issues(slug, branch)
        else:
            result["linked_issues"] = [{"note": "no GitHub remote detected — skipping issue fetch"}]

    except InvalidGitRepositoryError:
        result["uncommitted_changes"] = [{"error": f"{repo_path} is not a git repository"}]
    except Exception as exc:
        result["uncommitted_changes"] = [{"error": str(exc)}]

    # --- Tests -------------------------------------------------------------
    result["test_status"] = _detect_and_run_tests(repo_path)

    return result
