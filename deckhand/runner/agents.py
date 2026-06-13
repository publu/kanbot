"""Agent detection and execution for the runner.

Detection: walk the built-in catalog, keep any whose `bin` is on PATH, apply
user overrides/disables from config. The resulting list of names is what the
runner advertises to the server as its capabilities.

Execution: spawn the agent's argv (with {prompt} substituted per token) in the
target cwd, stream stdout/stderr lines back through an async callback, and honor
cancellation.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from ..agents import BUILTIN_AGENTS, AgentSpec
from ..config import Config


@dataclass
class ResolvedAgent:
    name: str
    label: str
    argv: List[str]
    env: Dict[str, str]


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
        found[spec.name] = ResolvedAgent(
            name=spec.name, label=spec.label, argv=list(argv), env=dict(spec.env)
        )
    return found


def build_argv(agent: ResolvedAgent, prompt: str) -> List[str]:
    out: List[str] = []
    for tok in agent.argv:
        if "{prompt}" in tok:
            out.append(tok.replace("{prompt}", prompt))
        else:
            out.append(tok)
    return out


LogCb = Callable[[str, str], Awaitable[None]]  # (stream, text) -> awaitable


class Execution:
    """A running agent subprocess for one session."""

    def __init__(self, session_id: str, proc: asyncio.subprocess.Process):
        self.session_id = session_id
        self.proc = proc

    async def cancel(self) -> None:
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass


async def run_agent(agent: ResolvedAgent, prompt: str, cwd: str, on_log: LogCb,
                    register: Callable[[Execution], None]) -> int:
    """Run the agent, streaming output. Returns the process exit code."""
    argv = build_argv(agent, prompt)
    workdir = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    env = os.environ.copy()
    env.update(agent.env)

    await on_log("system", f"$ {' '.join(shlex.quote(a) for a in argv)}")
    await on_log("system", f"(cwd: {workdir})")

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        await on_log("stderr", f"agent binary not found: {argv[0]}")
        return 127
    except OSError as e:
        await on_log("stderr", f"failed to start agent: {e}")
        return 1

    execution = Execution("", proc)
    register(execution)

    async def pump(stream, name: str):
        assert stream is not None
        while True:
            line = await stream.readline()
            if not line:
                break
            await on_log(name, line.decode("utf-8", "replace").rstrip("\n"))

    await asyncio.gather(
        pump(proc.stdout, "stdout"),
        pump(proc.stderr, "stderr"),
    )
    rc = await proc.wait()
    return rc
