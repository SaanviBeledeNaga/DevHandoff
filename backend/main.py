"""
DevHandoff — FastAPI Backend
============================
Bob-assisted module: the orchestrator that runs the three subagents
concurrently via asyncio.gather, passes each result through the LLM
synthesis layer, and returns the final Markdown handoff document.

Security hardening applied (audit fixes S3–S10, B5, B6):
  - CORS restricted to localhost:8501 only
  - Optional API-key guard via DEVHANDOFF_API_KEY env var
  - Rate limiting via slowapi (10 req/min on /generate-handoff)
  - repo_path validated against directory traversal
  - branch validated against safe character set
  - 500 errors return generic message; full detail logged server-side
  - LLM summarization gather uses return_exceptions=True
  - Synthesizer failure degrades gracefully instead of crashing

Run with:
    uvicorn backend.main:app --reload --port 8000
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path as _Path
from typing import Any

# Load .env before any os.getenv() calls (python-dotenv; no-op if not installed)
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=_Path(__file__).resolve().parent.parent / ".env", override=True)
except ImportError:
    pass

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator
from starlette.requests import Request

from backend import subagent_archaeologist, subagent_drift, subagent_inflight
from backend import llm_client, synthesizer
from backend.synthesizer import HANDOFF_SECTIONS

# ---------------------------------------------------------------------------
# Logging — timestamps included for production correlation
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Sentry error tracking
# Set SENTRY_DSN env var to enable — never hardcode the DSN in source.
# ---------------------------------------------------------------------------
_SENTRY_DSN = os.getenv("SENTRY_DSN")
if _SENTRY_DSN:
    try:
        import sentry_sdk  # type: ignore[import]
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=0.1,
            environment=os.getenv("ENVIRONMENT", "development"),
        )
        logger.info("Sentry error tracking enabled (environment=%s)", os.getenv("ENVIRONMENT", "development"))
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed. Run: pip install sentry-sdk")

# ---------------------------------------------------------------------------
# Rate limiter — 10 req/min on /generate-handoff, 20/min on /regenerate-section
# ---------------------------------------------------------------------------
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler  # type: ignore[import]
    from slowapi.errors import RateLimitExceeded  # type: ignore[import]
    from slowapi.util import get_remote_address  # type: ignore[import]
    from limits import parse as _parse_limit  # type: ignore[import]
    _limiter = Limiter(key_func=get_remote_address, default_limits=["10/minute"])
    _SLOWAPI_AVAILABLE = True
except ImportError:
    _limiter = None  # type: ignore[assignment]
    _SLOWAPI_AVAILABLE = False
    logger.warning("slowapi not installed — rate limiting disabled. Run: pip install slowapi")

# ---------------------------------------------------------------------------
# API-key guard
# Set DEVHANDOFF_API_KEY env var to enforce key-based access.
# If unset, the check is skipped (zero friction for local dev).
# ---------------------------------------------------------------------------
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_REQUIRED_KEY: str | None = os.getenv("DEVHANDOFF_API_KEY") or None


async def _verify_api_key(key: str | None = Security(_API_KEY_HEADER)) -> None:
    """Reject the request if DEVHANDOFF_API_KEY is set but the header is wrong."""
    if _REQUIRED_KEY and key != _REQUIRED_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# ---------------------------------------------------------------------------
# Input validation helpers (S6, S7)
# ---------------------------------------------------------------------------

_BRANCH_RE = re.compile(r"^[a-zA-Z0-9_./\-]{1,255}$")


def _validated_repo_path(raw: str) -> str:
    """
    Resolve repo_path to an absolute path and reject traversal attempts.
    Supports local directories as well as remote Git URLs (e.g. GitHub).
    Raises ValueError if the resolved path is not an existing directory or clone fails.
    """
    cleaned = raw.strip().strip("'\"")

    # Check for remote git URL (e.g., https://github.com/user/repo)
    if cleaned.startswith(("https://", "http://", "git@")):
        # Strip any query string / fragment that browsers append (e.g. ?tab=readme, #section)
        cleaned = cleaned.split("?")[0].split("#")[0].rstrip("/")
        if not re.match(r"^(https?://|git@)[a-zA-Z0-9_\-./:]+$", cleaned):
            raise ValueError(f"Invalid repository URL format: '{cleaned}'")
        import git
        repo_name = cleaned.rstrip("/").split("/")[-1].removesuffix(".git")
        if not repo_name:
            repo_name = "cloned_repo"
        target_dir = (_Path(__file__).resolve().parent.parent / "cloned_repos" / repo_name).resolve()
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if (target_dir / ".git").is_dir():
            try:
                repo = git.Repo(str(target_dir))
                repo.remotes.origin.pull()
            except Exception as e:
                logger.warning("Failed to pull latest in %s: %s", target_dir, e)
        else:
            try:
                logger.info("Cloning remote repo %s to %s", cleaned, target_dir)
                git.Repo.clone_from(cleaned, str(target_dir))
            except Exception as exc:
                raise ValueError(f"Failed to clone repository from '{cleaned}': {exc}") from exc
        return str(target_dir)

    if ".." in cleaned or ".." in _Path(cleaned).parts:
        raise ValueError(f"Directory traversal detected in repo_path '{raw}'")
    try:
        resolved = _Path(cleaned).resolve()
    except Exception as exc:
        raise ValueError(f"Invalid repo_path: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError(
            f"repo_path '{raw}' does not exist or is not a directory. "
            "Please provide an existing local folder path (e.g. 'demo-repo') or a valid GitHub URL."
        )
    return str(resolved)


def _validated_branch(raw: str) -> str:
    """Reject branch names that contain shell-unsafe or traversal characters."""
    cleaned = raw.strip().strip("'\"")
    if not _BRANCH_RE.match(cleaned):
        raise ValueError(
            f"branch '{raw}' contains invalid characters. "
            "Only alphanumerics, hyphens, underscores, dots, and slashes are allowed."
        )
    return cleaned


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="DevHandoff API",
    description="AI Developer Handoff Agent — multi-agent system powered by IBM watsonx.ai.",
    version="1.1.0",
)

# Wire rate limiter into the app (only if slowapi is available)
if _SLOWAPI_AVAILABLE and _limiter is not None:
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next):
        path = request.url.path
        limit_str = None
        if path == "/generate-handoff":
            limit_str = "10/minute"
        elif path == "/regenerate-section":
            limit_str = "20/minute"

        if limit_str:
            limit_item = _parse_limit(limit_str)
            key = _limiter._key_func(request)
            if not _limiter._limiter.hit(limit_item, key, path):
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded: {limit_str}"},
                    headers={"Retry-After": "60"},
                )
        return await call_next(request)

# CORS — restricted to the local Streamlit frontend only (fix S3)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)


# ---------------------------------------------------------------------------
# Global exception handlers (fix S10)
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return 422 with field-level detail — no stack traces leaked to client."""
    logger.warning("Validation error on %s: %s", request.url, exc)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.exception_handler(500)
