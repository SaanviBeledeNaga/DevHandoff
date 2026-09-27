"""
DevHandoff - Live fix-verification script
==========================================
Runs all HTTP test cases against the backend at http://localhost:8765
and prints exact status codes + response bodies.
Run with:
    python verify_fixes.py
"""

import os
import sys
import time
import subprocess
import httpx

BASE = "http://localhost:8765"
DEMO_REPO = os.path.abspath("demo-repo")   # guaranteed-existing directory
results = []


def check(label, cond, actual):
    status = "PASS" if cond else "FAIL"
    results.append((label, cond, actual))
    print(f"  [{status}] {label}")
    print(f"         actual: {actual}\n")


env_base = os.environ.copy()
env_base["PYTHONPATH"] = os.path.abspath(".")
srv_base = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "backend.main:app",
     "--port", "8765", "--log-level", "warning"],
    env=env_base,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(3)

# ---------------------------------------------------------------------------
print("=" * 65)
print("TEST A - Path-traversal rejection")
print("=" * 65)
r = httpx.post(
    f"{BASE}/generate-handoff",
    json={"repo_path": "../../etc", "branch": "main"},
    timeout=10,
)
check(
    "repo_path='../../etc' -> 4xx (not 500, not 200)",
    400 <= r.status_code <= 422,
    f"HTTP {r.status_code}  body={r.text[:300]}",
)

# Also test a Windows-style traversal
r2 = httpx.post(
    f"{BASE}/generate-handoff",
    json={"repo_path": "..\\..\\Windows\\System32", "branch": "main"},
    timeout=10,
)
check(
    "repo_path='..\\..\\ traversal' -> 4xx",
    400 <= r2.status_code <= 422,
    f"HTTP {r2.status_code}  body={r2.text[:200]}",
)

# ---------------------------------------------------------------------------
print("=" * 65)
print("TEST B - Auth rejection when DEVHANDOFF_API_KEY is set")
print("=" * 65)

KEY = "test-secret-key-xyz"
env_with_key = os.environ.copy()
env_with_key["DEVHANDOFF_API_KEY"] = KEY
env_with_key["PYTHONPATH"] = os.path.abspath(".")

srv = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "backend.main:app",
     "--port", "8766", "--log-level", "warning"],
    env=env_with_key,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(3)

try:
    # B1: No key -> 401
    r_no_key = httpx.post(
        "http://localhost:8766/generate-handoff",
        json={"repo_path": DEMO_REPO, "branch": "master"},
        timeout=10,
    )
    check(
        "No X-API-Key header -> 401",
        r_no_key.status_code == 401,
        f"HTTP {r_no_key.status_code}  body={r_no_key.text[:200]}",
    )

    # B2: Wrong key -> 401
    r_bad_key = httpx.post(
        "http://localhost:8766/generate-handoff",
        json={"repo_path": DEMO_REPO, "branch": "master"},
        headers={"X-API-Key": "wrong-key"},
        timeout=10,
    )
    check(
        "Wrong X-API-Key -> 401",
        r_bad_key.status_code == 401,
        f"HTTP {r_bad_key.status_code}  body={r_bad_key.text[:200]}",
    )

    # B3: Correct key + invalid repo -> 422 (auth passes, validation fails)
    r_good_key = httpx.post(
        "http://localhost:8766/generate-handoff",
        json={"repo_path": "../../etc", "branch": "master"},
        headers={"X-API-Key": KEY},
        timeout=10,
    )
    check(
        "Correct key + bad repo_path -> 422 (passes auth, fails validation)",
        400 <= r_good_key.status_code <= 422,
        f"HTTP {r_good_key.status_code}  body={r_good_key.text[:200]}",
    )
finally:
    srv.terminate()
    srv.wait(timeout=5)

# ---------------------------------------------------------------------------
print("=" * 65)
print("TEST C - CORS: disallowed vs allowed origin")
print("=" * 65)
r_evil = httpx.options(
    f"{BASE}/generate-handoff",
    headers={
        "Origin": "http://evil.com",
        "Access-Control-Request-Method": "POST",
    },
    timeout=10,
)
acao_evil = r_evil.headers.get("access-control-allow-origin", "<not present>")
check(
    "Origin: evil.com -> ACAO must NOT be 'http://evil.com' or '*'",
    acao_evil not in ("http://evil.com", "*"),
    f"HTTP {r_evil.status_code}  Access-Control-Allow-Origin: {acao_evil}",
)

