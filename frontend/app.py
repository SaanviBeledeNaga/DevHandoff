"""
DevHandoff — Streamlit Frontend
================================
Bob-assisted module: the user-facing UI that triggers the backend pipeline,
shows three independent per-subagent progress spinners (demonstrating real
parallelism), renders the final Markdown, and provides "Regenerate this
section" buttons next to each of the 11 handoff sections.

Run with:
    streamlit run frontend/app.py
"""

from __future__ import annotations

import logging
import os
import time
import threading
from typing import Any

# Frontend logger — writes to stderr/stdout so errors are visible in the
# Streamlit server process log even when the UI doesn't render them.
_logger = logging.getLogger("devhandoff.frontend")

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BACKEND_URL = "http://localhost:8000"

# Read the optional API key from the environment.
# Set DEVHANDOFF_API_KEY in your shell (or .env) before starting Streamlit.
# When unset the header is omitted and the backend runs in open-dev mode.
_API_KEY: str | None = os.getenv("DEVHANDOFF_API_KEY") or None


def _auth_headers() -> dict[str, str]:
    """Return the X-API-Key header dict when a key is configured, else empty."""
    if _API_KEY:
        return {"X-API-Key": _API_KEY}
    return {}


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

# Timeout for the full handoff generation
REQUEST_TIMEOUT = 600  # seconds

