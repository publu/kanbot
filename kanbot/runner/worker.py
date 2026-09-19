"""The background runner: connects to the KanBot server over WebSocket,
advertises which CLI agents are installed locally, and executes assigned tasks.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import sys
import time
from typing import Dict, Optional, Set

import websockets

from ..config import Config
from . import worktrees
from .agents import Execution, ResolvedAgent, build_argv, detect_agents, run_agent
from .api import ApiServer
from .discovery import discover_all
from .panes import Pane, PaneManager, keys_to_bytes

# The DRIVER: a second, read-only agent that keeps the working agent moving. After
# a grind pass it inspects the real repo and hands back concrete, grounded next
# actions — so the worker never coasts to a stop on "the rest is straightforward".
DRIVER_PROMPT = (
    "You are the DRIVER for an autonomous coding run — a second agent whose only "
    "job is to keep the working agent MOVING with real, concrete next steps.\n\n"
    "Inspect the actual state RIGHT NOW: read PROGRESS.md, run `git log --oneline "
    "-10` and `git status`, and open the files that matter. Then output the 3-6 "
    "MOST important concrete next actions toward the goal stated in PROGRESS.md's "
    "## DONE WHEN — name specific files, functions, tests, or commands. If the "
    "working agent has been claiming it's done or stalling, find the SPECIFIC "
    "unfinished work that proves it is NOT done and lead with that.\n"
    "Rules: be concrete and grounded in what you actually see — no platitudes, no "
    "'keep up the good work'. If everything genuinely looks complete, name the "
    "exact command that would prove it. Output ONLY a short bullet list."
)

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
        self.panes: Optional[PaneManager] = None      # created inside the event loop
        self.api: Optional[ApiServer] = None
        self._server_subs: Dict[str, object] = {}      # pane_id -> subscriber callback
        self.swarm = None

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
        try:
            await self._run_forever()
        finally:
            if self.swarm:
                await self.swarm.stop()
            if self.panes:
                for pane in list(self.panes.panes.values()):
                    if pane.alive:
                        await pane.terminate()
            if self.api:
                await self.api.stop()

    async def _run_forever(self) -> None:
        if not self.agents:
            self.log("WARNING: no CLI agents detected on PATH. The runner will "
                     "advertise nothing to run. Install one of: claude, codex, "
                     "gemini, opencode, aider, cursor-agent (or 'shell' fallback).")
        await self._boot_panes()
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
        self._server_subs.clear()          # a fresh socket has no subscriptions
        await self._send_panes()

    # -- panes: the runner owns a PTY per agent ----------------------------
    async def _boot_panes(self) -> None:
        from .api import sock_path
        self.panes = PaneManager(on_line=self._pane_line, on_state=self._pane_state,
                                 sock_path=sock_path())
        from ..swarm import Swarm
        self.swarm = Swarm(self.panes)
        self.api = ApiServer(self.panes, starter=self._api_start, extension=self.swarm.handle)
        try:
            await self.api.start()
            self.log(f"socket API at {self.api.path}  (kanbot agent list · kanbot attach)")
        except OSError as e:
            self.log(f"socket API unavailable: {e}")
            raise
        if self.swarm.store.config and not self.swarm.store.get("config", "paused", True):
            try:
                await self.swarm.start()
            except Exception as error:
                self.swarm.last_error = str(error)
                self.log("swarm connection unavailable; use kanbot swarm start after resolving it")

    async def _send_panes(self) -> None:
        if self.panes:
            self.panes.prune()
            await self.send({"type": "panes", "runner_id": self.cfg.runner_id,
                             "panes": self.panes.list()})

    async def _pane_line(self, pane: Pane, text: str) -> None:
        if pane.session_id:
            await self.send({"type": "log", "session_id": pane.session_id,
                             "stream": "stdout", "text": text})

    async def _pane_state(self, pane: Pane, state: str) -> None:
        await self.send({"type": "pane.updated", "runner_id": self.cfg.runner_id,
                         "pane": pane.info()})
        if state == "blocked" or (state == "done" and pane.interactive):
            asyncio.create_task(self._notify(pane, state))

    async def _notify(self, pane: Pane, state: str) -> None:
        """Tell the human. Default: a macOS banner; override with notify_command."""
        title = "Agent needs you" if state == "blocked" else "Agent finished"
        body = f"{pane.agent}: {pane.title}"[:200]
        cmd = self.cfg.notify_command
        if not cmd and sys.platform == "darwin":
            cmd = 'osascript -e "display notification \"$KANBOT_BODY\" with title \"$KANBOT_TITLE\""'
        if not cmd:
            return
        env = os.environ.copy()
        env.update({"KANBOT_TITLE": title, "KANBOT_BODY": body, "KANBOT_STATE": state,
                    "KANBOT_PANE_ID": pane.id, "KANBOT_AGENT": pane.agent})
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=20)
        except Exception as e:  # noqa: BLE001
            self.log(f"notify failed: {e}")

    def start_pane(self, agent_name: str, prompt: str = "", cwd: str = "",
                   interactive: bool = True, resume_of: str = "", title: str = "",
                   command: str = "") -> Pane:
        """Open an agent in a new pane right now (no card, no queue)."""
        assert self.panes is not None
        agent = self.agents.get(agent_name)
        if not agent:
            raise LookupError(f"agent '{agent_name}' is not available on this runner")
        argv = build_argv(agent, prompt, resume_of, auto_approve=self.cfg.auto_approve,
                          command=command, interactive=interactive)
        env = dict(agent.env)
        env.update(self.cfg.key_env_for(agent.name))
        pane = self.panes.spawn(argv, cwd=cwd or os.getcwd(), env=env, agent=agent.name,
                                title=title or prompt[:80] or agent.label,
                                interactive=interactive)
        if interactive and prompt and "{prompt}" not in " ".join(agent.tui_argv):
            async def typeit():
                await asyncio.sleep(1.5)
                pane.write(prompt.encode() + b"\r")
            asyncio.create_task(typeit())
        asyncio.create_task(self._send_panes())
        return pane

    async def _api_start(self, p: dict) -> dict:
        pane = self.start_pane(p.get("agent", "claude"), p.get("prompt", ""), p.get("cwd", ""),
                               bool(p.get("interactive", True)), p.get("resume", "") or "",
                               p.get("title", ""), p.get("command", "") or "")
        return {"agent": pane.info()}

    async def _pane_msg(self, msg: dict) -> None:
        """Server-relayed pane control: subscribe/unsubscribe/input/resize/kill/start."""
        assert self.panes is not None
        mtype = msg["type"]
        if mtype == "pane.start":
            try:
                pane = self.start_pane(msg.get("agent", "claude"), msg.get("prompt", ""),
                                       msg.get("cwd", ""), bool(msg.get("interactive", True)),
                                       msg.get("resume", "") or "", msg.get("title", ""))
                await self.send({"type": "pane.reply", "request_id": msg.get("request_id"),
                                 "pane": pane.info()})
            except Exception as e:  # noqa: BLE001
                await self.send({"type": "pane.reply", "request_id": msg.get("request_id"),
                                 "error": str(e)})
            return
        pane = self.panes.get(msg.get("pane_id", ""))
        if not pane:
            return
        if mtype == "pane.subscribe":
            if pane.id in self._server_subs:
                return
            pid = pane.id

            def on_data(data: bytes, pid=pid) -> None:
                asyncio.ensure_future(self.send({"type": "pane.data", "pane_id": pid,
                                                 "data": base64.b64encode(data).decode()}))
            pane.subscribers.append(on_data)
            self._server_subs[pid] = on_data
            pane.seen_at = time.time()
            await self.send({"type": "pane.data", "pane_id": pid, "replay": True,
                             "data": base64.b64encode(bytes(pane.buf)).decode()})
        elif mtype == "pane.unsubscribe":
            cb = self._server_subs.pop(pane.id, None)
            if cb in pane.subscribers:
                pane.subscribers.remove(cb)
        elif mtype == "pane.input":
            if "data" in msg:
                pane.write(base64.b64decode(msg["data"]))
            elif "text" in msg:
                pane.write(msg["text"].encode() + (b"\r" if msg.get("enter", True) else b""))
            elif "keys" in msg:
                pane.write(keys_to_bytes(msg["keys"]))
        elif mtype == "pane.resize":
            pane.resize(int(msg.get("rows", 40)), int(msg.get("cols", 140)))
        elif mtype == "pane.kill":
            asyncio.create_task(pane.terminate())
        elif mtype == "pane.read":
            await self.send({"type": "pane.reply", "request_id": msg.get("request_id"),
                             "text": pane.read_text(int(msg.get("lines", 60))),
                             "pane": pane.info()})
        elif mtype == "pane.remove":
            self.panes.remove(pane.id)
            await self._send_panes()

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
            await self._send_panes()
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
            elif mtype == "merge":
                asyncio.create_task(self._merge(msg))
            elif mtype.startswith("pane."):
                await self._pane_msg(msg)
            elif mtype in ("welcome", "pong"):
                pass

    def _spawn_task(self, msg: dict) -> None:
        sid = msg["session_id"]
        task = asyncio.create_task(self._execute(msg))
        self.tasks[sid] = task

    async def _execute(self, msg: dict) -> None:
        sid = msg["session_id"]
        card_id = msg.get("card_id", "")
        agent_name = msg.get("agent", "")
        prompt = msg.get("prompt", "")
        cwd = msg.get("cwd", "")
        isolate = bool(msg.get("isolate"))
        wt = None
        resume_of = msg.get("resume_of", "")
        command = msg.get("command", "") or ""
        interactive = bool(msg.get("interactive"))
        title = msg.get("title", "") or ""
        loop_max = min(MAX_LOOP_ITERATIONS, max(1, int(msg.get("loop_max", 1) or 1)))
        if interactive:
            loop_max = 1            # a TUI is a conversation, not a grind loop
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

        # Devin-style isolation: run this card on its own branch in a throwaway
        # worktree so it never collides with other agents in the same checkout.
        # Best-effort — a non-git cwd just runs in place.
        if isolate and cwd:
            wt = await asyncio.to_thread(worktrees.prepare, cwd, card_id or sid)
            if wt:
                cwd = wt["workdir"]
                await on_log("system", f"⑃ isolated · branch {wt['branch']} · {cwd}")
                await self.send({"type": "worktree", "session_id": sid, "card_id": card_id,
                                 "runner_id": self.cfg.runner_id, "branch": wt["branch"],
                                 "worktree": cwd, "base": wt["base"]})
            else:
                await on_log("system", "⑃ isolate requested but cwd isn't a git repo — running in place")

        try:
            # Ralph loop: run the agent with fresh context up to loop_max times,
            # stopping early when loop_until (a shell predicate) exits 0 in cwd, or
            # when the wall-clock budget (max_seconds) is spent — the bound that
            # makes a multi-hour "goal spree" safe to leave unattended.
            rc = 0
            started = time.monotonic()
            last_fp = await self._progress_fingerprint(cwd) if loop_max > 1 else ""
            stale = 0
            STALL_LIMIT = 4   # consecutive no-progress iterations (even with a driver) before we bail
            # The driver only makes sense for a grind loop with a durable ledger.
            drive = bool(loop_until) and loop_max > 1
            driver_block = ""
            for i in range(1, loop_max + 1):
                if max_seconds and (time.monotonic() - started) > max_seconds:
                    mins = round(max_seconds / 60)
                    await on_log("system", f"⏱ wall-clock budget reached (~{mins} min) — stopping after {i - 1} iteration(s)")
                    break
                if loop_max > 1:
                    elapsed = int(time.monotonic() - started)
                    budget = f" · {elapsed // 60}m/{max_seconds // 60}m" if max_seconds else ""
                    await on_log("system", f"━━━━━ iteration {i}/{loop_max}{budget} ━━━━━")
                rc = await run_agent(agent, prompt + driver_block, cwd, on_log, register,
                                     self.panes, resume_of=resume_of if i == 1 else "",
                                     auto_approve=self.cfg.auto_approve,
                                     command=command, interactive=interactive,
                                     session_id=sid, card_id=card_id, title=title)
                if loop_max == 1:
                    break
                if loop_until:
                    done = await self._loop_done(loop_until, cwd, on_log)
                    if done:
                        await on_log("system", f"✓ stop condition met after iteration {i}")
                        break
                    # Checkpoint the iteration's work and check it actually moved.
                    fp = await self._progress_fingerprint(cwd)
                    progressed = not (fp and fp == last_fp)
                    if progressed:
                        stale = 0; last_fp = fp
                    else:
                        stale += 1
                    if i >= loop_max:
                        await on_log("system", f"reached max iterations ({loop_max})")
                        break
                    # DRIVER: a second read-only agent inspects the repo and hands the
                    # worker concrete, grounded next actions for the next pass — so it
                    # never coasts to a stop. Run it when stalled, or periodically to
                    # keep momentum. Backstop: if even the driver can't move it for
                    # STALL_LIMIT passes, stop instead of spinning.
                    if not progressed and stale >= STALL_LIMIT:
                        await on_log("system", f"⚠ no progress for {STALL_LIMIT} passes even with the driver — stopping so the run doesn't spin. See ## BLOCKERS in PROGRESS.md.")
                        rc = 1
                        break
                    if drive and (not progressed or i % 2 == 0):
                        await on_log("system", "🫱 driver: inspecting the repo for concrete next actions…")
                        tips = await self._drive(agent, cwd, on_log)
                        if tips:
                            driver_block = ("\n\n--- DRIVER (a second agent inspected the "
                                            "repo just now and found concrete next work; do "
                                            "the first item that isn't done) ---\n" + tips
                                            + "\n--- end driver ---")
                            head = " ".join(tips.split())[:150]
                            await on_log("system", f"driver → {head}")
                        else:
                            driver_block = ""
                    msg = ("stop condition not met — looping with fresh context"
                           if progressed else
                           f"no progress this pass ({stale}/{STALL_LIMIT}) — driver re-aiming the next pass")
                    await on_log("system", msg)
                elif i >= loop_max:
                    await on_log("system", f"reached max iterations ({loop_max})")
            if wt:
                # Commit whatever the agent left, then hand the card its diffstat.
                await asyncio.to_thread(worktrees.commit_all, cwd, "deckhand: session work")
                ds = await asyncio.to_thread(worktrees.summary, cwd, wt["base"])
                await self.send({"type": "worktree.diff", "session_id": sid,
                                 "card_id": card_id, "diffstat": ds})
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

    async def _drive(self, agent: ResolvedAgent, cwd: str, on_log) -> str:
        """Run the read-only DRIVER agent in cwd and return its concrete next-action
        notes (capped). Empty string on any failure — the loop must never depend on
        the driver succeeding."""
        if not (cwd and os.path.isdir(cwd)):
            return ""
        try:
            argv = build_argv(agent, DRIVER_PROMPT, auto_approve=False)  # safe/read-only
            env = os.environ.copy(); env.update(agent.env)
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError:
            return ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=150)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            return ""
        return (out or b"").decode("utf-8", "replace").strip()[-2000:]

    async def _sh(self, cmd: str, cwd: str) -> tuple:
        """Run a shell command in cwd, return (rc, stdout)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            return proc.returncode, out.decode("utf-8", "replace")
        except OSError:
            return 1, ""

    async def _progress_fingerprint(self, cwd: str) -> str:
        """A signature of real progress in a repo: HEAD commit + a hash of any
        tracked-file changes + the PROGRESS.md contents. Used to (a) checkpoint
        uncommitted work so a fresh-context loop never loses it, and (b) detect a
        genuinely stuck run before it burns the whole budget."""
        if not (cwd and os.path.isdir(cwd)):
            return ""
        rc, _ = await self._sh("git rev-parse --git-dir", cwd)
        if rc != 0:
            return ""
        # Safety net: if the agent left work uncommitted, commit it so the next
        # fresh iteration (and any crash) keeps it.
        _, dirty = await self._sh("git status --porcelain", cwd)
        if dirty.strip():
            await self._sh("git add -A && git -c user.email=kanbot@local "
                           "-c user.name=kanbot commit -q -m "
                           "'kanbot: checkpoint (agent left work uncommitted)'", cwd)
        _, head = await self._sh("git rev-parse HEAD 2>/dev/null", cwd)
        prog = ""
        p = os.path.join(cwd, "PROGRESS.md")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    prog = fh.read()
            except OSError:
                pass
        import hashlib
        return hashlib.sha1((head.strip() + "\n" + prog).encode("utf-8", "replace")).hexdigest()

    async def _merge(self, msg: dict) -> None:
        """Fold a card's isolated branch back into the real repo, then drop the
        worktree. Runs on the runner that owns the worktree (its filesystem)."""
        res = await asyncio.to_thread(worktrees.merge, msg.get("cwd", ""),
                                      msg.get("branch", ""), msg.get("worktree", ""))
        await self.send({"type": "merge.result", "card_id": msg.get("card_id", ""), **res})

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
