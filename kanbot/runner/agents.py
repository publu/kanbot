"""Agent detection and execution for the runner.

Detection: walk the built-in catalog, keep any whose `bin` is on PATH, apply
user overrides/disables from config. The resulting list of names is what the
runner advertises to the server as its capabilities.

Execution: every run happens inside a runner-owned PTY pane (see panes.py), in
one of two modes:
  headless     the agent's print/exec argv (`claude -p …`); output is streamed
               line-by-line back to the card and the exit code decides success.
  interactive  the agent's own TUI (`claude "…"`), live on the board, typeable
               from the browser or `kanbot attach`, resumable, with hook-exact
               working/blocked/idle state.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from ..agents import BUILTIN_AGENTS, AgentSpec
from ..config import Config
from .panes import Pane, PaneManager


@dataclass
class ResolvedAgent:
    name: str
    label: str
    argv: List[str]
    env: Dict[str, str]
    resume_argv: List[str] = field(default_factory=list)
    safe_argv: List[str] = field(default_factory=list)
    safe_resume_argv: List[str] = field(default_factory=list)
    tui_argv: List[str] = field(default_factory=list)
    tui_resume_argv: List[str] = field(default_factory=list)
    safe_tui_argv: List[str] = field(default_factory=list)
    safe_tui_resume_argv: List[str] = field(default_factory=list)
    claude_hooks: bool = False

    @property
    def can_resume(self) -> bool:
        return bool(self.resume_argv)


def _override_argv(template: str) -> List[str]:
    """A config override is a shell-ish string; keep {prompt} as its own token."""
    return shlex.split(template)


def detect_agents(cfg: Config) -> Dict[str, ResolvedAgent]:
    found: Dict[str, ResolvedAgent] = {}
    for spec in BUILTIN_AGENTS:
        if spec.name in cfg.disabled_agents:
            continue
        argv = spec.argv
        binary = spec.bin
        if spec.name in cfg.agent_overrides:
            argv = _override_argv(cfg.agent_overrides[spec.name])
            binary = argv[0] if argv else spec.bin
        if not shutil.which(binary):
            continue
        env = dict(spec.env)
        env.update(cfg.key_env_for(spec.name))  # inject the configured provider API key
        tui_argv = list(spec.tui_argv)
        if spec.name == "shell":                # the user's own shell, not bash
            tui_argv = [os.environ.get("SHELL") or "bash", "-l"]
        found[spec.name] = ResolvedAgent(
            name=spec.name, label=spec.label, argv=list(argv), env=env,
            resume_argv=list(spec.resume_argv),
            safe_argv=list(spec.safe_argv), safe_resume_argv=list(spec.safe_resume_argv),
            tui_argv=tui_argv, tui_resume_argv=list(spec.tui_resume_argv),
            safe_tui_argv=list(spec.safe_tui_argv),
            safe_tui_resume_argv=list(spec.safe_tui_resume_argv),
            claude_hooks=spec.claude_hooks,
        )
    return found


def _pick_template(agent: ResolvedAgent, resuming: bool, auto_approve: bool,
                   interactive: bool) -> List[str]:
    if interactive and agent.tui_argv:
        if resuming and agent.tui_resume_argv:
            return (agent.tui_resume_argv if auto_approve
                    else (agent.safe_tui_resume_argv or agent.tui_resume_argv))
        return agent.tui_argv if auto_approve else (agent.safe_tui_argv or agent.tui_argv)
    if resuming:
        return (agent.resume_argv if auto_approve
                else (agent.safe_resume_argv or agent.resume_argv))
    return agent.argv if auto_approve else (agent.safe_argv or agent.argv)


def hook_settings() -> str:
    """Claude Code `--settings` JSON wiring its lifecycle hooks to this pane.

    Each hook runs `kanbot _hook <state>` with KANBOT_PANE_ID / KANBOT_SOCK
    inherited from the pane's environment, so the runner learns the exact state.
    """
    import sys
    exe = f"{shlex.quote(sys.executable)} -m kanbot"
    def h(state: str) -> list:
        return [{"hooks": [{"type": "command", "command": f"{exe} _hook {state}"}]}]
    return json.dumps({"hooks": {
        "UserPromptSubmit": h("working"),
        "PreToolUse": h("working"),
        "Notification": h("blocked"),
        "Stop": h("idle"),
        "SessionEnd": h("done"),
    }})


def build_argv(agent: ResolvedAgent, prompt: str, resume_of: str = "",
               auto_approve: bool = True, command: str = "",
               interactive: bool = False) -> List[str]:
    # A per-card raw command override wins over the agent's built-in templates,
    # so the user can run literally any command ({prompt}/{session_id} expand).
    if command and command.strip():
        template = _override_argv(command)
    else:
        resuming = bool(resume_of and (agent.can_resume or agent.tui_resume_argv))
        template = _pick_template(agent, resuming, auto_approve, interactive)
    out: List[str] = []
    for tok in template:
        if tok == "{prompt}" and not prompt:
            continue                       # `claude ""` would send an empty turn
        tok = tok.replace("{prompt}", prompt)
        tok = tok.replace("{session_id}", resume_of)
        out.append(tok)
    if agent.claude_hooks and out and os.path.basename(out[0]) == "claude" and not command.strip():
        out[1:1] = ["--settings", hook_settings()]
    return out


def needs_typed_prompt(argv_template: List[str]) -> bool:
    return "{prompt}" not in " ".join(argv_template)


LogCb = Callable[[str, str], Awaitable[None]]  # (stream, text) -> awaitable


class Execution:
    """A running agent pane for one session (kept for the cancel path)."""

    def __init__(self, session_id: str, pane: Pane):
        self.session_id = session_id
        self.pane = pane

    async def cancel(self) -> None:
        if self.pane.alive:
            await self.pane.terminate()


async def run_agent(agent: ResolvedAgent, prompt: str, cwd: str, on_log: LogCb,
                    register: Callable[[Execution], None], panes: PaneManager,
                    resume_of: str = "", auto_approve: bool = True, command: str = "",
                    interactive: bool = False, session_id: str = "", card_id: str = "",
                    title: str = "") -> int:
    """Run the agent in a pane, streaming output. Returns the process exit code."""
    using_override = bool(command and command.strip())
    if resume_of and not (agent.can_resume or agent.tui_resume_argv) and not using_override:
        await on_log("system", f"agent '{agent.name}' can't resume sessions; starting fresh.")
        resume_of = ""
    argv = build_argv(agent, prompt, resume_of, auto_approve=auto_approve, command=command,
                      interactive=interactive)
    if using_override:
        await on_log("system", "running custom command override for this card")
    elif not auto_approve:
        await on_log("system", "safe mode: agent runs without auto-approve flags")
    if resume_of:
        await on_log("system", f"resuming {agent.name} session {resume_of}")
    if cwd:
        if not os.path.isdir(cwd):
            try:
                os.makedirs(cwd, exist_ok=True)
                await on_log("system", f"created working directory {cwd}")
            except OSError as e:
                await on_log("stderr", f"could not create cwd {cwd}: {e}")
                return 1
        workdir = cwd
    else:
        workdir = os.getcwd()
    env = dict(agent.env)
    # Refresh the provider API key at exec time so keys set from the UI take
    # effect immediately, without restarting the runner.
    try:
        env.update(Config.load().key_env_for(agent.name))
    except Exception:  # noqa: BLE001
        pass

    shown = [a if not (i and argv[i - 1] == "--settings") else "{hooks}" for i, a in enumerate(argv)]
    await on_log("system", f"$ {' '.join(shlex.quote(a) for a in shown)}")
    await on_log("system", f"(cwd: {workdir})")

    try:
        pane = panes.spawn(argv, cwd=workdir, env=env, agent=agent.name,
                           title=title or prompt[:80] or agent.label,
                           interactive=interactive, session_id=session_id, card_id=card_id)
    except OSError as e:
        await on_log("stderr", f"failed to start agent: {e}")
        return 1
    register(Execution(session_id, pane))
    await on_log("system", f"⌨ pane {pane.id} · {'interactive' if interactive else 'headless'} · "
                           f"attach with: kanbot attach {pane.id}")

    # TUIs whose argv has no {prompt} slot (aider, a bare shell) get it typed in.
    tmpl = _pick_template(agent, bool(resume_of), auto_approve, interactive) if not using_override else argv
    if interactive and prompt and needs_typed_prompt(tmpl):
        await asyncio.sleep(1.5)
        pane.write(prompt.encode() + b"\r")

    rc = await pane.wait()
    if pane.exit_code == 127 and not pane.buf:
        await on_log("stderr", f"agent binary not found: {argv[0]}")
    return rc if rc is not None else 1
