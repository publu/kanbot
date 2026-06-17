"""Recover what a session actually accomplished, from its repo — the ground
truth an evaluator scores a distilled workflow against."""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict


def _git(cwd: str, *args: str, timeout: int = 10) -> str:
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def session_outcome(session: Dict[str, Any], max_chars: int = 5000) -> str:
    """Best-effort 'what was done' from the session's repo: commits in the
    session's time window (or the recent ones) plus a change stat. Empty string
    when there's no usable git ground truth (the caller then judges on the
    workflow's internal quality only)."""
    cwd = (session.get("cwd") or "").strip()
    if not cwd or not os.path.isdir(cwd) or not _git(cwd, "rev-parse", "--git-dir"):
        return ""

    parts = []
    start = session.get("started_at")
    end = session.get("mtime")
    log = ""
    if start and end:
        log = _git(cwd, "log", "--no-merges", "--pretty=format:%h %s",
                   f"--since=@{int(start) - 60}", f"--until=@{int(end) + 1800}")
    if not log.strip():
        log = _git(cwd, "log", "--no-merges", "--pretty=format:%h %s", "-12")
    if log.strip():
        parts.append("COMMITS (what shipped):\n" + log.strip())

    stat = _git(cwd, "diff", "--stat", "HEAD~5", "HEAD")
    if stat.strip():
        parts.append("RECENT CHANGE STAT (HEAD~5..HEAD):\n" + stat.strip())

    return ("\n\n".join(parts))[:max_chars]