async def _server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unhandled 500s: log full detail server-side, return generic message."""
    logger.exception("Unhandled 500 on %s %s", request.method, request.url)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class HandoffRequest(BaseModel):
    repo_path: str = Field(
        ...,
        max_length=500,
        description="Absolute path to the local git repository",
    )
    branch: str = Field(default="main", max_length=255)

    @field_validator("repo_path")
    @classmethod
    def validate_repo_path(cls, v: str) -> str:
        return _validated_repo_path(v)

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, v: str) -> str:
        return _validated_branch(v)


class SubagentStatus(BaseModel):
    name: str
    status: str          # "running" | "done" | "error"
    duration_seconds: float | None = None
    summary: str = ""    # LLM-summarized sentence(s)


class HandoffResponse(BaseModel):
    markdown: str
    subagent_statuses: list[SubagentStatus]
    total_duration_seconds: float
    raw: dict[str, Any] = Field(default_factory=dict, description="Raw subagent JSON (for debugging)")


class RegenerateRequest(BaseModel):
    section: str = Field(..., description="Section name to regenerate (must be one of the 11 sections)")
    repo_path: str = Field(..., max_length=500)
    branch: str = Field(default="main", max_length=255)
    # Cached subagent summaries — client sends back what it received to avoid re-running agents
    inflight_summary: str = ""
    archaeologist_summary: str = ""
    drift_summary: str = ""
    inflight_raw: dict = Field(default_factory=dict)
    archaeologist_raw: dict = Field(default_factory=dict)
    drift_raw: dict = Field(default_factory=dict)

    @field_validator("repo_path")
    @classmethod
    def validate_repo_path(cls, v: str) -> str:
        return _validated_repo_path(v)

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, v: str) -> str:
        return _validated_branch(v)


# ---------------------------------------------------------------------------
# Core orchestration logic
# ---------------------------------------------------------------------------

async def _run_subagent_with_timing(name: str, coro) -> tuple[str, float, Any]:
    """Run a subagent coroutine, measure duration, return (name, seconds, result)."""
    t0 = time.perf_counter()
    try:
        result = await coro
    except Exception as exc:
        logger.error("Subagent %s raised: %s", name, exc)
        result = {"error": str(exc)}
    duration = time.perf_counter() - t0
    logger.info("Subagent %s finished in %.2fs", name, duration)
    return name, duration, result


def _unwrap_summary(val: Any, label: str) -> str:
    """
    Extract the string result from an asyncio.gather(return_exceptions=True) slot.
    If the slot holds an exception, log it and return a graceful fallback string.
    """
    if isinstance(val, BaseException):
        logger.error("LLM summarization failed for %s: %s", label, val)
        return f"⚠️ Summary unavailable ({label}): {type(val).__name__}"
    return val  # type: ignore[return-value]


async def orchestrate(repo_path: str, branch: str) -> HandoffResponse:
    """
    Main orchestration function.

    1. Run all 3 subagents CONCURRENTLY via asyncio.gather
    2. Send each raw result to watsonx.ai for a short plain-English summary
       (return_exceptions=True so one failure doesn't kill the rest)
    3. Run the Synthesizer Agent — falls back to raw summaries on failure
    """
    t_start = time.perf_counter()

    # Auto-resolve branch if specified branch does not exist in the repo (e.g. master vs main)
    try:
        import git
        _repo = git.Repo(repo_path)
        heads = [h.name for h in _repo.heads]
        if heads and branch not in heads:
            if _repo.active_branch and _repo.active_branch.name in heads:
                logger.info("Branch '%s' not found, defaulting to active branch '%s'", branch, _repo.active_branch.name)
                branch = _repo.active_branch.name
            elif "main" in heads:
                branch = "main"
            elif "master" in heads:
                branch = "master"
    except Exception:
        pass

    # --- Step 1: run all 3 subagents in parallel ----------------------------
    logger.info("Starting 3 subagents in parallel for repo=%s branch=%s", repo_path, branch)

    results = await asyncio.gather(
        _run_subagent_with_timing(
            "In-Flight State Analyzer",
            subagent_inflight.analyze(repo_path, branch),
        ),
        _run_subagent_with_timing(
            "Commit Archaeologist",
            subagent_archaeologist.analyze(repo_path, branch),
        ),
        _run_subagent_with_timing(
            "Doc/Reality Drift Analyzer",
            subagent_drift.analyze(repo_path, branch),
        ),
        return_exceptions=False,  # _run_subagent_with_timing never raises
    )

    inflight_name, inflight_dur, inflight_raw = results[0]
    arch_name, arch_dur, arch_raw = results[1]
    drift_name, drift_dur, drift_raw = results[2]

    # --- Step 2: per-subagent LLM summarization (concurrent, fault-tolerant) ---
    # Skip LLM summarization for any subagent that returned an error dict —
    # the error message IS the summary in that case.
    logger.info("Summarizing 3 subagent outputs via watsonx.ai (concurrent)")

    async def _const(s: str) -> str:
        return s

    def _error_summary(raw: Any, label: str) -> str | None:
        """Return an error string if the subagent dict carries an 'error' key, else None."""
        if isinstance(raw, dict) and "error" in raw:
            return f"⚠️ {label} failed: {raw['error']}"
        return None

    _inflight_err = _error_summary(inflight_raw, "In-Flight State Analyzer")
    _arch_err = _error_summary(arch_raw, "Commit Archaeologist")
    _drift_err = _error_summary(drift_raw, "Doc/Reality Drift Analyzer")

    _summaries = await asyncio.gather(
        _const(_inflight_err) if _inflight_err else llm_client.summarize_inflight(inflight_raw),
        _const(_arch_err) if _arch_err else llm_client.summarize_archaeologist(arch_raw),
        _const(_drift_err) if _drift_err else llm_client.summarize_drift(drift_raw),
        return_exceptions=True,
    )

    inflight_summary = _unwrap_summary(_summaries[0], "inflight")
    archaeologist_summary = _unwrap_summary(_summaries[1], "archaeologist")
    drift_summary = _unwrap_summary(_summaries[2], "drift")

    # --- Step 3: final synthesis (graceful fallback on failure) --------------
    logger.info("Running Synthesizer Agent")
    try:
        markdown = await synthesizer.synthesize(
            repo_path=repo_path,
            branch=branch,
            inflight_summary=inflight_summary,
            archaeologist_summary=archaeologist_summary,
            drift_summary=drift_summary,
            inflight_raw=inflight_raw,
            archaeologist_raw=arch_raw,
            drift_raw=drift_raw,
        )
    except Exception:
        logger.exception("Synthesizer failed — returning raw summaries as fallback")
        markdown = (
            "## ⚠️ Synthesis Unavailable\n\n"
            "The LLM synthesizer could not complete. Raw subagent summaries below.\n\n"
            f"**In-Flight State:** {inflight_summary}\n\n"
            f"**Commit Archaeology:** {archaeologist_summary}\n\n"
            f"**Doc/Reality Drift:** {drift_summary}\n"
        )

    total_duration = time.perf_counter() - t_start

    subagent_statuses = [
        SubagentStatus(
            name=inflight_name,
            status="error" if "error" in inflight_raw else "done",
            duration_seconds=round(inflight_dur, 2),
            summary=inflight_summary,
        ),
        SubagentStatus(
            name=arch_name,
            status="error" if "error" in arch_raw else "done",
            duration_seconds=round(arch_dur, 2),
            summary=archaeologist_summary,
        ),
        SubagentStatus(
            name=drift_name,
            status="error" if "error" in drift_raw else "done",
            duration_seconds=round(drift_dur, 2),
            summary=drift_summary,
        ),
    ]

    return HandoffResponse(
        markdown=markdown,
        subagent_statuses=subagent_statuses,
        total_duration_seconds=round(total_duration, 2),
        raw={
            "inflight": inflight_raw,
            "archaeologist": arch_raw,
            "drift": drift_raw,
        },
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Simple health check endpoint."""
    return {
        "status": "ok",
        "service": "DevHandoff",
        "version": "1.1.0",
        "rate_limiting": _SLOWAPI_AVAILABLE,
        "auth_required": _REQUIRED_KEY is not None,
    }


def _rate_limit(limit: str):
    """
    Decorator marker: rate limiting is enforced at the middleware level before
    body parsing to protect against unvalidated DoS requests.
    """
    def _noop(fn):
        return fn
    return _noop


@app.post("/generate-handoff", response_model=HandoffResponse)
@_rate_limit("10/minute")
async def generate_handoff(
    request: Request,
    body: HandoffRequest,
    _: None = Depends(_verify_api_key),
) -> HandoffResponse:
    """
    Main endpoint — runs all 3 subagents concurrently and returns the
    full Markdown handoff document.

    Rate-limited to 10 requests/minute per IP (when slowapi is installed).
    Protected by X-API-Key header when DEVHANDOFF_API_KEY env var is set.
    """
    try:
        return await orchestrate(body.repo_path, body.branch)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Orchestration failed for repo=%s", body.repo_path)
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@app.post("/regenerate-section")
@_rate_limit("20/minute")
async def regenerate_section(
    request: Request,
    body: RegenerateRequest,
    _: None = Depends(_verify_api_key),
) -> dict[str, str]:
    """
    Re-run synthesis for a single section.
    Used by the 'Regenerate this section' buttons in the Streamlit frontend.
    The client sends back the cached subagent summaries and raw data to
    avoid re-running the expensive subagents.
    """
    if body.section not in HANDOFF_SECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown section '{body.section}'. Must be one of: {HANDOFF_SECTIONS}",
        )

    inflight_summary = body.inflight_summary
    archaeologist_summary = body.archaeologist_summary
    drift_summary = body.drift_summary
    inflight_raw = body.inflight_raw
    archaeologist_raw = body.archaeologist_raw
    drift_raw = body.drift_raw

    if not any([inflight_summary, archaeologist_summary, drift_summary]):
        # No cache — need to re-run full orchestration
        full = await orchestrate(body.repo_path, body.branch)
        return {"section": body.section, "markdown": full.markdown}

    try:
        markdown = await synthesizer.resynthesize_section(
            section_name=body.section,
            repo_path=body.repo_path,
            branch=body.branch,
            inflight_summary=inflight_summary,
            archaeologist_summary=archaeologist_summary,
            drift_summary=drift_summary,
            inflight_raw=inflight_raw,
            archaeologist_raw=archaeologist_raw,
            drift_raw=drift_raw,
        )
    except Exception as exc:
        logger.exception("Section resynthesis failed for '%s'", body.section)
        raise HTTPException(status_code=500, detail="Internal server error") from exc

    return {"section": body.section, "markdown": markdown}
