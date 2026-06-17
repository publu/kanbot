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

META_PROMPT = """You are mining a developer's past coding-agent session for \
REUSABLE PLAYBOOKS. The point is NOT to replay this one task with the names \
swapped out — it's to capture the GENERALIZABLE METHOD and the hard-won \
TAKEAWAYS so the workflow makes the NEXT, different-but-similar task go better.

The text below is a TRANSCRIPT: messy, conversational, full of dead ends and \
meta-commentary aimed at the assistant ("stop telling me what to do", "GIVE ME \
THE LAST PART", "ok that's good i guess"). IGNORE the noise. Mine the substance: \
what was actually built/fixed, HOW it was approached, and what was LEARNED.

STEP 1 — FIND THE OBJECTIVES. One session often mixes several unrelated goals. \
Split on real topic shifts; keep short follow-ups ("make sure it has tests") with \
the objective they belong to. If the material is just discussion/brainstorming \
with no real engineering method to extract, return an empty workflows array.

STEP 2 — CLIMB THE ABSTRACTION LADDER. For each objective ask: "what CLASS of \
task is this an instance of?" Write the workflow for the CLASS, not the instance.
- "sort this $ balances table"        -> a method for "add correct typed/numeric-aware sorting to any data table"
- "fix the veAERO rewards widget"     -> "diagnose & repair a data-fetching component by diffing it against a working one"
- "wire Hermes to Claude via my plan" -> "route a runner to a CLI agent through subscription auth instead of API keys"
Generalized does NOT mean vague — keep it concrete and runnable. When the pattern \
needs a specific target each run, make the FIRST step state it as a fill-in, e.g. \
"TARGET: <the table/component/feature to apply this to — fill in before running>".

STEP 3 — BAKE IN THE TAKEAWAYS. The session learned things: the gotcha that \
wasted time, the approach that finally worked, why a choice was made. Carry those \
forward so the next run doesn't rediscover them. Put them as concrete guidance \
INSIDE the step prompts ("Method: …", "Watch out for: …", "Prefer X over Y \
because …"), and lead the workflow `description` with the single most important \
takeaway.

STEP 4 — STRUCTURE each workflow as 3-6 ordered steps. Each step's `prompt` is a \
self-contained instruction for a FRESH agent with no memory of this chat — say \
what to do, the method, the gotchas, and how to verify. Steps hand off via files \
(PLAN.md / NOTES.md). For iterative work set `loop_max` (e.g. 20) and a \
`loop_until` shell predicate (e.g. `pytest -q`). `carry_context` true when a step \
needs the previous step's output.

NAMING: `name` is the PATTERN (the class of task), short and imperative — not the \
one-off and not "automation 1". `description` is one line that leads with the key \
takeaway.

Return ONLY a JSON object, no prose and no markdown fences:
{"workflows": [{"name": str, "description": str, "steps": [{"name": str, \
"prompt": str, "loop_max": int, "loop_until": str, "carry_context": bool, \
"continue_on_fail": bool}]}]}

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


def distill_workflows(template: Dict[str, Any], available: Optional[List[str]] = None,
                      timeout: int = 180) -> List[Dict[str, Any]]:
    """Turn a raw draft into one OR MORE clean reusable workflows using any
    available agent. A messy session covering several objectives comes back as
    several clearly-named workflows. Returns [] if no agent / it failed."""
    spec = pick_agent(available)
    if not spec:
        return []
    turns = [str(s.get("prompt") or "").strip() for s in template.get("steps", [])]
    turns = [t for t in turns if t]
    if not turns:
        return []
    ctx = str(template.get("_context") or "").strip()
    body = ""
    if ctx:
        body += f"THE SESSION'S OPENING REQUEST (the real goal): {ctx[:800]}\n\n"
    body += "LATER LINES FROM THE TRANSCRIPT (mostly noise — mine for intent):\n"
    body += "\n".join(f"- {t}" for t in turns)
    prompt = META_PROMPT % body[:6500]
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
        return []
    data = _extract_json(proc.stdout or "")
    if not data:
        return []
    raw = data.get("workflows") if isinstance(data.get("workflows"), list) else [data]
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        norm = _normalize(item, template)
        if norm:
            norm["_distilled_by"] = spec.name
            out.append(norm)
    return out


def distill_template(template: Dict[str, Any], available: Optional[List[str]] = None,
                     timeout: int = 180) -> Optional[Dict[str, Any]]:
    """Back-compat: first distilled workflow only."""
    out = distill_workflows(template, available, timeout)
    return out[0] if out else None
