"""Realtime hub: tracks connected web clients and runners, and schedules work.

- Web clients connect to /ws/web and receive a live event stream of board changes
  and session logs.
- Runners connect to /ws/runner, advertise their capabilities, and are handed
  queued cards to execute.

The scheduler is intentionally simple: whenever a card lands in a "queued" column,
or a runner becomes available, we try to match queued cards to idle runner slots.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Set

from ..profiles import compose_prompt
from .db import DB


class RunnerConn:
    def __init__(self, runner_id: str, ws):
        self.runner_id = runner_id
        self.ws = ws
        self.name: str = runner_id
        self.host: str = ""
        self.capabilities: List[str] = []
        self.max_concurrency: int = 2
        self.auto_approve: bool = True
        self.active: Set[str] = set()  # session ids currently running

    @property
    def free_slots(self) -> int:
        return max(0, self.max_concurrency - len(self.active))

    def can_run(self, agent: str) -> bool:
        if self.free_slots <= 0:
            return False
        if agent == "auto":
            return bool(self.capabilities)
        return agent in self.capabilities

    def pick_agent(self, requested: str) -> Optional[str]:
        if requested != "auto":
            return requested if requested in self.capabilities else None
        # Prefer a real coding agent over the raw shell fallback.
        preferred = [c for c in self.capabilities if c != "shell"]
        if preferred:
            return preferred[0]
        return self.capabilities[0] if self.capabilities else None


class Hub:
    def __init__(self, db: DB):
        self.db = db
        self.web: Set[Any] = set()
        self.runners: Dict[str, RunnerConn] = {}
        self.agent_sessions: Dict[str, List[dict]] = {}  # runner_id -> discovered sessions
        self._lock = asyncio.Lock()

    # -- web clients -------------------------------------------------------
    async def add_web(self, ws) -> None:
        self.web.add(ws)

    def remove_web(self, ws) -> None:
        self.web.discard(ws)

    async def broadcast(self, event: dict) -> None:
        """Push an event to every connected web client."""
        if not self.web:
            return
        msg = json.dumps(event)
        dead = []
        for ws in list(self.web):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.web.discard(ws)

    # -- runners -----------------------------------------------------------
    async def register_runner(self, conn: RunnerConn) -> None:
        self.runners[conn.runner_id] = conn
        self.db.upsert_runner(
            conn.runner_id, conn.name, conn.host, conn.capabilities, conn.max_concurrency,
            auto_approve=conn.auto_approve,
        )
        await self.broadcast({"type": "runner.updated", "runner": self.db.get_runner(conn.runner_id)})
        await self.try_dispatch()

    async def set_agent_sessions(self, runner_id: str, sessions: List[dict]) -> None:
        conn = self.runners.get(runner_id)
        rname = conn.name if conn else runner_id
        for s in sessions:
            s["runner_id"] = runner_id
            s["runner_name"] = rname
        self.agent_sessions[runner_id] = sessions
        await self.broadcast({"type": "agent.sessions.updated"})

    def all_agent_sessions(self) -> List[dict]:
        out: List[dict] = []
        for sessions in self.agent_sessions.values():
            out.extend(sessions)
        out.sort(key=lambda s: s.get("mtime", 0), reverse=True)
        return out

    async def deregister_runner(self, runner_id: str) -> None:
        conn = self.runners.pop(runner_id, None)
        self.agent_sessions.pop(runner_id, None)
        self.db.set_runner_status(runner_id, "offline", active=0)
        await self.broadcast({"type": "runner.updated", "runner": self.db.get_runner(runner_id)})
        await self.broadcast({"type": "agent.sessions.updated"})
        # Any sessions left mid-flight are marked failed so cards don't hang.
        if conn:
            for sid in list(conn.active):
                sess = self.db.get_session(sid)
                if sess and sess["status"] in ("assigned", "running"):
                    self.db.add_event(sid, "system", "Runner disconnected; session aborted.")
                    await self.finish_session(sid, status="failed", exit_code=None)

    def runner_count(self) -> int:
        return len(self.runners)

    def available_agents(self) -> List[str]:
        """Union of agent capabilities advertised by connected runners."""
        names: List[str] = []
        for r in self.runners.values():
            for c in r.capabilities:
                if c not in names:
                    names.append(c)
        return names

    # -- scheduling --------------------------------------------------------
    async def try_dispatch(self) -> None:
        """Match queued cards against available runner slots."""
        async with self._lock:
            boards = self.db.list_boards()
            for board in boards:
                queued = self.db.cards_with_status(board["id"], "queued")
                for card in queued:
                    runner = self._find_runner(card["agent"], card.get("pin_runner") or "")
                    if not runner:
                        continue
                    await self._assign(card, runner)

    def _find_runner(self, agent: str, pin_runner: str = "") -> Optional[RunnerConn]:
        if pin_runner:
            r = self.runners.get(pin_runner)
            return r if (r and r.can_run(agent)) else None
        candidates = [r for r in self.runners.values() if r.can_run(agent)]
        if not candidates:
            return None
        # Prefer the runner with the most free capacity.
        candidates.sort(key=lambda r: r.free_slots, reverse=True)
        return candidates[0]

    async def _assign(self, card: Dict[str, Any], runner: RunnerConn) -> None:
        agent = runner.pick_agent(card["agent"])
        if not agent:
            return
        session = self.db.create_session(card, agent)
        sid = session["id"]
        self.db.update_session(
            sid, status="assigned", runner_id=runner.runner_id, runner_name=runner.name
        )
        runner.active.add(sid)
        self.db.update_card(card["id"], status="running")
        self.db.set_runner_status(
            runner.runner_id,
            "busy" if runner.free_slots == 0 else "online",
            active=len(runner.active),
        )
        # Move the card into the Running column.
        running_col = self.db.column_by_kind(card["board_id"], "running")
        if running_col:
            self.db.move_card(card["id"], running_col["id"],
                              self.db._next_position(running_col["id"]))
        payload = {
            "type": "assign",
            "session_id": sid,
            "card_id": card["id"],
            "agent": agent,
            # prompt mode (e.g. 'lean') is folded into the prompt here, so it's
            # re-applied on every fresh-context loop iteration automatically.
            "prompt": compose_prompt(card.get("profile", ""), card.get("prompt", "")),
            "cwd": card.get("cwd", ""),
            "resume_of": card.get("resume_of", "") or "",
            "loop_max": int(card.get("loop_max", 1) or 1),
            "loop_until": card.get("loop_until", "") or "",
            "command": card.get("command", "") or "",
        }
        try:
            await runner.ws.send_text(json.dumps(payload))
        except Exception:
            self.db.add_event(sid, "system", "Failed to deliver task to runner.")
            await self.finish_session(sid, status="failed", exit_code=None)
            return
        await self._emit_card(card["id"])
        await self.broadcast({"type": "session.created", "session": self.db.get_session(sid)})
        await self.broadcast({"type": "runner.updated", "runner": self.db.get_runner(runner.runner_id)})

    # -- session lifecycle (driven by runner messages) --------------------
    async def session_started(self, sid: str) -> None:
        from .db import now
        self.db.update_session(sid, status="running", started_at=now())
        await self.broadcast({"type": "session.updated", "session": self.db.get_session(sid)})

    async def session_log(self, sid: str, stream: str, text: str) -> None:
        ev = self.db.add_event(sid, stream, text)
        await self.broadcast({"type": "session.event", "session_id": sid, "event": ev})

    async def finish_session(self, sid: str, status: str, exit_code: Optional[int]) -> None:
        from .db import now
        sess = self.db.get_session(sid)
        if not sess:
            return
        self.db.update_session(sid, status=status, exit_code=exit_code, ended_at=now())
        card = self.db.get_card(sess["card_id"])
        advanced = False
        if card:
            # Workflow run with another step to take? Queue the next step and
            # keep the card in Running instead of finishing it.
            advanced = await self._advance_workflow(card, status)
            if not advanced:
                # Any finished run (success or failure) leaves Running and lands
                # in Done, carrying its status so the card shows ✓ done / ✗ failed.
                new_status = "done" if status == "success" else status
                self.db.update_card(card["id"], status=new_status)
                col = self.db.column_by_kind(card["board_id"], "done")
                if col:
                    self.db.move_card(card["id"], col["id"], self.db._next_position(col["id"]))
            await self._emit_card(card["id"])
        # Free the runner slot.
        runner = self.runners.get(sess["runner_id"])
        if runner:
            runner.active.discard(sid)
            self.db.set_runner_status(
                runner.runner_id,
                "busy" if runner.free_slots == 0 else "online",
                active=len(runner.active),
            )
            await self.broadcast({"type": "runner.updated", "runner": self.db.get_runner(runner.runner_id)})
        await self.broadcast({"type": "session.updated", "session": self.db.get_session(sid)})
        await self.try_dispatch()

    async def cancel_session(self, sid: str) -> None:
        sess = self.db.get_session(sid)
        if not sess:
            return
        runner = self.runners.get(sess["runner_id"])
        if runner:
            try:
                await runner.ws.send_text(json.dumps({"type": "cancel", "session_id": sid}))
            except Exception:
                pass
        self.db.add_event(sid, "system", "Cancellation requested.")
        await self.finish_session(sid, status="cancelled", exit_code=None)

    async def _emit_card(self, card_id: str) -> None:
        card = self.db.get_card(card_id)
        if card:
            await self.broadcast({"type": "card.updated", "card": card})

    # -- workflows ---------------------------------------------------------
    def _step_card_fields(self, workflow: Dict[str, Any], step: Dict[str, Any],
                          step_index: int, carry_text: str = "") -> Dict[str, Any]:
        """Map a workflow step onto the card fields the dispatcher reads, so the
        existing card->session machinery runs each step unchanged."""
        prompt = step.get("prompt", "") or ""
        if carry_text and step.get("carry_context"):
            prompt = (prompt + "\n\n--- Output from the previous step (for context) ---\n"
                      + carry_text).strip()
        return {
            "agent": step.get("agent") or workflow.get("agent") or "auto",
            "prompt": prompt,
            "profile": step.get("profile", "") or "",
            "command": step.get("command", "") or "",
            "loop_max": max(1, int(step.get("loop_max") or 1)),
            "loop_until": step.get("loop_until", "") or "",
            "step_index": step_index,
        }

    async def start_workflow(self, workflow: Dict[str, Any], cwd: str = "",
                             title: str = "", run: bool = True) -> Dict[str, Any]:
        """Instantiate a workflow as a single run card parked on step 0."""
        board_id = workflow["board_id"]
        steps = workflow.get("steps") or []
        if not steps:
            raise ValueError("workflow has no steps")
        kind = "running" if run else "backlog"
        col = self.db.column_by_kind(board_id, kind) or self.db.columns(board_id)[0]
        f = self._step_card_fields(workflow, steps[0], 0)
        card = self.db.create_card(
            board_id, col["id"], title or workflow["name"], prompt=f["prompt"],
            agent=f["agent"], cwd=cwd or workflow.get("cwd", ""), loop_max=f["loop_max"],
            loop_until=f["loop_until"], profile=f["profile"], command=f["command"],
            workflow_id=workflow["id"], step_index=0,
        )
        await self.broadcast({"type": "card.created", "card": card})
        if run:
            self.db.update_card(card["id"], status="queued")
            card = self.db.get_card(card["id"])
            await self.broadcast({"type": "card.updated", "card": card})
            await self.try_dispatch()
        return card

    def _session_tail_text(self, sid: str, limit: int = 2400) -> str:
        """The last slice of a session's stdout — carried into the next step."""
        evs = self.db.events(sid)
        text = "\n".join(e["text"] for e in evs if e.get("stream") == "stdout")
        return text[-limit:] if len(text) > limit else text

    async def _advance_workflow(self, card: Dict[str, Any], session_status: str) -> bool:
        """If `card` is a workflow run with a next step to take, queue it and
        return True. Returns False to let normal completion (done/failed) run."""
        wf_id = card.get("workflow_id") or ""
        if not wf_id:
            return False
        workflow = self.db.get_workflow(wf_id)
        if not workflow:
            return False
        steps = workflow.get("steps") or []
        cur = int(card.get("step_index") or 0)
        nxt = cur + 1
        if nxt >= len(steps):
            return False
        cur_step = steps[cur]
        ok = session_status == "success"
        if not ok and not cur_step.get("continue_on_fail"):
            return False
        carry = ""
        nxt_step = steps[nxt]
        if nxt_step.get("carry_context"):
            sess = self.db.list_sessions(card_id=card["id"], limit=1)
            if sess:
                carry = self._session_tail_text(sess[0]["id"])
        f = self._step_card_fields(workflow, nxt_step, nxt, carry)
        self.db.update_card(
            card["id"], status="queued", step_index=nxt, prompt=f["prompt"],
            agent=f["agent"], profile=f["profile"], command=f["command"],
            loop_max=f["loop_max"], loop_until=f["loop_until"],
        )
        return True
