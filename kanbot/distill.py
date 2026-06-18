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

META_PROMPT = """You are distilling a developer's past session into a reusable \
PLAYBOOK. A playbook's whole job is to AUTOMATE THE PROMPTING: capture so much of \
the method, judgment, and standards from this session that NEXT time the developer \
supplies ONE line — a TARGET — and a fresh agent reproduces the same class of \
outcome with no further explaining. You are running INSIDE the actual repository \
this session worked in — read the real code to ground everything. Do not invent.

READ-ONLY: explore (read, grep, glob, list) to verify your understanding; you MUST \
NOT edit, create, or delete files, or run anything that changes state.

The text below is the human side of the session — messy, full of dead ends and \
meta-commentary aimed at the assistant. Ignore the noise, but mine the developer's \
OWN WORDS for the real intent, the method that worked, the standards/taste they \
insisted on, and the gotchas that cost time. Their vocabulary is the raw material.

Work in four phases:

PHASE 1 — TRIAGE. Decide what real work happened and, more importantly, what GOAL \
it served. If it was just chatter with no reusable method, return \
{"workflows": []}. Never manufacture one.

PHASE 2 — GROUND. Open the actual files involved; confirm the components, modules, \
patterns and conventions referenced really exist in THIS repo. Anything you cannot \
tie to real code, drop.

PHASE 3 — GENERALIZE TO THE INTENT. This is the point of the whole exercise. Climb \
the abstraction ladder until the playbook names the GOAL, not the one-off artifact \
the session happened to produce. The `name` must read as a reusable INTENT with an \
explicit fill-in slot in {curly braces} — a command the developer could re-issue \
against a totally different subject (shape: "<kind of work> on {what} toward \
{quality/goal}"). If your name describes the specific thing built this time, you \
have NOT climbed high enough — abstract again. The concrete subject from this \
session becomes the {TARGET}; it is never baked into the name.

PHASE 4 — FALSIFY. Try to invalidate each playbook: is this the method the \
code/transcript actually shows, or a guess? Would it genuinely work against a \
DIFFERENT {TARGET}, or is it a one-off? Could a fresh agent with no memory follow \
it? Drop every one that fails. Fewer real playbooks (even zero) beats padding.

NOW WRITE A BIG PLAYBOOK. The value is in the depth — a thin playbook means you did \
not extract enough, so dig back into the transcript and the code before settling. \
Structure each surviving playbook as 3-6 ordered steps. Each step's `prompt` is a \
THOROUGH markdown brief for a fresh agent — several short labelled sections that \
together leave nothing to re-explain at run time:
  - the exact method that worked here (concrete, ordered);
  - the standards and taste the developer insisted on — quote their own phrasing;
  - the gotchas that wasted time and how to avoid them;
  - how to verify the step is actually done.
Write in the session's real vocabulary. The FIRST step states the run-time input: \
"TARGET: {the specific thing to apply this to — fill in before running}". Steps \
hand off via files (PLAN.md / NOTES.md). For iterative work set `loop_max` \
(e.g. 20) and a `loop_until` shell predicate (e.g. `pytest -q`); `carry_context` \
true when a step needs the previous step's output.

`name`: the generalized intent WITH its {slot} — short, imperative, reusable.
`description`: ONE plain-English sentence — what running it does and when to reach \
for it, present tense. No session recap, no jargon dump, no paragraph.

Return ONLY a JSON object as the very last thing you output, no markdown fences:
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


def _argv_for(spec, prompt: str, write: bool = False) -> List[str]:
    # Default to the agent's SAFE (read-only) invocation — distillation/judging
    # only inspect. write=True uses the full (write) argv, ONLY ever pointed at a
    # throwaway sandbox worktree (Part 2B replay), never the user's real repo.
    template = (spec.argv if write else (spec.safe_argv or spec.argv))
    out = []
    for tok in template:
        out.append(tok.replace("{prompt}", prompt).replace("{session_id}", ""))
    return out


def _run_agent(spec, prompt: str, cwd: Optional[str], timeout: int, write: bool = False) -> str:
    """Run one agent on a prompt in cwd; return stdout ('' on failure)."""
    env = os.environ.copy()
    env.update(spec.env)
    workdir = cwd if cwd and os.path.isdir(cwd) else tempfile.gettempdir()
    try:
        proc = subprocess.run(
            _argv_for(spec, prompt, write), cwd=workdir, stdin=subprocess.DEVNULL,
            env=env, capture_output=True, text=True, timeout=timeout,
        )
        return proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run_agent_text(prompt: str, available: Optional[List[str]] = None,
                   cwd: Optional[str] = None, timeout: int = 300, write: bool = False) -> str:
    """Run any available agent, return raw stdout. write=True allows file edits
    (sandbox replay only)."""
    spec = pick_agent(available)
    return _run_agent(spec, prompt, cwd, timeout, write) if spec else ""


def _stream_argv(spec, prompt: str):
    """Read-only argv tuned for a LIVE feed. For claude we switch to NDJSON
    streaming so the UI sees every read/grep/tool-step as it happens; the final
    'result' event still carries the full answer for JSON extraction. Returns
    (argv, mode) where mode is 'claude-json' or 'raw'."""
    base = _argv_for(spec, prompt)            # safe/read-only by construction
    if spec.name == "claude" and base[:2] == ["claude", "-p"]:
        return (["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"],
                "claude-json")
    return base, "raw"


def _render_claude_event(ev, repo: str = "") -> Optional[str]:
    """Turn one claude NDJSON event into a short human line for the terminal feed."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        return f"● grounding in {repo} — reading the real code" if repo \
            else "● no repo for this session — reasoning from the transcript"
    if t == "assistant":
        out = []
        for b in (ev.get("message", {}) or {}).get("content", []) or []:
            if b.get("type") == "tool_use":
                inp = b.get("input", {}) or {}
                arg = inp.get("file_path") or inp.get("path") or inp.get("pattern") \
                    or inp.get("command") or inp.get("query") or ""
                out.append(f"→ {b.get('name','tool')} {str(arg)[:90]}".rstrip())
            elif b.get("type") == "text":
                txt = (b.get("text") or "").strip().splitlines()
                if txt and txt[0]:
                    out.append("  " + txt[0][:120])
        return "\n".join(out) if out else None
    if t == "result":
        return "✓ analysis complete"
    return None


