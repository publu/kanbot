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


def _step_name_from(text: str, fallback: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return fallback
    # First clause / sentence, capped — a readable step label.
    head = re.split(r"[.\n;:]", text, 1)[0].strip()
    head = head[:48].rstrip()
    return head or fallback


# --- session -> workflow(s) ------------------------------------------------
# A long session rarely maps to a single workflow: it tends to contain several
# distinct objectives separated by topic shifts. We split human turns into
# segments (each segment = one candidate workflow) and let the caller decide
# whether to keep them split or combine them into one.
_STOP = set(
    "the a an to of and or for in on with into your you it its is are be do done now then please "
    "make sure that this these those i we he she they them his her their use using also let lets ok "
    "okay next so but if when can could would should will from at as by".split()
)
_CUE = ("now ", "now,", "ok now", "okay now", "next ", "next,", "then ", "also ",
        "another", "switch", "new task", "different", "finally", "moving on",
        "one more", "let's now", "lets now", "separately")
_IMPERATIVE = ("add", "create", "implement", "write", "build", "refactor", "fix",
               "make", "remove", "delete", "update", "set up", "setup", "test",
               "document", "wire", "migrate", "rename", "extract", "split",
               "convert", "generate", "design", "port", "integrate")


def _keywords(text: str) -> set:
    toks = re.findall(r"[a-zA-Z][a-zA-Z0-9_]+", (text or "").lower())
    return {t for t in toks if len(t) > 2 and t not in _STOP}


def _is_boundary(turn: str, acc_kw: set) -> bool:
    """Does this human turn start a NEW objective (vs. continue the current one)?

    Conservative on purpose: short follow-ups ("make sure it has tests", "now fix
    the lint") must stay attached to what they refer to. We only break on an
    explicit transition cue, or a *substantial* fresh imperative that shares
    almost no vocabulary with the current segment."""
    t = turn.strip().lower()
    if not t:
        return False
    head = t[:24]
    if any(c in head for c in _CUE):
        return True
    kw = _keywords(turn)
    if len(kw) < 4 or not acc_kw:          # too short to be its own objective
        return False
    overlap = len(kw & acc_kw) / (len(kw | acc_kw) or 1)
    starts_imperative = any(t.startswith(v) for v in _IMPERATIVE)
    return overlap < 0.1 and starts_imperative


def _segment_turns(turns: List[str]) -> List[List[str]]:
    segments: List[List[str]] = []
    cur: List[str] = []
    acc: set = set()
    for turn in turns:
        if cur and _is_boundary(turn, acc):
            segments.append(cur)
            cur, acc = [], set()
        cur.append(turn)
        acc |= _keywords(turn)
    if cur:
        segments.append(cur)
    return segments


def _collect_turns(sessions: List[Dict[str, Any]]) -> List[str]:
    turns: List[str] = []
    for s in sessions:
        for m in (s.get("tail") or []):
            if m.get("role") == "user" and m.get("text"):
                turns.append(m["text"])
    dedup: List[str] = []
    for t in turns:
        if not dedup or dedup[-1] != t:
            dedup.append(t)
    if not dedup:
        first = sessions[0].get("title") or sessions[0].get("recap") or ""
        dedup = [first] if first else ["Continue the work."]
    return dedup


def _template(name: str, agent: str, cwd: str, turns: List[str], description: str) -> Dict[str, Any]:
    steps = [_step(_step_name_from(t, f"Step {i + 1}"), t, carry_context=(i > 0))
             for i, t in enumerate(turns)]
    return {"name": name, "description": description, "agent": agent, "cwd": cwd, "steps": steps}


def extract_workflows(sessions, split: bool = True) -> List[Dict[str, Any]]:
    """Extract one or more workflow templates from a session (or several merged,
    in the order given). split=True segments by topic into multiple workflows;
    split=False combines everything into a single workflow."""
    if isinstance(sessions, dict):
        sessions = [sessions]
    if not sessions:
        return []
    agent = sessions[0].get("agent", "auto") or "auto"
    cwd = sessions[0].get("cwd", "") or ""
    base = sessions[0].get("name") or f"{agent} workflow"
    src = f"{len(sessions)} sessions" if len(sessions) > 1 else f"a {agent} session"
    turns = _collect_turns(sessions)
    if not split:
        return [_template(f"{base} (extracted)", agent, cwd, turns,
                          f"Extracted from {src} — {len(turns)} step(s). "
                          "Edit the prompts to generalize it.")]
    segments = _segment_turns(turns)
    total = len(segments)
    out = []
    for i, seg in enumerate(segments):
        label = _step_name_from(seg[0], f"part {i + 1}")
        name = f"{base} (extracted)" if total == 1 else f"{base}: {label}"
        out.append(_template(name, agent, cwd, seg,
                             f"Extracted from {src} — segment {i + 1}/{total}, "
                             f"{len(seg)} step(s)."))
    return out


def extract_from_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """Back-compat: one flat workflow from one session."""
    return extract_workflows(session, split=False)[0]


# --- proactive suggestions -------------------------------------------------
# "Let me go through your sessions and propose automations." Instead of making
# the user hand-pick sessions, we read everything they've run, find the recurring
# shapes (per project + cross-cutting patterns), and hand back ready-to-save
# automations with a plain-English rationale for each.
def _project_name(s: Dict[str, Any]) -> str:
    cwd = (s.get("cwd") or "").rstrip("/")
    if cwd:
        return cwd.rsplit("/", 1)[-1] or cwd
    return s.get("name") or "project"


def _user_texts(s: Dict[str, Any]) -> List[str]:
    return [m.get("text", "") for m in (s.get("tail") or [])
            if m.get("role") == "user" and m.get("text")]


# (starter-template name, trigger keywords, why-it-matters phrasing)
_PATTERNS = [
    ("Harden until green", ("test", "tests", "failing", "green", " ci", "lint", "fix the"),
     "chase a green test suite"),
    ("Deep refactor", ("refactor", "clean up", "restructure", "rename", "extract", "tidy"),
     "refactor and restructure code"),
    ("Ship a feature", ("implement", "add a", "add an", "build a", "feature", "create a", "wire up"),
     "build features end to end"),
]


def _pattern_suggestions(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_name = {t["name"]: t for t in STARTER_TEMPLATES}
    out = []
    for name, kws, why in _PATTERNS:
        hits = []
        for s in sessions:
            blob = " ".join(_user_texts(s)).lower()
            if blob and any(k in blob for k in kws):
                hits.append(s)
        if len(hits) >= 3 and name in by_name:
            tpl = {k: (list(v) if isinstance(v, list) else v) for k, v in by_name[name].items()}
            out.append({
                "title": name,
                "kind": "pattern",
                "rationale": f"Across {len(hits)} of your sessions you {why} — "
                             "this automation does the whole loop unattended.",
                "sources": [s.get("name") or _project_name(s) for s in hits][:6],
                "template": tpl,
                "score": len(hits) + 1,
            })
    return out


def suggest_automations(sessions: List[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    """Analyze discovered sessions and propose automations (workflows).

    Two lenses: per-project (the recurring flow inside one repo) and
    cross-cutting patterns (test-hardening, refactors, feature builds you do
    everywhere). Returns previews ranked by how strong the signal is."""
    usable = [s for s in sessions if _user_texts(s)]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in usable:
        groups.setdefault(s.get("cwd") or s.get("name") or "misc", []).append(s)

    projects: List[Dict[str, Any]] = []
    for grp in groups.values():
        if not grp:
            continue
        # An automation is a tight, repeatable pipeline — so base it on the one
        # most substantial session in the project (most turns, then most recent),
        # not a pile-up of every session. The confidence comes from the count.
        rep = max(grp, key=lambda s: (len(_user_texts(s)), s.get("mtime", 0)))
        if len(_user_texts(rep)) < 2:
            continue
        proj = _project_name(rep)
        tpl = extract_workflows(rep, split=False)[0]
        tpl["steps"] = tpl["steps"][:8]
        tpl["name"] = f"{proj} automation"
        tpl["description"] = (f"A repeatable run based on your work in {proj} "
                              f"({len(grp)} session(s) seen).")
        n = len(grp)
        projects.append({
            "title": f"{proj} automation",
            "kind": "project",
            "rationale": (f"You've run {n} session{'s' if n != 1 else ''} in {proj}. "
                          f"Here's a {len(tpl['steps'])}-step automation from your "
                          "most substantial one — tweak it once, rerun it forever."),
            "sources": [s.get("name") or proj for s in grp][:6],
            "template": tpl,
            "score": n,
        })
    projects.sort(key=lambda x: -x["score"])

    patterns = _pattern_suggestions(usable)
    # Lead with up to 2 reusable pattern automations, then the busiest projects.
    out = patterns[:2] + projects[:max(0, limit - min(2, len(patterns)))]
    for s in out:
        s.pop("score", None)
    return out[:limit]