st.set_page_config(
    page_title="DevHandoff",
    page_icon="🔀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Custom CSS — minimal, clean
# ---------------------------------------------------------------------------

st.markdown(
    """
<style>
    .agent-card {
        background: #f7f8fa;
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 14px 16px;
        margin-bottom: 8px;
    }
    .agent-card.running { border-left: 4px solid #f59e0b; }
    .agent-card.done    { border-left: 4px solid #22c55e; }
    .agent-card.error   { border-left: 4px solid #ef4444; }
    .agent-name { font-weight: 600; font-size: 0.95rem; }
    .agent-status { font-size: 0.82rem; color: #57606a; margin-top: 4px; }
    .section-header { display: flex; align-items: baseline; gap: 12px; margin-top: 24px; }
    .section-header h2 { margin: 0; }
    hr.section-sep { border: none; border-top: 1px solid #e5e7eb; margin: 12px 0; }
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------

def _init_state() -> None:
    defaults = {
        "handoff_markdown": "",
        "subagent_statuses": [],
        "raw_data": {},
        "summaries": {},
        "generating": False,
        "total_duration": None,
        # Per-section regeneration state
        "regen_loading": {},   # section -> bool
        "section_overrides": {},  # section -> override markdown
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()

# ---------------------------------------------------------------------------
# Backend call helpers
# ---------------------------------------------------------------------------

def _raise_for_status_with_context(resp: httpx.Response) -> None:
    """
    Raise an informative exception on 4xx/5xx responses.
    Provides clear, specific messages for 401 and 422 so the UI can render
    the exact validation message instead of a generic error dump.
    """
    if resp.status_code == 401:
        raise PermissionError(
            "Authentication failed (HTTP 401): the backend requires an API key. "
            "Set DEVHANDOFF_API_KEY in your environment to match the backend's key."
        )
    if resp.status_code == 422:
        try:
            data = resp.json()
            details = data.get("detail", [])
            if isinstance(details, list):
                messages = []
                for d in details:
                    msg = d.get("msg", "")
                    if msg.startswith("Value error, "):
                        msg = msg[len("Value error, "):]
                    loc = " -> ".join(str(x) for x in d.get("loc", []) if x != "body")
                    messages.append(f"{loc}: {msg}" if loc else msg)
                error_msg = "; ".join(messages)
            else:
                error_msg = str(details)
            raise ValueError(f"Input validation error: {error_msg}")
        except Exception as e:
            if isinstance(e, ValueError):
                raise
            resp.raise_for_status()
    resp.raise_for_status()


def call_generate_handoff(repo_path: str, branch: str) -> dict:
    """POST /generate-handoff — includes X-API-Key header when configured."""
    resp = httpx.post(
        f"{BACKEND_URL}/generate-handoff",
        json={"repo_path": repo_path, "branch": branch},
        headers=_auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status_with_context(resp)
    return resp.json()


def call_regenerate_section(section: str, repo_path: str, branch: str, summaries: dict, raw: dict) -> str:
    """POST /regenerate-section — includes X-API-Key header when configured."""
    resp = httpx.post(
        f"{BACKEND_URL}/regenerate-section",
        json={
            "section": section,
            "repo_path": repo_path,
            "branch": branch,
            "inflight_summary": summaries.get("inflight", ""),
            "archaeologist_summary": summaries.get("archaeologist", ""),
            "drift_summary": summaries.get("drift", ""),
            "inflight_raw": raw.get("inflight", {}),
            "archaeologist_raw": raw.get("archaeologist", {}),
            "drift_raw": raw.get("drift", {}),
        },
        headers=_auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status_with_context(resp)
    return resp.json().get("markdown", "")


# ---------------------------------------------------------------------------
# Parallel progress display
# ---------------------------------------------------------------------------

def render_agent_cards(statuses: list[dict]) -> None:
    """
    Render three agent status cards.  Called both during loading (with
    synthetic 'running' state) and after completion (with real statuses).
    """
    cols = st.columns(3)
    agent_icons = ["🔍", "⛏️", "📄"]
    for i, (col, icon) in enumerate(zip(cols, agent_icons)):
        with col:
            if i < len(statuses):
                s = statuses[i]
                css_class = s.get("status", "running")
                status_emoji = {"running": "⏳", "done": "✅", "error": "❌"}.get(css_class, "⏳")
                dur = f" · {s['duration_seconds']:.1f}s" if s.get("duration_seconds") else ""
                summary_html = f"<div class='agent-status'>{s.get('summary', '')[:160]}</div>" if s.get("summary") else ""
                st.markdown(
                    f"""
<div class="agent-card {css_class}">
  <div class="agent-name">{icon} {s['name']}</div>
  <div class="agent-status">{status_emoji} {css_class.capitalize()}{dur}</div>
  {summary_html}
</div>""",
                    unsafe_allow_html=True,
                )
            else:
                # Placeholder while generating
                names = [
                    "In-Flight State Analyzer",
                    "Commit Archaeologist",
                    "Doc/Reality Drift Analyzer",
                ]
                st.markdown(
                    f"""
<div class="agent-card running">
  <div class="agent-name">{icon} {names[i]}</div>
  <div class="agent-status">⏳ Running…</div>
</div>""",
                    unsafe_allow_html=True,
                )


# ---------------------------------------------------------------------------
# Section parsing helpers
# ---------------------------------------------------------------------------

def _split_into_sections(markdown: str) -> dict[str, str]:
    """
    Parse the LLM-generated Markdown into a dict keyed by section name.
    Handles `## Section Name` headings.
    """
    sections: dict[str, str] = {}
    current_section: str | None = None
    buffer: list[str] = []

    for line in markdown.splitlines():
        if line.startswith("## "):
            if current_section is not None:
                sections[current_section] = "\n".join(buffer).strip()
            current_section = line.lstrip("# ").strip()
            buffer = []
        else:
            buffer.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(buffer).strip()

    return sections


def _rebuild_markdown(sections: dict[str, str], overrides: dict[str, str]) -> str:
    """Reassemble full Markdown, substituting per-section overrides."""
    parts: list[str] = []
    for name in HANDOFF_SECTIONS:
        content = overrides.get(name) or sections.get(name, "_No data available for this section._")
        # Strip a leading `## Section Name` if the override already includes it
        if content.startswith(f"## {name}"):
            parts.append(content)
        else:
            parts.append(f"## {name}\n\n{content}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------

def main() -> None:
    # ---- Sidebar -----------------------------------------------------------
    with st.sidebar:
        st.title("🔀 DevHandoff")
        st.caption("AI Developer Handoff Agent")
        st.divider()
        st.markdown(
            "**Powered by:**\n"
            "- 🤖 IBM Bob (AI dev assistant)\n"
            "- 🧠 IBM watsonx.ai (LLM)\n"
            "- ⚡ 3 parallel subagents\n"
            "- 🐍 FastAPI + Streamlit"
        )
        st.divider()
        st.markdown("**Architecture**")
        st.markdown(
            """
```
User Input
    ↓
Orchestrator (asyncio.gather)
    ├── Subagent 1: In-Flight State
    ├── Subagent 2: Commit Archaeologist
    └── Subagent 3: Doc/Reality Drift
         ↓ (all 3 run in parallel)
LLM Summarizer (watsonx.ai × 3)
         ↓
Synthesizer Agent (final doc)
         ↓
Streamlit Renderer
```
"""
        )

    # ---- Header ------------------------------------------------------------
    st.title("🔀 DevHandoff")
    st.markdown(
        "Automatically generate a complete developer handoff document from a Git repository "
        "using three parallel AI agents and a local LLM."
    )

    # ---- Input form --------------------------------------------------------
    with st.form("handoff_form"):
        col1, col2 = st.columns([3, 1])
        with col1:
            repo_path = st.text_input(
                "Repository Path or GitHub URL",
                value="https://github.com/SaanviBeledeNaga/ASHA",
                placeholder="e.g. https://github.com/user/repo or demo-repo",
                help="Enter a local directory path or any public GitHub repository URL.",
            )
        with col2:
            branch = st.text_input(
                "Branch",
                value="main",
                help="Branch name to analyze (default: main).",
            )
        submitted = st.form_submit_button("🚀 Generate Handoff", use_container_width=True)

    # ---- Generation --------------------------------------------------------
    if submitted and repo_path:
        st.session_state["generating"] = True
        st.session_state["handoff_markdown"] = ""
        st.session_state["subagent_statuses"] = []
        st.session_state["section_overrides"] = {}
        st.session_state["regen_loading"] = {}

        # Show 3 "running" agent cards immediately to demonstrate parallelism
        st.markdown("### 🔄 3 Agents Running in Parallel")
        agent_placeholder = st.empty()
        with agent_placeholder.container():
            render_agent_cards([])  # empty = all three show as "running"

        status_text = st.empty()
        status_text.info("⏳ Subagents dispatched — waiting for all three to complete…")

        try:
            t0 = time.perf_counter()
            result = call_generate_handoff(repo_path, branch)
            elapsed = time.perf_counter() - t0

            st.session_state["handoff_markdown"] = result["markdown"]
            st.session_state["subagent_statuses"] = result["subagent_statuses"]
            st.session_state["total_duration"] = result["total_duration_seconds"]
            st.session_state["raw_data"] = result.get("raw", {})

            # Cache subagent summaries for section regeneration
            sas = result["subagent_statuses"]
            st.session_state["summaries"] = {
                "inflight": sas[0]["summary"] if len(sas) > 0 else "",
                "archaeologist": sas[1]["summary"] if len(sas) > 1 else "",
                "drift": sas[2]["summary"] if len(sas) > 2 else "",
            }

            # Update cards to show real completion status
            with agent_placeholder.container():
                render_agent_cards(result["subagent_statuses"])

            status_text.success(
                f"✅ Handoff document generated in {result['total_duration_seconds']:.1f}s"
            )

        except httpx.ConnectError:
            _logger.error("Backend unreachable at %s", BACKEND_URL)
            status_text.error(
                "❌ Cannot connect to backend at http://localhost:8000.  "
                "Start it with: `uvicorn backend.main:app --reload --port 8000`"
            )
            st.session_state["generating"] = False
            return
        except PermissionError as exc:
            # 401 from backend — API key missing or wrong
            _logger.error("Auth rejected by backend: %s", exc)
            status_text.error(
                f"🔑 {exc}\n\n"
                "Make sure DEVHANDOFF_API_KEY is set to the same value in both "
                "the backend shell and the Streamlit shell."
            )
            st.session_state["generating"] = False
            return
        except Exception as exc:
            _logger.exception("Handoff generation failed for repo=%s", repo_path)
            status_text.error(f"❌ Error: {exc}")
            st.session_state["generating"] = False
            return

        st.session_state["generating"] = False

    elif submitted and not repo_path:
        st.warning("Please enter a repository path.")

    # ---- Render handoff document ------------------------------------------
    if st.session_state["handoff_markdown"]:
        st.divider()

        # Show agent summary cards if we haven't just shown them above
        if not submitted and st.session_state["subagent_statuses"]:
            st.markdown("### Agent Summary")
            render_agent_cards(st.session_state["subagent_statuses"])

        # Tabs: Rendered | Raw Markdown | Raw JSON
        tab_rendered, tab_raw_md, tab_raw_json = st.tabs(
            ["📄 Handoff Document", "📝 Raw Markdown", "🔧 Raw Agent Data"]
        )

        # Parse sections for individual rendering
        parsed_sections = _split_into_sections(st.session_state["handoff_markdown"])

        with tab_rendered:
            st.markdown("## 📋 Developer Handoff Document")
            if st.session_state.get("total_duration"):
                st.caption(f"Generated in {st.session_state['total_duration']:.1f}s · {len(HANDOFF_SECTIONS)} sections")

            st.divider()

            for section_name in HANDOFF_SECTIONS:
                # Check if there's a per-section override
                override = st.session_state["section_overrides"].get(section_name)
                content = override if override else parsed_sections.get(
                    section_name, "_No data available for this section._"
                )

                # Section header + regenerate button side by side
                col_head, col_btn = st.columns([5, 1])
                with col_head:
                    st.markdown(f"## {section_name}")
                with col_btn:
                    regen_key = f"regen_{section_name}"
                    is_loading = st.session_state["regen_loading"].get(section_name, False)
                    btn_label = "⏳" if is_loading else "🔄"
                    if st.button(btn_label, key=regen_key, help=f"Regenerate: {section_name}", disabled=is_loading):
                        st.session_state["regen_loading"][section_name] = True
                        try:
                            new_md = call_regenerate_section(
                                section=section_name,
                                repo_path=st.session_state.get("last_repo_path", repo_path),
                                branch=st.session_state.get("last_branch", branch),
                                summaries=st.session_state["summaries"],
                                raw=st.session_state["raw_data"],
                            )
                            # Strip the `## Section Name` header if present (we render it ourselves)
                            clean = new_md
                            if clean.startswith(f"## {section_name}"):
                                clean = clean[len(f"## {section_name}"):].strip()
                            st.session_state["section_overrides"][section_name] = clean
                        except PermissionError as exc:
                            _logger.error("Auth rejected during section regen: %s", exc)
                            st.error(f"🔑 {exc}")
                        except Exception as exc:
                            _logger.exception(
                                "Section regeneration failed for '%s'", section_name
                            )
                            st.error(f"Regeneration failed: {exc}")
                        finally:
                            st.session_state["regen_loading"][section_name] = False
                        st.rerun()

                st.markdown(content)
                st.markdown("<hr class='section-sep'>", unsafe_allow_html=True)

        with tab_raw_md:
            full_md = _rebuild_markdown(parsed_sections, st.session_state["section_overrides"])
            st.text_area(
                "Raw Markdown",
                value=full_md,
                height=600,
                help="Copy this to share the handoff document.",
            )
            st.download_button(
                label="⬇️ Download Markdown",
                data=full_md,
                file_name="handoff.md",
                mime="text/markdown",
            )

        with tab_raw_json:
            st.json(st.session_state["raw_data"])

    # Store last used inputs for regeneration
    if submitted and repo_path:
        st.session_state["last_repo_path"] = repo_path
        st.session_state["last_branch"] = branch


if __name__ == "__main__":
    main()
else:
    main()
