"""The background runner: connects to the KanBot server over WebSocket,
advertises which CLI agents are installed locally, and executes assigned tasks.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import time
from typing import Dict, Optional

import websockets

from ..config import Config
from .agents import Execution, ResolvedAgent, detect_agents, run_agent
from .discovery import discover_all

# Safety cap so a Ralph loop can't run away on its own. High enough for a true
# multi-hour "goal spree" (one task per iteration) — a wall-clock budget
# (max_seconds, per-assignment) is the primary bound for long runs.
MAX_LOOP_ITERATIONS = 1000


class Runner:
    def __init__(self, cfg: Config, verbose: bool = True):
        self.cfg = cfg
        self.verbose = verbose
        self.agents: Dict[str, ResolvedAgent] = detect_agents(cfg)
        self.ws = None
        self.executions: Dict[str, Execution] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self._stop = False

    def log(self, *a):
        if self.verbose:
            print("[deckhand]", *a, flush=True)

    @property
    def ws_endpoint(self) -> str:
        url = self.cfg.ws_url.rstrip("/") + "/ws/runner"
        if self.cfg.token:
            url += f"?token={self.cfg.token}"
        return url

    async def send(self, obj: dict) -> None:
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps(obj))
            except Exception:
                pass

    async def run_forever(self) -> None:
        if not self.agents:
            self.log("WARNING: no CLI agents detected on PATH. The runner will "
                     "advertise nothing to run. Install one of: claude, codex, "
                     "gemini, opencode, aider, cursor-agent (or 'shell' fallback).")
        backoff = 1
        while not self._stop:
            try:
                async with websockets.connect(self.ws_endpoint, ping_interval=20,
                                               ping_timeout=20, max_size=None) as ws:
                    self.ws = ws
                    backoff = 1
                    await self._handshake()
                    self.log(f"connected to {self.cfg.server_url} as "
                             f"'{self.cfg.runner_name}' with agents: "
                             f"{', '.join(self.agents) or '(none)'}")
                    discover_task = asyncio.create_task(self._discover_loop())
                    try:
                        await self._consume()
                    finally:
                        discover_task.cancel()
            except (OSError, websockets.exceptions.WebSocketException) as e:
                self.log(f"connection lost ({e}); retrying in {backoff}s")
            except asyncio.CancelledError:
                break
            finally:
                self.ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _handshake(self) -> None:
        await self.send({
            "type": "hello",
            "runner_id": self.cfg.runner_id,
            "name": self.cfg.runner_name,
            "host": platform.node(),
            "capabilities": list(self.agents.keys()),
            "max_concurrency": self.cfg.max_concurrency,
            "auto_approve": self.cfg.auto_approve,
        })

    async def _discover_loop(self) -> None:
        """Periodically report the agents' own sessions so the board can show
        in-progress work and offer to revive past sessions."""
        names = list(self.agents.keys())
        while True:
            try:
                sessions = await asyncio.to_thread(
                    discover_all, names, self.cfg.discovery_sources)
                await self.send({"type": "agent.sessions",
                                 "runner_id": self.cfg.runner_id, "sessions": sessions})
            except Exception as e:
                self.log(f"discovery error: {e}")
            await asyncio.sleep(6)

    async def _consume(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "assign":
                self._spawn_task(msg)
            elif mtype == "cancel":
                await self._cancel(msg.get("session_id", ""))
            elif mtype in ("welcome", "pong"):
                pass

    def _spawn_task(self, msg: dict) -> None:
        sid = msg["session_id"]
        task = asyncio.create_task(self._execute(msg))
        self.tasks[sid] = task

    async def _execute(self, msg: dict) -> None:
        sid = msg["session_id"]
        agent_name = msg.get("agent", "")
        prompt = msg.get("prompt", "")
        cwd = msg.get("cwd", "")
        resume_of = msg.get("resume_of", "")
        command = msg.get("command", "") or ""
        loop_max = min(MAX_LOOP_ITERATIONS, max(1, int(msg.get("loop_max", 1) or 1)))
        loop_until = msg.get("loop_until", "") or ""
        max_seconds = max(0, int(msg.get("max_seconds", 0) or 0))  # 0 = unbounded
        agent = self.agents.get(agent_name)
        await self.send({"type": "session.start", "session_id": sid})
        self.log(f"running session {sid} with '{agent_name}'")

        async def on_log(stream: str, text: str) -> None:
            await self.send({"type": "log", "session_id": sid, "stream": stream, "text": text})

        if not agent and command.strip():
            # A pure custom command doesn't need the named agent installed —
            # run it with no special env via a minimal synthetic agent.
            agent = ResolvedAgent(name=agent_name or "custom", label="Custom command",
                                  argv=[], env={})
        if not agent:
            await on_log("stderr", f"agent '{agent_name}' is not available on this runner")
            await self.send({"type": "session.end", "session_id": sid,
                             "status": "failed", "exit_code": 127})
            return

        def register(ex: Execution) -> None:
            ex.session_id = sid
            self.executions[sid] = ex

        try:
            # Ralph loop: run the agent with fresh context up to loop_max times,
            # stopping early when loop_until (a shell predicate) exits 0 in cwd, or
            # when the wall-clock budget (max_seconds) is spent — the bound that
            # makes a multi-hour "goal spree" safe to leave unattended.
            rc = 0
            started = time.monotonic()
            for i in range(1, loop_max + 1):
                if max_seconds and (time.monotonic() - started) > max_seconds:
                    mins = round(max_seconds / 60)
                    await on_log("system", f"⏱ wall-clock budget reached (~{mins} min) — stopping after {i - 1} iteration(s)")
                    break
                if loop_max > 1:
                    elapsed = int(time.monotonic() - started)
                    budget = f" · {elapsed // 60}m/{max_seconds // 60}m" if max_seconds else ""
                    await on_log("system", f"━━━━━ iteration {i}/{loop_max}{budget} ━━━━━")
                rc = await run_agent(agent, prompt, cwd, on_log, register,
                                     resume_of=resume_of if i == 1 else "",
                                     auto_approve=self.cfg.auto_approve,
                                     command=command)
                if loop_max == 1:
                    break
                if loop_until:
                    done = await self._loop_done(loop_until, cwd, on_log)
                    if done:
                        await on_log("system", f"✓ stop condition met after iteration {i}")
                        break
                    if i < loop_max:
                        await on_log("system", "stop condition not met — looping with fresh context")
                if i >= loop_max:
                    await on_log("system", f"reached max iterations ({loop_max})")
            status = "success" if rc == 0 else "failed"
            await self.send({"type": "session.end", "session_id": sid,
                             "status": status, "exit_code": rc})
            self.log(f"session {sid} finished: {status} (exit {rc})")
        except asyncio.CancelledError:
            await self.send({"type": "session.end", "session_id": sid,
                             "status": "cancelled", "exit_code": None})
            raise
        except Exception as e:
            await on_log("stderr", f"runner error: {e}")
            await self.send({"type": "session.end", "session_id": sid,
                             "status": "failed", "exit_code": 1})
        finally:
            self.executions.pop(sid, None)
            self.tasks.pop(sid, None)

    async def _loop_done(self, predicate: str, cwd: str, on_log) -> bool:
        """Run the loop-stop predicate (a shell command) in cwd. Exit 0 = stop."""
        workdir = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", predicate, cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if out:
                await on_log("system", f"[stop-check] {out.decode('utf-8','replace').strip()[:200]}")
            return proc.returncode == 0
        except OSError as e:
            await on_log("stderr", f"stop-check failed: {e}")
            return False

    async def _cancel(self, sid: str) -> None:
        ex = self.executions.get(sid)
        if ex:
            self.log(f"cancelling session {sid}")
            await ex.cancel()
        task = self.tasks.get(sid)
        if task:
            task.cancel()

    def stop(self) -> None:
        self._stop = True
