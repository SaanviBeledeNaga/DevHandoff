"""
DevHandoff — LLM Client (IBM watsonx.ai)
=========================================
Thin async wrapper around the IBM watsonx.ai text generation REST API.

Required environment variables (set in .env or shell):
  WATSONX_API_KEY     IBM Cloud API key
  WATSONX_PROJECT_ID  watsonx.ai project ID (UUID)

Optional:
  WATSONX_URL         Instance URL (default: https://us-south.ml.cloud.ibm.com)
  WATSONX_MODEL_ID    Model to use  (default: ibm/granite-3-8b-instruct)
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers — always read direct from .env so the values are
# current regardless of when the process started.
# ---------------------------------------------------------------------------

def _read_env_value(key: str, default: str = "") -> str:
    """Return env var value, falling back to reading .env file directly."""
    val = os.getenv(key, "")
    if val:
        return val
    try:
        from pathlib import Path as _P
        env_path = _P(__file__).resolve().parent.parent / ".env"
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


# Module-level defaults (non-secret config only)
_WX_URL_DEFAULT = "https://us-south.ml.cloud.ibm.com"
_WX_MODEL_DEFAULT = "ibm/granite-3-8b-instruct"

_IAM_URL = "https://iam.cloud.ibm.com/identity/token"
_TIMEOUT = 120  # seconds


# ---------------------------------------------------------------------------
# watsonx.ai API calls
# ---------------------------------------------------------------------------

async def _get_iam_token(api_key: str) -> str:
    """Exchange an IBM Cloud API key for a short-lived IAM bearer token."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _IAM_URL,
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": api_key,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _call_watsonx(prompt: str, api_key: str, project_id: str) -> str:
    """Send a prompt to watsonx.ai and return the generated text."""
    wx_url = _read_env_value("WATSONX_URL", _WX_URL_DEFAULT).rstrip("/")
    model_id = _read_env_value("WATSONX_MODEL_ID", _WX_MODEL_DEFAULT)
    generate_url = f"{wx_url}/ml/v1/text/generation?version=2023-05-29"

    token = await _get_iam_token(api_key)

    payload = {
        "model_id": model_id,
        "input": prompt,
        "parameters": {
            "decoding_method": "greedy",
            "max_new_tokens": 2048,
            "temperature": 0.3,
            "repetition_penalty": 1.1,
        },
        "project_id": project_id,
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            generate_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if results:
            return results[0].get("generated_text", "").strip()
        return ""


# ---------------------------------------------------------------------------
# Public generate() — single entry point for all LLM calls
# ---------------------------------------------------------------------------

async def generate(prompt: str, max_tokens: int = 2048) -> str:
    """
    Call watsonx.ai and return the generated text string.

    On any failure returns a descriptive ⚠️ error string (never raises)
    so the pipeline degrades gracefully.

    Requires WATSONX_API_KEY and WATSONX_PROJECT_ID to be set in .env.
    """
    api_key = _read_env_value("WATSONX_API_KEY")
    project_id = _read_env_value("WATSONX_PROJECT_ID")

    if not api_key or not project_id:
        return (
            "⚠️ watsonx.ai not configured. "
            "Set WATSONX_API_KEY and WATSONX_PROJECT_ID in .env."
        )

    try:
        logger.info("Calling watsonx.ai model: %s", _read_env_value("WATSONX_MODEL_ID", _WX_MODEL_DEFAULT))
        response = await _call_watsonx(prompt, api_key, project_id)
        if response:
            return response
        logger.warning("watsonx.ai returned an empty response")
        return "⚠️ watsonx.ai returned an empty response. Raw subagent data is still available."
    except httpx.HTTPStatusError as exc:
        logger.error("watsonx.ai HTTP error %s: %s", exc.response.status_code, exc.response.text)
        return (
            f"⚠️ watsonx.ai returned HTTP {exc.response.status_code}. "
            "Check WATSONX_API_KEY, WATSONX_PROJECT_ID, and WATSONX_URL."
        )
    except httpx.ConnectError:
        wx_url = _read_env_value("WATSONX_URL", _WX_URL_DEFAULT)
        logger.error("Cannot reach watsonx.ai at %s", wx_url)
        return (
            f"⚠️ watsonx.ai unreachable at {wx_url}. "
            "Check WATSONX_URL and network connectivity."
        )
    except Exception as exc:
        logger.error("watsonx.ai call failed: %s", exc)
        return f"⚠️ watsonx.ai call failed: {exc}."


# ---------------------------------------------------------------------------
# Error detection helper
# ---------------------------------------------------------------------------

_LLM_ERROR_PREFIXES = ("⚠️ watsonx.ai",)


def _llm_failed(text: str) -> bool:
    """Return True if generate() returned an error notice instead of real output."""
    return any(text.startswith(p) for p in _LLM_ERROR_PREFIXES)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _json_snippet(data: Any, max_chars: int = 4000) -> str:
    """Safely serialize data to a truncated JSON string for prompt injection."""
    try:
        raw = json.dumps(data, indent=2, default=str)
    except Exception:
        raw = str(data)
    return raw[:max_chars] + (" ... [truncated]" if len(raw) > max_chars else "")


# ---------------------------------------------------------------------------
# Subagent-specific summarizers
# ---------------------------------------------------------------------------

async def summarize_inflight(data: dict) -> str:
    """Turn Subagent 1's raw JSON into 2-4 plain-English sentences."""
    prompt = f"""You are a senior developer reading a structured JSON report about the
current in-flight state of a code repository.  Write 2-4 plain-English sentences
(no bullet points, no markdown headers) summarizing:
- how many files have uncommitted changes and what kind of changes they are
- what GitHub issues or PRs are linked to the current branch (if any)
- whether the test suite is passing or failing, and how many tests failed

Be concise and factual.  Here is the JSON data:

{_json_snippet(data)}
"""
    res = await generate(prompt)
    if _llm_failed(res):
        uncommitted = len(data.get("uncommitted_changes", []))
        issues = len(data.get("linked_issues", []))
        tests = data.get("test_status", {})
        failed = tests.get("failed", 0)
        passed = tests.get("passed", 0)
        framework = tests.get("framework", "")
        if framework:
            test_info = f"passing ({passed} passed)" if failed == 0 else f"{failed} test(s) failed"
        else:
            test_info = "not run"
        return (
            f"⚠️ [LLM unavailable — heuristic summary] "
            f"{uncommitted} uncommitted change(s) found. "
            f"{issues} linked issue(s)/PR(s) identified. "
            f"Test suite status: {test_info}."
        )
    return res


async def summarize_archaeologist(data: dict) -> str:
    """Turn Subagent 2's raw JSON into 2-4 plain-English sentences."""
    prompt = f"""You are a senior developer reading a structured JSON report containing
git history data for files recently modified in a repository.  Write 2-4 plain-English
sentences summarizing:
- which files were changed and how recently
- what the commit messages suggest about the reason for those changes
- any issue references (#NNN) that indicate what work was in progress

Be concise and factual.  Here is the JSON data:

{_json_snippet(data)}
"""
    res = await generate(prompt)
    if _llm_failed(res):
        files = len(data.get("files", []))
        commits_count = sum(len(f.get("recent_commits", [])) for f in data.get("files", []))
        return (
            f"⚠️ [LLM unavailable — heuristic summary] "
            f"Analyzed Git commit history across {files} file(s) with {commits_count} recent commit records tracked."
        )
    return res


async def summarize_drift(data: dict) -> str:
    """Turn Subagent 3's raw JSON into 2-4 plain-English sentences."""
    prompt = f"""You are a senior developer reading a structured JSON report about
documentation drift and technical debt in a codebase.  Write 2-4 plain-English
sentences summarizing:
- how many public functions/classes are missing docstrings
- what documentation-vs-code mismatches were found
- how many TODO/FIXME/XXX comments exist and what themes they cover

Be concise and factual.  Here is the JSON data:

{_json_snippet(data)}
"""
    res = await generate(prompt)
    if _llm_failed(res):
        todos = len(data.get("todos", []))
        drift_items = len(data.get("drift_items", []))
        stats = data.get("stats", {})
        total_public = stats.get("total_public_symbols", 0)
        with_docs = stats.get("symbols_with_docstrings", 0)
        missing_docs = total_public - with_docs
        return (
            f"⚠️ [LLM unavailable — heuristic summary] "
            f"Found {todos} TODO/FIXME marker(s), "
            f"{missing_docs} undocumented public symbol(s), "
            f"and {drift_items} documentation drift point(s)."
        )
    return res
