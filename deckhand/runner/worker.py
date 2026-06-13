"""The background runner: connects to the Deckhand server over WebSocket,
advertises which CLI agents are installed locally, and executes assigned tasks.
"""
from __future__ import annotations

import asyncio
import json
import platform
from typing import Dict, Optional

import websockets

from ..config import Config
from .agents import Execution, ResolvedAgent, detect_agents, run_agent


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
                    await self._consume()
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
        })

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
        agent = self.agents.get(agent_name)
        await self.send({"type": "session.start", "session_id": sid})
        self.log(f"running session {sid} with '{agent_name}'")

        async def on_log(stream: str, text: str) -> None:
            await self.send({"type": "log", "session_id": sid, "stream": stream, "text": text})

        if not agent:
            await on_log("stderr", f"agent '{agent_name}' is not available on this runner")
            await self.send({"type": "session.end", "session_id": sid,
                             "status": "failed", "exit_code": 127})
            return

        def register(ex: Execution) -> None:
            ex.session_id = sid
            self.executions[sid] = ex

        try:
            rc = await run_agent(agent, prompt, cwd, on_log, register)
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
