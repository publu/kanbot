"""The self-improvement pass: over a few real sessions, distill (steered by the
current exemplars) -> evaluate against ground truth -> bank what clears the bar.
Each banked exemplar immediately steers the rest of the pass, so the system
bootstraps off its own wins."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..distill import distill_workflows
from ..runner.discovery import all_user_turns
from .evaluator import evaluate_workflow


def _turns(session: Dict[str, Any]) -> List[str]:
    turns = all_user_turns(session.get("path", ""), session.get("fmt", "claude"))
    if not turns:
        turns = [m.get("text", "") for m in (session.get("tail") or [])
                 if m.get("role") == "user" and m.get("text")]
    return [t for t in turns if t]


def pick_sessions(sessions: List[Dict[str, Any]], k: int = 3) -> List[Dict[str, Any]]:
    """Substantial sessions whose repo still exists (so ground-truth eval works),
    richest first."""
    cand = [s for s in sessions
            if s.get("cwd") and os.path.isdir(s["cwd"])
            and len([m for m in (s.get("tail") or []) if m.get("role") == "user"]) >= 2]
    cand.sort(key=lambda s: int(s.get("turns", 0)), reverse=True)
    return cand[:max(0, k)]


def run_improvement_pass(db, board_id: str, sessions: List[Dict[str, Any]],
                         available: Optional[List[str]], bar: float = 75.0,
                         limit: int = 3) -> List[Dict[str, Any]]:
    """One pass. Returns a per-(session,workflow) summary of scores + banks."""
    exemplars = [e["template"] for e in db.top_exemplars(3, board_id)]
    summary: List[Dict[str, Any]] = []
    for s in pick_sessions(sessions, limit):
        turns = _turns(s)
        if not turns:
            continue
        src = sum(len(t) for t in turns) // 4
        draft = {
            "agent": s.get("agent", "auto") or "auto",
            "cwd": s.get("cwd", "") or "",
            "_context": s.get("title") or s.get("recap") or "",
            "steps": [{"prompt": t} for t in turns],
        }
        wfs = distill_workflows(draft, available, 300, exemplars)
        for w in wfs:
            w["source_tokens"] = src
            res = evaluate_workflow(w, s, available, src // 25)
            if not res:
                continue
            db.log_eval(board_id, s.get("session_id", ""), w.get("name", ""),
                        res["score"], res["breakdown"], res["critique"], res["critic"])
            banked = res["score"] >= bar and res["breakdown"].get("verdict") != "reject"
            if banked:
                db.add_exemplar(board_id, w.get("name", ""),
                                {k: w.get(k) for k in ("name", "description", "agent", "cwd", "steps")},
                                res["score"], src, res["breakdown"])
                # newly-proven exemplar steers the rest of this pass
                exemplars = [e["template"] for e in db.top_exemplars(3, board_id)]
            summary.append({"session": s.get("name"), "workflow": w.get("name"),
                            "score": res["score"], "banked": banked})
    return summary
