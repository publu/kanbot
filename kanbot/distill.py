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

META_PROMPT = """You are extracting REUSABLE, GROUNDED workflows from a \
developer's past coding session. You are running INSIDE the actual repository \
that session worked in — USE IT. Read the real code to ground every workflow in \
what actually exists. Do not invent.

READ-ONLY: you may explore (read, grep, glob, list) to verify your understanding, \
but you MUST NOT edit, create, or delete files, or run commands that change \
anything. This is analysis, not work.

The text below is the human side of the session — messy, conversational, full of \
dead ends and meta-commentary aimed at the assistant ("stop telling me what to \
do", "GIVE ME THE LAST PART", "ok that's good i guess"). IGNORE the noise. Mine \
the substance: what was actually built/fixed, HOW, and what was LEARNED.

Work in four phases:

PHASE 1 — TRIAGE. From the transcript and a quick look at the repo, decide what \
real engineering work happened and of what kind (feature / bug fix / refactor / \
infra / research). If it was just discussion or there is no real, reusable method \
to extract, return {"workflows": []}. Do not manufacture a workflow.

PHASE 2 — GROUND IN THE CODE. For each candidate objective, open the actual files \
involved. Confirm the components, modules, patterns, and conventions referenced \
really exist in THIS repo. A workflow you cannot tie to real code is a \
hallucination — drop it.

PHASE 3 — EXTRACT THE PATTERN + TAKEAWAYS. Climb the abstraction ladder: write \
each workflow for the CLASS of task ("sort this $ table" -> "add correct \
typed/numeric sorting to any data table"), grounded in the concrete reality you \
just verified. Bake in the takeaways: the gotcha that wasted time, the approach \
that worked, why a choice was made — as "Method: …" / "Watch out for: …" guidance \
inside the step prompts, with the key lesson leading the `description`. When the \
pattern needs a specific subject each run, make the FIRST step a fill-in: \
"TARGET: <what to apply this to — fill in before running>".

PHASE 4 — FALSIFY. Before emitting, try to INVALIDATE each workflow: Is this the \
method the code/transcript actually shows, or a guess? Is it genuinely reusable, \
or a one-off? Could a fresh agent with no memory of this chat follow it against \
this repo? Drop every workflow that fails. Returning FEWER real, grounded \
workflows (even zero) is the goal — never pad with plausible slop.

STRUCTURE each surviving workflow as 3-6 ordered steps. Each step's `prompt` is a \
self-contained instruction for a fresh agent: what to do, the method, the \
gotchas, how to verify. Steps hand off via files (PLAN.md / NOTES.md). For \
iterative work set `loop_max` (e.g. 20) and a `loop_until` shell predicate (e.g. \
`pytest -q`). `carry_context` true when a step needs the previous step's output. \
`name` is the PATTERN (class of task), short and imperative. `description` leads \
with the key takeaway.

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
    prompt = META_PROMPT % body[:7000]
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
NOT from a past session. A playbook is a 3-6 step procedure a fresh agent can \
follow to accomplish a CLASS of task with almost no extra prompting.

%s

Design the playbook:
- Break the work into 3-6 ordered steps. Each step's `prompt` is a self-contained \
instruction for a fresh agent: what to do, the method, the gotchas, how to verify.
- Climb the abstraction ladder: write for the CLASS of task, not one instance. \
Where a specific subject is needed per run, make the first step a fill-in \
("TARGET: <what to apply this to — fill in before running>").
- Bake in method + gotchas as "Method: …" / "Watch out for: …" guidance inside the \
step prompts; lead the `description` with the single most useful takeaway.
- Steps hand off via files (PLAN.md / NOTES.md) since each runs with fresh context.
- For iterative work set `loop_max` (e.g. 20) and a `loop_until` shell predicate \
(e.g. `pytest -q`). Set `carry_context` true when a step needs the previous \
step's output.
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
    prompt = META_PROMPT % body[:7000]
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
