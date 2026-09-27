"""
DevHandoff — Synthesizer Agent
===============================
Bob-assisted module: takes the three LLM-summarized subagent outputs and
calls watsonx.ai once more to produce the final structured Markdown handoff
document with exactly the 11 required sections.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.llm_client import generate, _json_snippet, _llm_failed

logger = logging.getLogger(__name__)

# The exact 11 sections required in the handoff document, in order
HANDOFF_SECTIONS = [
    "Context",
    "What Changed",
    "Why",
    "Current Implementation",
    "Known Problems",
    "Unfinished Work",
    "Key Files",
    "Dependencies",
    "Tests to Run",
    "Risks",
    "Recommended Next Actions",
]


def _build_synthesis_prompt(
    repo_path: str,
    branch: str,
    inflight_summary: str,
    archaeologist_summary: str,
    drift_summary: str,
    inflight_raw: dict,
    archaeologist_raw: dict,
    drift_raw: dict,
) -> str:
    """Build the prompt that asks watsonx.ai to write the full handoff doc."""
    todos_preview = _json_snippet(drift_raw.get("todos", [])[:20], max_chars=1500)
    drift_preview = _json_snippet(drift_raw.get("drift_items", [])[:15], max_chars=1500)
    commits_preview = _json_snippet(archaeologist_raw.get("files", [])[:5], max_chars=2000)
    test_preview = _json_snippet(inflight_raw.get("test_status", {}), max_chars=800)
    issues_preview = _json_snippet(inflight_raw.get("linked_issues", [])[:10], max_chars=800)

    return f"""You are an expert software architect writing a developer handoff document.
A developer is leaving the project mid-stream.  You have been given structured analysis
from three automated agents.  Write a complete Markdown handoff document for the incoming
developer so they can get up to speed immediately.

REPOSITORY: {repo_path}
BRANCH: {branch}

=== AGENT 1 — In-Flight State (LLM Summary) ===
{inflight_summary}

=== AGENT 2 — Commit Archaeology (LLM Summary) ===
{archaeologist_summary}

=== AGENT 3 — Doc/Reality Drift (LLM Summary) ===
{drift_summary}

=== SUPPLEMENTAL RAW DATA ===
Linked Issues/PRs:
{issues_preview}

Test Status:
{test_preview}

Recent Commits by File:
{commits_preview}

TODOs in Codebase:
{todos_preview}

Documentation Drift Items:
{drift_preview}

---

INSTRUCTIONS:
Write a Markdown document with EXACTLY the following 11 sections, in this order.
Use `## Section Name` as the heading for each.  Do not add extra sections.
Do not skip any section — if information is unavailable, write a brief note saying so.

1. ## Context
   (What project is this? What is the branch for? What was the developer working on?)

2. ## What Changed
   (Enumerate files and high-level changes that are uncommitted or recently committed)

3. ## Why
   (Based on commit messages and issue links, explain the rationale for the changes)

4. ## Current Implementation
   (Describe the current code structure and what has actually been implemented so far)

5. ## Known Problems
   (Failing tests, bugs mentioned in TODOs/issues, documentation drift issues)

6. ## Unfinished Work
   (What was in-progress and not yet completed? TODOs, open PRs, half-finished logic)

7. ## Key Files
   (List the most important files an incoming developer must read first, with 1-line descriptions)

8. ## Dependencies
   (Key libraries, services, or external systems this code depends on)

9. ## Tests to Run
   (Exact commands to run the test suite, and which tests are most critical to watch)

10. ## Risks
    (What could break? What assumptions are fragile? What has no test coverage?)

11. ## Recommended Next Actions
    (Ranked list from highest to lowest priority/risk — what should the new developer do first?)

