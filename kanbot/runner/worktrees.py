"""Per-task git worktrees — the "Devin" isolation model.

Each card that opts into isolation runs on its own branch in a throwaway
worktree that shares the repo's object store, so parallel agents never collide
in one checkout and you can review a card's diff before it touches your branch.

Everything here is best-effort: if `cwd` isn't a git repo, or a git command
fails, the caller falls back to running in place. A run must never fail because
a worktree couldn't be made.

ponytail: plain `git worktree` over any library — git already does all of this.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional, Tuple


def _git(cwd: str, *args: str, timeout: int = 120) -> Tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _toplevel(cwd: str) -> Optional[str]:
    if not (cwd and os.path.isdir(cwd)):
        return None
    rc, out = _git(cwd, "rev-parse", "--show-toplevel")
    return out if rc == 0 and out else None


def branch_name(card_id: str) -> str:
    return f"deckhand/{card_id[:8]}"


def prepare(cwd: str, card_id: str) -> Optional[dict]:
    """Create (or reuse, on a spree/resume re-run) a worktree on branch
    ``deckhand/<card>`` forked from the repo's current HEAD. Returns
    ``{workdir, branch, base}`` or None if cwd isn't a git repo / it failed."""
    top = _toplevel(cwd)
    if not top:
        return None
    rc, base = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    base = base if rc == 0 and base and base != "HEAD" else "HEAD"
    branch = branch_name(card_id)
    # Worktrees live beside the repo so they share its object store cheaply.
    wt = os.path.join(os.path.dirname(top), ".deckhand-worktrees",
                      os.path.basename(top) + "-" + card_id[:8])
    if os.path.isdir(wt):                       # already made — reuse it
        return {"workdir": wt, "branch": branch, "base": base}
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    # -B creates or resets the branch to HEAD, then checks it out in the worktree.
    rc, out = _git(cwd, "worktree", "add", "-B", branch, wt, "HEAD")
    if rc != 0:
        return None
    return {"workdir": wt, "branch": branch, "base": base}


def commit_all(workdir: str, message: str) -> None:
    """Commit whatever the agent left in the worktree so the branch is mergeable
    (a single, non-loop run never hits the worker's checkpoint path)."""
    rc, dirty = _git(workdir, "status", "--porcelain")
    if rc == 0 and dirty.strip():
        _git(workdir, "add", "-A")
        _git(workdir, "-c", "user.email=deckhand@local", "-c", "user.name=deckhand",
             "commit", "-q", "-m", message)


def summary(workdir: str, base: str, max_chars: int = 1200) -> str:
    """A short ``git diff --stat`` of the branch's work vs the base it forked."""
    rc, out = _git(workdir, "diff", "--stat", f"{base}...HEAD")
    if rc != 0 or not out.strip():
        rc, out = _git(workdir, "diff", "--stat")   # nothing committed yet
    return out[:max_chars]


def merge(cwd: str, branch: str, workdir: str) -> dict:
    """Merge the card's branch back into the real repo's current branch, then
    drop the worktree + branch. Returns ``{state, output}`` where state is
    ``merged`` | ``conflict`` | ``error``. A conflicted merge is aborted so the
    real checkout is left clean."""
    top = _toplevel(cwd)
    if not top:
        return {"state": "error", "output": "not a git repository"}
    rc, out = _git(top, "merge", "--no-ff", "-m", f"deckhand: merge {branch}", branch)
    if rc != 0:
        _git(top, "merge", "--abort")
        return {"state": "conflict", "output": out[-800:]}
    # A branch checked out in a worktree can't be deleted — remove the worktree first.
    if workdir:
        _git(top, "worktree", "remove", "--force", workdir)
    _git(top, "branch", "-D", branch)
    return {"state": "merged", "output": out[-800:]}


def demo() -> None:
    """Self-check: make a repo, isolate a card, commit in the worktree, merge back."""
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="deckhand-wt-test-")
    try:
        repo = os.path.join(d, "repo")
        os.makedirs(repo)
        for a in (("init", "-q"), ("config", "user.email", "t@t"),
                  ("config", "user.name", "t")):
            _git(repo, *a)
        open(os.path.join(repo, "f.txt"), "w").write("base\n")
        _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")

        wt = prepare(repo, "card1234abcd")
        assert wt and os.path.isdir(wt["workdir"]), "worktree not created"
        assert wt["branch"] == "deckhand/card1234", wt["branch"]
        # agent leaves uncommitted work in the isolated checkout
        open(os.path.join(wt["workdir"], "new.txt"), "w").write("work\n")
        commit_all(wt["workdir"], "session work")
        assert "new.txt" in summary(wt["workdir"], wt["base"]), "diffstat missing file"
        # the real repo must NOT see the file until merge
        assert not os.path.exists(os.path.join(repo, "new.txt")), "leaked before merge"

        res = merge(repo, wt["branch"], wt["workdir"])
        assert res["state"] == "merged", res
        assert os.path.exists(os.path.join(repo, "new.txt")), "merge didn't land"
        assert not os.path.isdir(wt["workdir"]), "worktree not cleaned up"

        # non-git dir degrades to None / error, never raises
        assert prepare(d, "x") is None
        assert merge(d, "b", "")["state"] == "error"
        print("worktrees demo OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    demo()