def stream_agent(prompt: str, available: Optional[List[str]], cwd: Optional[str],
                 on_line, timeout: int = 300):
    """Run an agent and call on_line(str) for each unit of activity as it arrives,
    so callers can stream the agent's real work to the UI. Returns (full_text, name)."""
    import time as _t
    spec = pick_agent(available)
    if not spec:
        return "", None
    env = os.environ.copy(); env.update(spec.env)
    grounded = bool(cwd and os.path.isdir(cwd))
    workdir = cwd if grounded else tempfile.gettempdir()
    repo = os.path.basename(cwd.rstrip("/")) if grounded else ""
    argv, mode = _stream_argv(spec, prompt)
    try:
        proc = subprocess.Popen(
            argv, cwd=workdir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True, bufsize=1)
    except OSError:
        return "", spec.name
    buf, start = [], _t.time()
    final = ""

    def feed(s):
        if not on_line or not s:
            return
        for ln in str(s).splitlines():
            if ln.strip():
                try: on_line(ln[:400])
                except Exception: pass

    try:
        for line in proc.stdout:
            if mode == "claude-json":
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    feed(line); continue
                if ev.get("type") == "result" and isinstance(ev.get("result"), str):
                    final = ev["result"]
                feed(_render_claude_event(ev, repo))
            else:
                buf.append(line)
                feed(line.rstrip("\n"))
            if _t.time() - start > timeout:
                proc.kill(); break
    except Exception:
        pass
    try: proc.wait(timeout=5)
    except Exception:
        try: proc.kill()
        except Exception: pass
    text = final or "".join(buf)
    return text, spec.name


def distill_workflows_stream(template, available, on_line, timeout=300, exemplars=None):
    """Same as distill_workflows but streams the agent's stdout via on_line."""
    turns = [str(s.get("prompt") or "").strip() for s in template.get("steps", [])]
    turns = [t for t in turns if t]
    if not turns:
        return []
    ctx = str(template.get("_context") or "").strip()
    body = _exemplar_block(exemplars)
    if ctx:
        body += f"\nTHE SESSION'S OPENING REQUEST (the real goal): {ctx[:800]}\n\n"
    body += "LATER LINES FROM THE TRANSCRIPT (mostly noise — mine for intent):\n"
    body += "\n".join(f"- {t}" for t in turns)
    prompt = META_PROMPT % body[:12000]
    text, by = stream_agent(prompt, available, str(template.get("cwd") or ""), on_line, timeout)
    data = _extract_json(text)
    if not data:
        return []
    raw = data.get("workflows") if isinstance(data.get("workflows"), list) else [data]
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        norm = _normalize(item, template)
        if norm:
            norm["_distilled_by"] = by
            out.append(norm)
    return out


def run_agent_json(prompt: str, available: Optional[List[str]] = None,
                   cwd: Optional[str] = None, timeout: int = 300):
    """Run any available reasoning agent and return (parsed_json|None, agent_name).
    Shared by distillation and the evaluator (Part 2)."""
    spec = pick_agent(available)
    if not spec:
        return None, None
    return _extract_json(_run_agent(spec, prompt, cwd, timeout)), spec.name


def _exemplar_block(exemplars: Optional[List[dict]]) -> str:
    """A few proven workflows, shown as the bar to match (Part 2 bootstrapping)."""
    if not exemplars:
        return ""
    out = ["\nPROVEN EXEMPLARS — workflows that scored well before. Match this "
           "level of grounding, generalization, and baked-in takeaways (do not "
           "copy their subject matter):"]
    for ex in exemplars[:3]:
        steps = " → ".join(s.get("name", "") for s in (ex.get("steps") or []))
        out.append(f'  • {ex.get("name","")}: {ex.get("description","")[:140]}  [{steps}]')
    return "\n".join(out) + "\n"