Write clearly for a developer who has never seen this codebase before.
Be specific and concrete — reference actual file names, function names, and issue numbers where available.
"""


def _build_deterministic_markdown(
    repo_path: str,
    branch: str,
    inflight_summary: str,
    archaeologist_summary: str,
    drift_summary: str,
    inflight_raw: dict,
    archaeologist_raw: dict,
    drift_raw: dict,
) -> str:
    import os
    uncommitted = inflight_raw.get("uncommitted_changes", [])
    issues = inflight_raw.get("linked_issues", [])
    tests = inflight_raw.get("test_status", {})
    files = archaeologist_raw.get("files", [])
    todos = drift_raw.get("todos", [])
    drift_items = drift_raw.get("drift_items", [])

    base_name = os.path.basename(os.path.abspath(repo_path))

    sections = [
        f"## Context\n\n**Repository**: `{base_name}`  \n**Branch**: `{branch}`  \n**Local Path**: `{repo_path}`  \n\n*ℹ️ Note: Synthesized directly from subagent heuristics (LLM unavailable). Set `GROQ_API_KEY` in `.env` to enable full AI synthesis.*",
        "## What Changed\n\n" + ("\n".join(f"- `{u.get('file')}` ({u.get('status')})" for u in uncommitted[:15]) if uncommitted else "Working directory clean; tracking commits on this branch."),
        "## Why\n\n" + ("\n".join(f"- #{i.get('number')}: {i.get('title')}" for i in issues[:10]) if issues else "No linked GitHub issues or pull requests explicitly tied to this branch."),
        "## Current Implementation\n\n" + ("\n".join(f"- `{f.get('file')}`: {len(f.get('recent_commits', []))} tracked commit(s)" for f in files[:10]) if files else "Active codebase is tracking current branch HEAD."),
        "## Known Problems\n\n" + (f"**Failing tests**: {tests.get('failed')} failure(s) recorded in test runner.\n\n" if tests.get("failed") else "**Tests**: Passing or clean.\n\n") + ("\n".join(f"- TODO in `{t.get('file')}:{t.get('line')}`: {t.get('text')}" for t in todos[:8]) if todos else "No unresolved TODOs or FIXMEs identified."),
        "## Unfinished Work\n\n" + (f"- {len(uncommitted)} uncommitted file changes pending review or commit.\n" if uncommitted else "- No uncommitted work pending in working tree.\n") + ("\n".join(f"- Open item: #{i.get('number')} {i.get('title')}" for i in issues[:5]) if issues else ""),
        "## Key Files\n\n" + ("\n".join(f"- `{f.get('file')}`" for f in files[:15]) if files else "Repository index mapped by subagents."),
        "## Dependencies\n\n- Detected project configuration files (e.g. `requirements.txt`, `package.json`, environment definitions).",
        f"## Tests to Run\n\n```bash\npytest\n```\n**Status**: {tests.get('status', 'not run')} ({tests.get('passed', 0)} passed, {tests.get('failed', 0)} failed)",
        "## Risks\n\n" + (f"- {len(uncommitted)} uncommitted file(s) risk conflict if not committed.\n" if uncommitted else "- Low working-tree risk.\n") + (f"- {len(drift_items)} documentation/reality drift point(s) detected.\n" if drift_items else ""),
        "## Recommended Next Actions\n\n1. Review recent commits and test suite status.\n2. Resolve open TODO items and drift points identified.\n3. For full AI synthesis: set `GROQ_API_KEY` in `.env`."
    ]

    return "\n\n---\n\n".join(sections)


async def synthesize(
    repo_path: str,
    branch: str,
    inflight_summary: str,
    archaeologist_summary: str,
    drift_summary: str,
    inflight_raw: dict,
    archaeologist_raw: dict,
    drift_raw: dict,
) -> str:
    """
    Synthesizer Agent — calls the LLM with all three subagent outputs and
    returns the complete Markdown handoff document as a string.
    Falls back to rich deterministic markdown if watsonx.ai is unavailable.
    """
    prompt = _build_synthesis_prompt(
        repo_path=repo_path,
        branch=branch,
        inflight_summary=inflight_summary,
        archaeologist_summary=archaeologist_summary,
        drift_summary=drift_summary,
        inflight_raw=inflight_raw,
        archaeologist_raw=archaeologist_raw,
        drift_raw=drift_raw,
    )
    logger.info("Running Synthesizer Agent for repo=%s branch=%s", repo_path, branch)
    markdown = await generate(prompt)
    if _llm_failed(markdown):
        return _build_deterministic_markdown(
            repo_path=repo_path,
            branch=branch,
            inflight_summary=inflight_summary,
            archaeologist_summary=archaeologist_summary,
            drift_summary=drift_summary,
            inflight_raw=inflight_raw,
            archaeologist_raw=archaeologist_raw,
            drift_raw=drift_raw,
        )
    return markdown


async def resynthesize_section(
    section_name: str,
    repo_path: str,
    branch: str,
    inflight_summary: str,
    archaeologist_summary: str,
    drift_summary: str,
    inflight_raw: dict,
    archaeologist_raw: dict,
    drift_raw: dict,
) -> str:
    """
    Re-run synthesis for a single section only.
    Used by the frontend "Regenerate this section" buttons.
    Returns a Markdown string containing just that section.
    """
    prompt = f"""You are an expert software architect.  You are regenerating a single section
of a developer handoff document.  Write ONLY the content for:

## {section_name}

Use the following data sources:

Agent 1 — In-Flight State:
{inflight_summary}

Agent 2 — Commit Archaeology:
{archaeologist_summary}

Agent 3 — Doc/Reality Drift:
{drift_summary}

Raw supplemental data:
{_json_snippet({'inflight': inflight_raw, 'archaeologist': archaeologist_raw, 'drift': drift_raw}, max_chars=3000)}

Repository: {repo_path}
Branch: {branch}

Write 3-8 concrete, specific sentences or bullet points for the ## {section_name} section only.
Do not include any other sections.
"""
    content = await generate(prompt)
    if _llm_failed(content):
        full_doc = _build_deterministic_markdown(
            repo_path=repo_path,
            branch=branch,
            inflight_summary=inflight_summary,
            archaeologist_summary=archaeologist_summary,
            drift_summary=drift_summary,
            inflight_raw=inflight_raw,
            archaeologist_raw=archaeologist_raw,
            drift_raw=drift_raw,
        )
        for part in full_doc.split("\n\n---\n\n"):
            if part.startswith(f"## {section_name}"):
                return part
        # Section not found in deterministic doc — return an explicit notice
        return (
            f"## {section_name}\n\n"
            "⚠️ LLM unavailable. This section could not be synthesized. "
            "Set `GROQ_API_KEY` in `.env` and regenerate."
        )
    return f"## {section_name}\n\n{content}"