r_allowed = httpx.options(
    f"{BASE}/generate-handoff",
    headers={
        "Origin": "http://localhost:8501",
        "Access-Control-Request-Method": "POST",
    },
    timeout=10,
)
acao_allowed = r_allowed.headers.get("access-control-allow-origin", "<not present>")
check(
    "Origin: localhost:8501 -> ACAO reflects the allowed origin",
    "localhost:8501" in acao_allowed,
    f"HTTP {r_allowed.status_code}  Access-Control-Allow-Origin: {acao_allowed}",
)
srv_base.terminate()
srv_base.wait(timeout=5)

# ---------------------------------------------------------------------------
print("=" * 65)
print("TEST D - Rate limiting (15 rapid requests -> 429 after threshold)")
print("=" * 65)
# Use a fresh server without API key so auth doesn't interfere
env_rl = os.environ.copy()
env_rl["PYTHONPATH"] = os.path.abspath(".")
srv_rl = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "backend.main:app",
     "--port", "8767", "--log-level", "warning"],
    env=env_rl,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(3)

try:
    statuses = []
    for i in range(15):
        try:
            r = httpx.post(
                "http://localhost:8767/generate-handoff",
                json={"repo_path": "../../invalid", "branch": "main"},
                timeout=5,
            )
            statuses.append(r.status_code)
        except Exception as e:
            statuses.append(f"ERR:{e}")

    has_429 = 429 in statuses
    check(
        "15 rapid requests -> at least one 429 Too Many Requests",
        has_429,
        f"all status codes: {statuses}",
    )
finally:
    srv_rl.terminate()
    srv_rl.wait(timeout=5)

# ---------------------------------------------------------------------------
print("=" * 65)
print("TEST E - watsonx.ai fallback (B5/B6): bad credentials -> graceful 200")
print("=" * 65)
script = f"""
import sys, asyncio, os
sys.path.insert(0, r"{os.path.abspath(".")}")

# Override env vars to point at an unreachable watsonx endpoint
os.environ["WATSONX_API_KEY"] = "invalid-key-for-test"
os.environ["WATSONX_PROJECT_ID"] = "00000000-0000-0000-0000-000000000000"
os.environ["WATSONX_URL"] = "https://127.0.0.1:19999"

import backend.llm_client as lc
# Re-read env vars after override (module-level vars already set, patch directly)
lc._WX_API_KEY = "invalid-key-for-test"
lc._WX_PROJECT_ID = "00000000-0000-0000-0000-000000000000"
lc._WX_URL = "https://127.0.0.1:19999"
lc._WX_GENERATE_URL = "https://127.0.0.1:19999/ml/v1/text/generation?version=2023-05-29"

from backend.main import orchestrate

async def run():
    result = await orchestrate(r"{DEMO_REPO}", "master")
    md = result.markdown
    print("STATUS:OK")
    print("MARKDOWN_LEN:" + str(len(md)))
    if md:
        print("MARKDOWN_NONEMPTY:TRUE")
    else:
        print("MARKDOWN_NONEMPTY:FALSE")
    graceful = (
        "Synthesis Unavailable" in md
        or "## Context" in md
        or "Summary unavailable" in md
        or "watsonx.ai" in md
        or "LLM unavailable" in md
    )
    print("GRACEFUL:" + str(graceful).upper())
    print("SNIPPET:" + md[:150].replace("\\n", " "))

asyncio.run(run())
"""

proc = subprocess.run(
    [sys.executable, "-c", script],
    capture_output=True,
    text=True,
    timeout=180,
)
out = proc.stdout
err = proc.stderr
ok_status   = "STATUS:OK" in out
ok_nonempty = "MARKDOWN_NONEMPTY:TRUE" in out
ok_graceful = "GRACEFUL:TRUE" in out

check(
    "Bad watsonx creds -> orchestrate() completes without raising",
    ok_status,
    f"stdout={out[:400]}  stderr={err[:200]}",
)
check(
    "Bad watsonx creds -> markdown is non-empty",
    ok_nonempty,
    f"MARKDOWN_NONEMPTY={'TRUE' if ok_nonempty else 'FALSE'}  len={out}",
)
check(
    "Bad watsonx creds -> content is a graceful fallback (not a crash/500)",
    ok_graceful,
    f"GRACEFUL={'TRUE' if ok_graceful else 'FALSE'}",
)

# ---------------------------------------------------------------------------
print("=" * 65)
print("SUMMARY")
print("=" * 65)
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print(f"  {passed} passed  /  {failed} failed  /  {len(results)} total\n")
if failed:
    print("FAILED checks:")
    for label, ok, actual in results:
        if not ok:
            print(f"  FAIL: {label}")
            print(f"        {actual}")
    sys.exit(1)
else:
    print("All checks passed.")