DRAFT_PROMPT = """You are AUTHORING a reusable PLAYBOOK from a short description — \
NOT from a past session. A playbook's whole job is to AUTOMATE THE PROMPTING: the \
user supplies ONE line (a TARGET) and a fresh agent reproduces the outcome with no \
further explaining.

%s

Design the playbook:
- GENERALIZE TO THE INTENT. The `name` is the reusable GOAL with an explicit \
fill-in slot in {curly braces} — a command re-issuable against a different subject \
(shape: "<kind of work> on {what} toward {quality/goal}"). Never bake the specific \
subject into the name; it is the {TARGET}.
- WRITE BIG. Break the work into 3-6 ordered steps. Each step's `prompt` is a \
THOROUGH markdown brief — several short labelled sections: the exact method \
(ordered), the standards/taste to hold to, the gotchas to avoid, and how to verify \
the step is done. Leave nothing to re-explain at run time. A thin playbook is a \
failed one.
- The FIRST step states the run-time input: "TARGET: {the specific thing to apply \
this to — fill in before running}".
- Steps hand off via files (PLAN.md / NOTES.md) since each runs with fresh context.
- For iterative work set `loop_max` (e.g. 20) and a `loop_until` shell predicate \
(e.g. `pytest -q`). Set `carry_context` true when a step needs the previous \
step's output.
- `description`: ONE plain-English sentence — what running it does and when to \
reach for it. No jargon dump, no paragraph.
%s
Return ONLY a JSON object as the very last thing you output, no markdown fences:
{"workflows": [{"name": str, "description": str, "steps": [{"name": str, \
"prompt": str, "loop_max": int, "loop_until": str, "carry_context": bool, \
"continue_on_fail": bool}]}]}

THE PLAYBOOK TO AUTHOR:
%s
"""


def draft_workflows_stream(description: str, cwd: str, available: Optional[List[str]],
                           on_line, timeout: int = 300) -> List[Dict[str, Any]]:
    """Author a brand-new playbook from a freeform description (not a session),
    streaming the agent's real work via on_line. If cwd is a real repo, the agent
    is grounded in it (read-only) so the steps fit the actual code."""
    desc = (description or "").strip()
    if not desc:
        return []
    grounded = bool(cwd and os.path.isdir(cwd))
    intro = ("You are running READ-ONLY inside the actual repository this playbook "
             "will operate on — explore it (read/grep/glob) to ground every step in "
             "real files, conventions, and tooling. Do not edit anything."
             if grounded else
             "No repository is attached — write the playbook to be broadly reusable "
             "for this class of task.")
    exem = ""
    prompt = DRAFT_PROMPT % (intro, exem, desc[:4000])
    base = {"agent": "auto", "cwd": cwd or "", "name": "", "description": ""}
    text, by = stream_agent(prompt, available, cwd if grounded else "", on_line, timeout)
    data = _extract_json(text)
    if not data:
        return []
    raw = data.get("workflows") if isinstance(data.get("workflows"), list) else [data]
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        norm = _normalize(item, base)
        if norm:
            norm["_distilled_by"] = by
            out.append(norm)
    return out


def distill_workflows(template: Dict[str, Any], available: Optional[List[str]] = None,
                      timeout: int = 300, exemplars: Optional[List[dict]] = None) -> List[Dict[str, Any]]:
    """Extract one OR MORE clean, GROUNDED workflows from a session draft using
    any available agent — run read-only INSIDE the session's repo so the agent
    can verify its findings against real code (pruning hallucinations). Optionally
    steered by proven `exemplars`. Returns [] if no agent / nothing grounded."""
    turns = [str(s.get("prompt") or "").strip() for s in template.get("steps", [])]
    turns = [t for t in turns if t]
    if not turns:
        return []
    ctx = str(template.get("_context") or "").strip()
    body = _exemplar_block(exemplars)
    if ctx:
        body += f"\nTHE SESSION'S OPENING REQUEST (the real goal): {ctx[:800]}\n\n"
    body += "LATER LINES FROM THE TRANSCRIPT (mostly noise — mine for intent):\n"
    body += "\n".join(f"- {t}" for t in turns)
    prompt = META_PROMPT % body[:12000]
    cwd = str(template.get("cwd") or "").strip()
    data, by = run_agent_json(prompt, available, cwd, timeout)
    if not data:
        return []
    raw = data.get("workflows") if isinstance(data.get("workflows"), list) else [data]
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        norm = _normalize(item, template)
        if norm:
            norm["_distilled_by"] = by
            out.append(norm)
    return out


def distill_template(template: Dict[str, Any], available: Optional[List[str]] = None,
                     timeout: int = 180) -> Optional[Dict[str, Any]]:
    """Back-compat: first distilled workflow only."""
    out = distill_workflows(template, available, timeout)
    return out[0] if out else None
