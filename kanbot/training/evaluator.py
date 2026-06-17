"""Score a distilled workflow against the session it came from — the reward the
self-improvement loop hill-climbs. LLM-as-judge, grounded in the repo + the real
outcome (Phase A). Sandbox replay (Phase B) plugs in via `sandbox=...`."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..distill import run_agent_json
from .groundtruth import session_outcome

JUDGE_PROMPT = """You are auditing a candidate AUTOMATION — a reusable multi-step \
workflow distilled from a past coding session. Judge it like a skeptical staff \
engineer. You are running inside the session's repo; read code to verify claims.

CANDIDATE WORKFLOW:
%(wf)s

WHAT THE ORIGINAL SESSION ACTUALLY ACCOMPLISHED (ground truth):
%(outcome)s

Prompting reduction this workflow claims: ~%(reduction)sx (it replaces the whole \
conversation with one TARGET input).

Score 0-100 overall, plus sub-scores:
- fidelity: does its method reflect what actually had to be done here (grounded, \
not hallucinated against the real repo)?
- reusability: would it work on the NEXT similar task from just its TARGET, with \
no memory of this chat?
- takeaways: does it bake in the real gotchas/standards, or is it generic filler?
Penalize hallucinated steps, vagueness, one-off specifics, and any step a fresh \
agent couldn't execute. A great workflow earns its prompting-reduction claim.

Return ONLY JSON, nothing else:
{"score": <0-100>, "fidelity": <0-100>, "reusability": <0-100>, \
"takeaways": <0-100>, "verdict": "keep"|"revise"|"reject", \
"critique": "1-2 sentences on the single biggest thing to improve"}"""


def _i(v: Any, lo: int = 0, hi: int = 100) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return 0


def evaluate_workflow(template: Dict[str, Any], session: Dict[str, Any],
                      available: Optional[List[str]] = None,
                      reduction: int = 0, timeout: int = 240) -> Optional[Dict[str, Any]]:
    """Judge one workflow against one session. Returns a score dict or None if no
    agent / unparseable."""
    outcome = session_outcome(session) or \
        "(no git ground truth — judge on the workflow's internal quality and the transcript intent only)"
    wf = json.dumps({
        "name": template.get("name"),
        "description": template.get("description"),
        "steps": [{"name": s.get("name"), "prompt": s.get("prompt")}
                  for s in (template.get("steps") or [])],
    }, indent=2)[:5000]
    prompt = JUDGE_PROMPT % {"wf": wf, "outcome": outcome[:4500], "reduction": reduction or "?"}
    data, by = run_agent_json(prompt, available, session.get("cwd", ""), timeout)
    if not isinstance(data, dict):
        return None
    return {
        "score": _i(data.get("score")),
        "breakdown": {
            "fidelity": _i(data.get("fidelity")),
            "reusability": _i(data.get("reusability")),
            "takeaways": _i(data.get("takeaways")),
            "verdict": str(data.get("verdict") or ""),
            "prompting_reduction": int(reduction or 0),
        },
        "critique": str(data.get("critique") or ""),
        "critic": by or "",
    }
