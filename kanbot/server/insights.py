"""Insight providers.

A tag can be a plain label, or it can carry an `insight` key that pulls live
context "from other spots" and attaches it to any card wearing that tag. Insights
are read-only and fast; they run against the card's working directory (the common
local-first case where the server and runner share a filesystem).

Each provider returns a dict: {ok, title, summary, lines:[...], detail:str}.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _run(args: List[str], cwd: str, timeout: int = 8) -> Optional[str]:
    try:
        out = subprocess.run(
            args, cwd=cwd or None, capture_output=True, text=True, timeout=timeout
        )
        return (out.stdout or out.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _git_root(cwd: str) -> Optional[str]:
    if not cwd or not os.path.isdir(cwd):
        return None
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    return root if root and os.path.isdir(root) else None


def insight_git(card: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    cwd = card.get("cwd") or ""
    root = _git_root(cwd)
    if not root:
        return {"ok": False, "title": "Git", "summary": "Not a git repository",
                "lines": [], "detail": ""}
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root) or "?"
    status = _run(["git", "status", "--porcelain"], root) or ""
    changed = [l for l in status.splitlines() if l.strip()]
    diffstat = _run(["git", "diff", "--stat"], root) or ""
    last = _run(["git", "log", "-1", "--pretty=format:%h %s (%cr)"], root) or ""
    lines = [f"branch: {branch}", f"changed files: {len(changed)}"]
    if last:
        lines.append(f"last commit: {last}")
    return {
        "ok": True,
        "title": "Git",
        "summary": f"{branch} · {len(changed)} changed",
        "lines": lines,
        "detail": (diffstat or status)[:4000],
    }


def insight_files(card: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    cwd = card.get("cwd") or ""
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "title": "Files", "summary": "No working directory",
                "lines": [], "detail": ""}
    try:
        entries = sorted(
            (e for e in os.scandir(cwd) if not e.name.startswith(".")),
            key=lambda e: e.stat().st_mtime, reverse=True,
        )[:12]
    except OSError:
        entries = []
    lines = []
    for e in entries:
        tag = "/" if e.is_dir() else ""
        lines.append(f"{e.name}{tag}")
    return {
        "ok": True,
        "title": "Files",
        "summary": f"{len(lines)} recent",
        "lines": lines,
        "detail": "\n".join(lines),
    }


def insight_command(card: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Run an arbitrary read-only command configured on the tag (e.g. tests, lint)."""
    cmd = cfg.get("command")
    cwd = card.get("cwd") or ""
    if not cmd:
        return {"ok": False, "title": "Command", "summary": "No command configured",
                "lines": [], "detail": "Set config.command on the tag."}
    out = _run(["bash", "-lc", cmd], cwd, timeout=int(cfg.get("timeout", 20)))
    if out is None:
        return {"ok": False, "title": cfg.get("label", "Command"),
                "summary": "failed to run", "lines": [], "detail": ""}
    tail = out.splitlines()[-3:]
    return {
        "ok": True,
        "title": cfg.get("label", "Command"),
        "summary": tail[-1][:60] if tail else "ok",
        "lines": tail,
        "detail": out[-4000:],
    }


PROVIDERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = {
    "git": insight_git,
    "files": insight_files,
    "command": insight_command,
}

PROVIDER_META = [
    {"key": "git", "label": "Git status & diff",
     "description": "Branch, changed files, diffstat for the card's repo."},
    {"key": "files", "label": "Recent files",
     "description": "Most recently modified files in the working directory."},
    {"key": "command", "label": "Custom command",
     "description": "Run a read-only command (tests, lint) and show the tail. Set config.command."},
]


def compute(card: Dict[str, Any], insight_key: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    fn = PROVIDERS.get(insight_key)
    if not fn:
        return {"ok": False, "title": insight_key, "summary": "unknown provider",
                "lines": [], "detail": ""}
    try:
        return fn(card, cfg or {})
    except Exception as e:  # never let an insight crash a request
        return {"ok": False, "title": insight_key, "summary": "error",
                "lines": [str(e)], "detail": str(e)}
