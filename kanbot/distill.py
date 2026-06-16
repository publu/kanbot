"""Distill a raw session-derived workflow into a clean, reusable one.

The heuristic extractor in workflows.py copies a user's raw chat turns verbatim
as steps — useful as a *draft*, but it's literally their transcript, not a
reusable automation. This module runs the user's local `claude` CLI to do the
real work: read the raw turns and synthesize a generalized workflow with short,
guided, standalone step-prompts (the kind that actually work with fresh context),
adding loops where the work was iterative.

No API key needed — it shells out to the same `claude` CLI the runner already
uses. If `claude` isn't installed or the call fails, callers fall back to the
heuristic draft.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

META_PROMPT = """You are turning a developer's past coding-agent session into a \
REUSABLE automation (a workflow) they can run again on similar tasks.

Below are the human instructions from one session, in order. They are messy, \
specific to that moment, and conversational.

Produce a clean, GENERALIZED workflow:
- Distill the real objective. Drop one-off chatter, profanity, and details that \
won't transfer (specific names, ids, "the thing we discussed").
- Write 3-6 STEPS. Each step's `prompt` must be a crisp, self-contained \
instruction that works on its own with a fresh agent (no memory of this chat). \
Keep prompts short and guided — say what to do and how to verify it.
- Steps run top-to-bottom, each a fresh agent run, handing off via files \
(PLAN.md / NOTES.md) in the repo.
- For work that iterates until done (e.g. "make the tests pass"), set \
`loop_max` to a sensible cap (e.g. 20) and optionally `loop_until` to a shell \
predicate that exits 0 when finished (e.g. `pytest -q`).
- `carry_context` true when a step needs the previous step's output.
- Give the workflow a short imperative `name` and a one-line `description`.

Return ONLY a JSON object, no prose and no markdown fences:
{"name": str, "description": str, "steps": [{"name": str, "prompt": str, \
"loop_max": int, "loop_until": str, "carry_context": bool, \
"continue_on_fail": bool}]}

HUMAN INSTRUCTIONS FROM THE SESSION:
%s
"""


def claude_available() -> bool:
    return shutil.which("claude") is not None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first valid JSON object out of arbitrary model output."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _normalize(data: Dict[str, Any], base: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    steps: List[Dict[str, Any]] = []
    for i, st in enumerate(raw_steps[:8]):
        if not isinstance(st, dict):
            continue
        prompt = str(st.get("prompt") or "").strip()
        if not prompt:
            continue
        steps.append({
            "name": str(st.get("name") or f"Step {i + 1}")[:60],
            "prompt": prompt,
            "agent": "", "profile": "", "command": "",
            "loop_max": max(1, int(st.get("loop_max") or 1)),
            "loop_until": str(st.get("loop_until") or ""),
            "carry_context": bool(st.get("carry_context", i > 0)),
            "continue_on_fail": bool(st.get("continue_on_fail", False)),
        })
    if not steps:
        return None
    return {
        "name": str(data.get("name") or base.get("name") or "workflow")[:80],
        "description": str(data.get("description") or base.get("description") or ""),
        "agent": base.get("agent", "auto") or "auto",
        "cwd": base.get("cwd", "") or "",
        "steps": steps,
    }


def distill_template(template: Dict[str, Any], timeout: int = 120) -> Optional[Dict[str, Any]]:
    """Run `claude` to turn a raw draft workflow into a clean reusable one.
    Returns the distilled template, or None if claude is unavailable/failed."""
    if not claude_available():
        return None
    turns = [str(s.get("prompt") or "").strip() for s in template.get("steps", [])]
    turns = [t for t in turns if t]
    if not turns:
        return None
    body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(turns))[:6000]
    prompt = META_PROMPT % body
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--dangerously-skip-permissions"],
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = _extract_json(proc.stdout or "")
    if not data:
        return None
    return _normalize(data, template)
