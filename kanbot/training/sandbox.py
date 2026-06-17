"""Phase B: execution-grounded reward via a throwaway git worktree.

Replays a candidate workflow against a *copy* of the session's repo at its
pre-session state, then diffs the result against what the session actually
produced. The agent runs in WRITE mode here — but ONLY ever inside the disposable
worktree, never the user's real checkout, and the worktree is force-removed after.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple


def _git(cwd: str, *args: str, timeout: int = 60) -> Tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def is_git(cwd: str) -> bool:
    return bool(cwd) and os.path.isdir(cwd) and _git(cwd, "rev-parse", "--git-dir")[0] == 0


def _commit_before(cwd: str, ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    rc, out = _git(cwd, "rev-list", "-1", f"--before=@{int(ts)}", "HEAD")
    return out.strip() or None


def session_diff(cwd: str, started_at: Optional[float], mtime: Optional[float],
                 max_chars: int = 8000) -> str:
    """What the session actually changed: the diff between the commit just before
    it started and the commit just after it ended."""
    if not is_git(cwd):
        return ""
    pre = _commit_before(cwd, started_at)
    post = None
    if mtime:
        rc, out = _git(cwd, "rev-list", "-1", f"--before=@{int(mtime) + 1800}", "HEAD")
        post = out.strip() or None
    if pre and post and pre != post:
        rc, d = _git(cwd, "diff", "--stat", f"{pre}..{post}")
        rc2, full = _git(cwd, "diff", f"{pre}..{post}")
        if full.strip():
            return (d + "\n\n" + full)[:max_chars]
    return ""


def make_worktree(cwd: str, started_at: Optional[float] = None) -> Optional[str]:
    """Disposable detached worktree at the session's pre-state (or HEAD)."""
    if not is_git(cwd):
        return None
    ref = _commit_before(cwd, started_at) or "HEAD"
    base = tempfile.mkdtemp(prefix="kanbot-replay-")
    wt = os.path.join(base, "wt")
    rc, _ = _git(cwd, "worktree", "add", "--detach", wt, ref)
    if rc != 0:
        shutil.rmtree(base, ignore_errors=True)
        return None
    return wt


def worktree_diff(wt: str, max_chars: int = 8000) -> str:
    _git(wt, "add", "-A")
    rc, out = _git(wt, "diff", "--cached")
    return out[:max_chars]


def cleanup_worktree(cwd: str, wt: str) -> None:
    _git(cwd, "worktree", "remove", "--force", wt)
    shutil.rmtree(os.path.dirname(wt), ignore_errors=True)


def replay_workflow(template: Dict[str, Any], cwd: str, available: Optional[List[str]],
                    started_at: Optional[float] = None,
                    timeout_per_step: int = 300) -> Optional[str]:
    """Run the workflow's steps WRITE-mode in a throwaway worktree; return the
    produced diff (or None if the repo can't be sandboxed). Caller compares it to
    session_diff(). Expensive — one real agent run per step."""
    from ..distill import run_agent_text
    wt = make_worktree(cwd, started_at)
    if not wt:
        return None
    try:
        prev = ""
        for st in (template.get("steps") or [])[:6]:
            prompt = str(st.get("prompt") or "")
            if not prompt:
                continue
            if prev and st.get("carry_context"):
                prompt += "\n\n--- previous step output ---\n" + prev[-1500:]
            prev = run_agent_text(prompt, available, wt, timeout_per_step, write=True)
        return worktree_diff(wt)
    finally:
        cleanup_worktree(cwd, wt)
