"""Workflow templating and extraction.

A workflow is an ordered chain of agent steps. This module holds two things that
make workflows easy to *create* rather than just run:

  * STARTER_TEMPLATES — a small library of battle-tested long-run shapes
    (plan -> build -> test-loop -> review -> report) the UI can instantiate.
  * extract_from_session — turn a real Claude/Codex transcript into a draft
    workflow, so a session you already ran becomes a repeatable template.

Templates are plain dicts of the same shape the DB import accepts:
    {name, description, agent, cwd, steps:[{name, prompt, agent, profile,
     command, loop_max, loop_until, carry_context, continue_on_fail}]}
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _step(name: str, prompt: str, **kw) -> Dict[str, Any]:
    return {
        "name": name,
        "prompt": prompt,
        "agent": kw.get("agent", ""),
        "profile": kw.get("profile", ""),
        "command": kw.get("command", ""),
        "loop_max": int(kw.get("loop_max", 1)),
        "loop_until": kw.get("loop_until", ""),
        "carry_context": kw.get("carry_context", True),
        "continue_on_fail": kw.get("continue_on_fail", False),
    }


# Steps lean on file-based handoff (PLAN.md / NOTES.md in the repo) because each
# step runs with fresh context — durable on-disk notes survive between steps and
# across the hours a long run takes.
STARTER_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "Ship a feature",
        "description": "Plan, build with a test loop, review, and report. The "
                       "backbone long-run workflow: hand it a feature and walk away.",
        "agent": "auto",
        "cwd": "",
        "steps": [
            _step("Plan",
                  "Read the codebase and write a concrete implementation plan for the "
                  "task below into PLAN.md (files to touch, order of work, how you'll "
                  "verify each part). Do not write feature code yet.\n\nTASK:\n",
                  loop_max=1),
            _step("Build until tests pass",
                  "Implement the plan in PLAN.md. Make small, verifiable changes and run "
                  "the test suite as you go. Keep a running log in NOTES.md. Stop only "
                  "when the full test suite passes.",
                  loop_max=20, loop_until="", carry_context=True),
            _step("Review & harden",
                  "Review the diff you just produced for correctness, edge cases, and "
                  "dead code. Fix what you find. Re-run the tests.",
                  loop_max=3, carry_context=True),
            _step("Report",
                  "Write a concise SUMMARY.md: what changed, why, how it was verified, "
                  "and anything left for a human to check.",
                  loop_max=1, carry_context=True, continue_on_fail=True),
        ],
    },
    {
        "name": "Harden until green",
        "description": "Drive an existing change to a fully passing test suite, then "
                       "clean up. Good for finishing a half-done branch overnight.",
        "agent": "auto",
        "cwd": "",
        "steps": [
            _step("Make it green",
                  "Run the test suite. Diagnose and fix every failure, looping with "
                  "fresh context until everything passes. Note tricky fixes in NOTES.md.",
                  loop_max=30, loop_until="", carry_context=True),
            _step("Clean up",
                  "Remove debug code, tighten naming, and ensure the change reads like "
                  "the surrounding code. Re-run tests to confirm still green.",
                  loop_max=3, carry_context=True),
        ],
    },
    {
        "name": "Deep refactor",
        "description": "Map the target, refactor in safe slices, verify continuously.",
        "agent": "auto",
        "cwd": "",
        "steps": [
            _step("Map the target",
                  "Map the area to refactor: write the current structure, the desired "
                  "structure, and a slice-by-slice migration plan into PLAN.md. No code "
                  "changes yet.\n\nTARGET:\n", loop_max=1),
            _step("Refactor in slices",
                  "Execute PLAN.md one slice at a time. After each slice, run the tests "
                  "and commit-worthy-check before moving on. Loop until the plan is done "
                  "and tests pass.",
                  loop_max=25, carry_context=True),
            _step("Verify & document",
                  "Confirm behavior is unchanged (tests + a quick manual reasoning pass) "
                  "and update any docs/comments the refactor invalidated.",
                  loop_max=2, carry_context=True, continue_on_fail=True),
        ],
    },
]


def starter_templates() -> List[Dict[str, Any]]:
    return STARTER_TEMPLATES


_WORD = re.compile(r"[A-Za-z0-9].*")


def _step_name_from(text: str, fallback: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return fallback
    # First clause / sentence, capped — a readable step label.
    head = re.split(r"[.\n;:]", text, 1)[0].strip()
    head = head[:48].rstrip()
    return head or fallback


def extract_from_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """Build a draft workflow template from a discovered agent session.

    Each human turn in the transcript becomes a step, in order — so a real
    Claude/Codex conversation you liked becomes a workflow you can rerun, tweak,
    and template. Falls back to the session's first prompt if the tail is thin.
    """
    agent = session.get("agent", "auto") or "auto"
    name = session.get("name") or f"{agent} workflow"
    cwd = session.get("cwd", "") or ""
    tail = session.get("tail") or []
    user_turns = [m.get("text", "") for m in tail if m.get("role") == "user" and m.get("text")]
    # de-noise / de-dup consecutive identical prompts
    seen: List[str] = []
    for t in user_turns:
        if not seen or seen[-1] != t:
            seen.append(t)
    if not seen:
        first = session.get("title") or session.get("recap") or ""
        seen = [first] if first else ["Continue the work."]
    steps = []
    for i, text in enumerate(seen):
        steps.append(_step(_step_name_from(text, f"Step {i + 1}"), text,
                           carry_context=(i > 0)))
    return {
        "name": f"{name} (extracted)",
        "description": f"Extracted from a {agent} session — {len(steps)} step(s). "
                       "Edit the prompts to generalize it into a reusable workflow.",
        "agent": agent,
        "cwd": cwd,
        "steps": steps,
    }
