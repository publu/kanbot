"""Distill a raw session-derived workflow into a clean, reusable one.

The heuristic extractor in workflows.py copies a user's raw chat turns verbatim
as steps — useful as a *draft*, but it's literally their transcript, not a
reusable automation. This module runs whichever coding-agent CLI is available to
do the real work: read the raw turns and synthesize a generalized workflow with
short, guided, standalone step-prompts (the kind that actually work with fresh
context), adding loops where the work was iterative.

Agent-agnostic: it uses any reasoning agent the connected runners advertise
(claude, codex, glm, gemini, …) — not a hardcoded one — resolving the command
from the shared catalog. No API key needed. If nothing usable is available or
the call fails, callers fall back to the heuristic draft.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from .agents import BUILTIN_BY_NAME, builtin_names

# Agents that can actually reason text -> JSON, best first. `shell` can't, and
# the others have unknown output shapes so they sit at the back.
_PREFERENCE = ["claude", "codex", "glm", "gemini", "cursor-agent", "opencode"]

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


def _candidate_specs(available: Optional[List[str]] = None) -> List[Any]:
    """Resolve usable agents: prefer real reasoning CLIs, restrict to what the
    connected runners advertise (if given), and require the binary on this host."""
    names = available if available else builtin_names()
    ordered = [n for n in _PREFERENCE if n in names]
    ordered += [n for n in names if n not in _PREFERENCE and n != "shell"]
    specs = []
    seen = set()
    for n in ordered:
        spec = BUILTIN_BY_NAME.get(n)
        if spec and spec.name not in seen and shutil.which(spec.bin):
            specs.append(spec); seen.add(spec.name)
    return specs


def pick_agent(available: Optional[List[str]] = None):
    specs = _candidate_specs(available)
    return specs[0] if specs else None


def distill_available(available: Optional[List[str]] = None) -> bool:
    return pick_agent(available) is not None


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


def _argv_for(spec, prompt: str) -> List[str]:
    out = []
    for tok in spec.argv:                       # the agent's headless/auto template
        out.append(tok.replace("{prompt}", prompt).replace("{session_id}", ""))
    return out


def distill_template(template: Dict[str, Any], available: Optional[List[str]] = None,
                     timeout: int = 180) -> Optional[Dict[str, Any]]:
    """Turn a raw draft workflow into a clean reusable one using any available
    agent. Returns the distilled template, or None if none usable / it failed."""
    spec = pick_agent(available)
    if not spec:
        return None
    turns = [str(s.get("prompt") or "").strip() for s in template.get("steps", [])]
    turns = [t for t in turns if t]
    if not turns:
        return None
    body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(turns))[:6000]
    prompt = META_PROMPT % body
    env = os.environ.copy()
    env.update(spec.env)
    try:
        proc = subprocess.run(
            _argv_for(spec, prompt),
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = _extract_json(proc.stdout or "")
    if not data:
        return None
    out = _normalize(data, template)
    if out:
        out["_distilled_by"] = spec.name
    return out
