# 🔀 DevHandoff — AI Developer Handoff Agent

> **Automatically generate a complete developer handoff document from any Git repository using three parallel AI subagents and a local LLM — no paid APIs, no cloud dependencies.**

---

## What Problem Does It Solve?

When a developer leaves a project mid-stream, the incoming developer wastes days reconstructing context — why decisions were made, what's broken, what's unfinished — through rushed meetings and manual code spelunking. **DevHandoff automates that reconstruction in minutes.**

---

## Architecture

```
User Input (repo path + branch)
        ↓
FastAPI Orchestrator
        ↓ asyncio.gather (all 3 run in PARALLEL)
┌───────────────────────────────────────────────────┐
│  Subagent 1          Subagent 2       Subagent 3  │
│  In-Flight State     Commit           Doc/Reality  │
│  Analyzer            Archaeologist    Drift Analyzer│
└───────────────────────────────────────────────────┘
        ↓ (raw JSON × 3)
LLM Summarizer — Ollama (qwen2.5-coder:14b)
        ↓ (plain-English summaries × 3)
Synthesizer Agent — Ollama (final doc)
        ↓
Streamlit UI — renders 11-section Markdown document
```

---

## Project Structure

```
devhandoff/
├── backend/
│   ├── main.py                  # FastAPI app + orchestrator
│   ├── subagent_inflight.py     # Subagent 1: Git diff, GitHub issues, test runner
│   ├── subagent_archaeologist.py# Subagent 2: git log + git blame archaeology
│   ├── subagent_drift.py        # Subagent 3: AST analysis + doc drift + TODOs
│   ├── llm_client.py            # Ollama REST client (local LLM only)
│   └── synthesizer.py           # Final synthesis + per-section regeneration
├── frontend/
│   └── app.py                   # Streamlit UI with parallel progress indicators
├── demo-repo/                   # Sample messy repo for live demos
│   ├── main.py                  # TaskFlow API entry point
│   ├── models.py                # SQLAlchemy models (with intentional drift)
│   ├── api/tasks.py             # Task CRUD routes (with TODOs/FIXMEs)
│   ├── api/users.py             # User auth (insecure MD5, flagged)
│   ├── notifications.py         # Stubbed email notifications
│   ├── middleware.py            # Untracked WIP file
│   └── tests/test_api.py        # Partial test coverage
├── requirements.txt
├── .gitignore
├── .bobignore
└── README.md
```

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.11+ | [python.org](https://python.org) |
| Git | any | pre-installed on most systems |
| Groq API key | free | [console.groq.com](https://console.groq.com) |

---

## Setup & Installation

```bash
# 1. Clone / navigate to this project
cd devhandoff

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install all dependencies
pip install -r requirements.txt

# 4. Set your Groq API key (required for LLM summaries)
#    Copy .env.example to .env and fill in — never commit .env
cp .env.example .env
# Then set: GROQ_API_KEY=gsk_...
```

---

## Running the Application

### Start the backend (Terminal 1)

```bash
uvicorn backend.main:app --reload --port 8000
```

### Start the frontend (Terminal 2)

```bash
streamlit run frontend/app.py
```

Then open **http://localhost:8501** in your browser.

---

## Running the Demo

Point DevHandoff at the included demo repo to see it work immediately:

1. Enter the **full path** to `demo-repo/` in the Repository Path field  
   (e.g. `C:\Users\you\devhandoff\demo-repo` on Windows or `/home/you/devhandoff/demo-repo` on Linux/Mac)
2. Branch: `master`
3. Click **Generate Handoff**

The demo repo (`TaskFlow API`) was designed to trigger every subagent:
- **Staged changes** to `models.py` (WIP Tag model)
- **Unstaged changes** to `notifications.py`
- **Untracked file** `middleware.py` (never committed)
- **7 TODO/FIXME/XXX markers** across the codebase
- **Documentation drift** — `notify_assignee` and `send_email_notification` are in README but not fully implemented
- **Insecure MD5 hashing** flagged with XXX comment
- **Partial test coverage** with TODO comments for missing tests

---

## Environment Variables

Copy `.env.example` to `.env` (never commit `.env`):

```bash
# Required: Groq API key for LLM summaries
# Get a free key at: https://console.groq.com
GROQ_API_KEY=gsk_your_key_here

# Optional: model override (default: llama3-8b-8192)
# GROQ_MODEL_ID=llama-3.1-70b-versatile

# Optional: GitHub Personal Access Token for fetching linked issues/PRs
# GITHUB_TOKEN=ghp_your_token_here
```

The system works without a GitHub token — issue fetching gracefully falls back to
a "no GitHub remote detected" note in the output.

---

## API Reference

### `POST /generate-handoff`

```json
{
  "repo_path": "/absolute/path/to/repo",
  "branch": "main"
}
```

**Response:**
```json
{
  "markdown": "## Context\n\n...",
  "subagent_statuses": [
    {"name": "In-Flight State Analyzer", "status": "done", "duration_seconds": 1.2, "summary": "..."},
    {"name": "Commit Archaeologist", "status": "done", "duration_seconds": 0.8, "summary": "..."},
    {"name": "Doc/Reality Drift Analyzer", "status": "done", "duration_seconds": 0.5, "summary": "..."}
  ],
  "total_duration_seconds": 42.7,
  "raw": { "inflight": {...}, "archaeologist": {...}, "drift": {...} }
}
```

### `POST /regenerate-section`

Re-synthesizes a single section using cached subagent data — used by
the "🔄" buttons in the Streamlit UI.

### `GET /health`

Returns `{"status": "ok"}`.

---

## The 11 Handoff Sections

The generated document always contains exactly these sections in order:

1. **Context** — What project, what branch, what was the developer working on
2. **What Changed** — Uncommitted and recently committed file changes
3. **Why** — Rationale from commit messages and linked GitHub issues
4. **Current Implementation** — What's actually built so far
5. **Known Problems** — Failing tests, bugs, doc drift
6. **Unfinished Work** — TODOs, open PRs, half-finished logic
7. **Key Files** — Most important files to read first
8. **Dependencies** — Libraries, services, external systems
9. **Tests to Run** — Exact commands + critical test paths
10. **Risks** — What could break, fragile assumptions, no test coverage
11. **Recommended Next Actions** — Ranked by priority/risk

---

## How IBM Bob Was Used

IBM Bob (AI developer assistant) was used throughout this project:

| Module | Bob's Role |
|--------|-----------|
| `backend/subagent_inflight.py` | Designed the GitHub API fallback logic and test runner auto-detection |
| `backend/subagent_archaeologist.py` | Structured the git blame + commit log archaeology pipeline |
| `backend/subagent_drift.py` | Designed the AST extraction + doc-vs-code diff algorithm |
| `backend/llm_client.py` | Scaffolded the async Ollama streaming client with model fallback |
| `backend/synthesizer.py` | Crafted the synthesis mega-prompt with exact section ordering |
| `backend/main.py` | Orchestrated `asyncio.gather` concurrency pattern |
| `frontend/app.py` | Designed the three independent per-subagent progress cards UI |
| `demo-repo/` | Generated realistic messy code with authentic TODOs and drift |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `❌ Cannot connect to backend` | Run `uvicorn backend.main:app --reload --port 8000` |
| `⚠️ Groq not configured` | Set `GROQ_API_KEY` in `.env` |
| `⚠️ Groq returned HTTP 401` | Invalid API key — check `GROQ_API_KEY` |
| Generation takes too long | Switch to `GROQ_MODEL_ID=llama3-8b-8192` (faster) |
| GitHub issues not fetching | Add `GITHUB_TOKEN` to `.env`, or it gracefully falls back |
| `InvalidGitRepositoryError` | Path must point to a directory containing a `.git` folder |

---

## License

MIT
