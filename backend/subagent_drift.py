"""
DevHandoff — Doc/Reality Drift Analyzer (Subagent 3)
=====================================================
Bob-assisted module: uses Python's ast module to extract real code
signatures/docstrings, then diffs them against README/docs to surface
documentation drift.  Also regex-scans for TODO/FIXME/XXX debt markers.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any


# Comment debt markers to hunt for
_TODO_RE = re.compile(r"#\s*(TODO|FIXME|XXX|HACK|BUG|NOTE)\b[:\s]*(.*)", re.IGNORECASE)

# Extensions considered "documentation"
_DOC_EXTENSIONS = {".md", ".rst", ".txt"}

# Python files to skip
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", "dist", "build"}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _extract_python_signatures(root: Path) -> list[dict]:
    """
    Walk all *.py files under root and extract function/class signatures
    with their docstrings (first line only, truncated to 200 chars).
    """
    signatures: list[dict] = []

    for py_file in root.rglob("*.py"):
        # Skip ignored dirs
        if any(part in _SKIP_DIRS for part in py_file.parts):
            continue

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, OSError, PermissionError):
            # Skip files that cannot be read (permissions) or parsed (syntax errors)
            continue

        rel_path = str(py_file.relative_to(root))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                docstring = ast.get_docstring(node) or ""
                first_line = docstring.split("\n")[0][:200] if docstring else ""

                # Build argument list for functions
                args_repr = ""
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in node.args.args]
                    args_repr = f"({', '.join(args)})"

                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                signatures.append(
                    {
                        "file": rel_path,
                        "line": node.lineno,
                        "kind": kind,
                        "name": node.name,
                        "signature": f"{node.name}{args_repr}",
                        "docstring_first_line": first_line,
                        "has_docstring": bool(docstring),
                    }
                )

    return signatures


# ---------------------------------------------------------------------------
# Documentation surface extraction
# ---------------------------------------------------------------------------

def _extract_documented_names(root: Path) -> set[str]:
    """
    Scan README.md, /docs/, and any .md/.rst files for names that look like
    function/class references (e.g. `function_name`, `ClassName`, code blocks).
    Returns a set of mentioned identifiers.
    """
    mentioned: set[str] = set()
    # Inline code or code-block identifiers
    inline_code_re = re.compile(r"`([A-Za-z_]\w*)`")
    heading_words_re = re.compile(r"[A-Za-z_]\w+")

    doc_files: list[Path] = []
    for ext in _DOC_EXTENSIONS:
        doc_files.extend(root.rglob(f"*{ext}"))

    for doc_file in doc_files:
        if any(part in _SKIP_DIRS for part in doc_file.parts):
            continue
        try:
            text = doc_file.read_text(encoding="utf-8", errors="replace")
            mentioned.update(inline_code_re.findall(text))
            # Also grab words from ## headings
            for line in text.splitlines():
                if line.startswith("#"):
                    mentioned.update(heading_words_re.findall(line))
        except Exception:
            pass

    return mentioned


# ---------------------------------------------------------------------------
# TODO / FIXME scanner
# ---------------------------------------------------------------------------

def _scan_todos(root: Path) -> list[dict]:
    """
    Regex-scan all source files for TODO/FIXME/XXX/HACK/BUG markers.
    Returns list of {file, line, kind, text}.
    """
    todos: list[dict] = []
    # Scan Python, JS/TS, and common config formats
    patterns = ["*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.yaml", "*.yml", "*.json", "*.sh"]

    scanned_files: set[Path] = set()
    for pattern in patterns:
        for f in root.rglob(pattern):
            if f not in scanned_files and not any(part in _SKIP_DIRS for part in f.parts):
                scanned_files.add(f)

    for source_file in scanned_files:
        try:
            for lineno, line in enumerate(
                source_file.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                m = _TODO_RE.search(line)
                if m:
                    todos.append(
                        {
                            "file": str(source_file.relative_to(root)),
                            "line": lineno,
                            "kind": m.group(1).upper(),
                            "text": m.group(2).strip()[:200],
                        }
                    )
        except Exception:
            pass

    return sorted(todos, key=lambda x: (x["file"], x["line"]))


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def _detect_drift(
    signatures: list[dict], documented_names: set[str]
) -> list[dict]:
    """
    Compare actual code signatures against names mentioned in documentation.
    Flags:
      - undocumented_in_code: public function/class with no docstring
      - mentioned_but_missing: name appears in docs but not in codebase
    Returns a list of drift items.
    """
    drift: list[dict] = []

    code_names = {s["name"] for s in signatures}

    # Public symbols missing docstrings
    for sig in signatures:
        if not sig["name"].startswith("_") and not sig["has_docstring"]:
            drift.append(
                {
                    "kind": "missing_docstring",
                    "name": sig["name"],
                    "file": sig["file"],
                    "line": sig["line"],
                    "detail": f"Public {sig['kind']} `{sig['name']}` has no docstring",
                }
            )

    # Names in docs that don't exist in code
    for name in documented_names:
        if len(name) > 3 and name not in code_names and not name[0].isupper():
            # heuristic: skip short words and class-like names to reduce noise
            drift.append(
                {
                    "kind": "mentioned_but_missing_in_code",
                    "name": name,
                    "file": "docs",
                    "line": None,
                    "detail": f"`{name}` is referenced in documentation but not found in source code",
                }
            )

    return drift


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def analyze(repo_path: str, branch: str) -> dict[str, Any]:
    """
    Subagent 3 — Doc/Reality Drift Analyzer.

    NOTE: Drift analysis scans the working-tree files on disk.  It does NOT
    checkout the requested branch — doing so would clobber unstaged changes that
    Subagent 1 is simultaneously inspecting.  The ``branch`` parameter is
    recorded in the output so consumers know which branch was requested.

    Returns:
      - signatures: all public function/class signatures with docstring status
      - drift_items: list of mismatches between docs and code
      - todos: all TODO/FIXME/XXX markers in the codebase
      - stats: aggregate counts
      - branch_note: explains that working-tree (not branch HEAD) was scanned
    """
    root = Path(repo_path)

    signatures = _extract_python_signatures(root)
    documented_names = _extract_documented_names(root)
    drift_items = _detect_drift(signatures, documented_names)
    todos = _scan_todos(root)

    return {
        "signatures": signatures,
        "drift_items": drift_items,
        "todos": todos,
        "stats": {
            "total_public_symbols": sum(1 for s in signatures if not s["name"].startswith("_")),
            "symbols_with_docstrings": sum(1 for s in signatures if s["has_docstring"]),
            "total_todos": len(todos),
            "total_drift_items": len(drift_items),
        },
        "branch_note": (
            f"Scanned working-tree files (branch '{branch}' requested). "
            "No git checkout performed to preserve uncommitted changes."
        ),
    }
