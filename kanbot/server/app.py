"""Deckhand FastAPI application: REST API, realtime WebSockets, and static UI."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time as _time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..agents import catalog
from ..profiles import list_profiles
from ..distill import claude_available, distill_template
from ..workflows import extract_workflows, starter_templates, suggest_automations
from .db import DB, now
from .hub import Hub, RunnerConn
from .insights import PROVIDER_META, compute
from .schemas import (BoardCreate, CardCreate, CardMove, CardPatch, ReviveRequest,
                      TagAttach, TagCreate, UploadRequest, WorkflowClone,
                      WorkflowExtract, WorkflowImport, WorkflowRun, WorkflowSave)

STATIC_DIR = Path(__file__).parent / "static"
SERVER_TOKEN = os.environ.get("KANBOT_TOKEN") or os.environ.get("DECKHAND_TOKEN", "")


def create_app(db_path: Optional[str] = None) -> FastAPI:
    from ..config import config_dir, db_path as default_db_path

    db = DB(Path(db_path) if db_path else default_db_path())
    uploads_dir = config_dir() / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    hub = Hub(db)
    app = FastAPI(title="KanBot", version=__version__)
    app.state.db = db
    app.state.hub = hub

    # Allow a hosted UI (e.g. the Vercel page) to talk to this local server.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def no_cache(request, call_next):
        resp = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    def board_state(board_id: str) -> dict:
        board = db.get_board(board_id)
        if not board:
            raise HTTPException(404, "board not found")
        return {
            "board": board,
            "columns": db.columns(board_id),
            "cards": db.list_cards(board_id),
            "tags": db.list_tags(board_id),
        }

    async def enqueue_if_needed(card: dict, prev_status: str) -> None:
        """If a card landed in the Running column, queue it for a runner."""
        col = db.get_column(card["column_id"])
        if not col:
            return
        kind = col["kind"]
        # Dropping into Running means "run it" -> queue for dispatch (unless it's
        # already executing). Other columns just park the card.
        mapping = {"backlog": "idle", "done": "done"}
        if kind == "running":
            if card["status"] not in ("running", "queued"):
                db.update_card(card["id"], status="queued")
                await hub.try_dispatch()
        elif kind in mapping and card["status"] != "running":
            if mapping[kind] != card["status"]:
                db.update_card(card["id"], status=mapping[kind])

    # -- meta --------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__, "runners": hub.runner_count()}

    @app.get("/api/agents")
    async def agents():
        return {"agents": catalog(), "insights": PROVIDER_META,
                "profiles": list_profiles(), "distill": claude_available()}

    @app.get("/api/runners")
    async def runners():
        return {"runners": db.list_runners()}

    @app.get("/api/agent-sessions")
    async def agent_sessions():
        """Recent Claude/Codex sessions discovered on connected runners,
        including which are actively being worked on right now."""
        return {"sessions": hub.all_agent_sessions()}

    @app.post("/api/boards/{board_id}/revive")
    async def revive(board_id: str, body: ReviveRequest):
        """Adopt an external agent session as a card that resumes it."""
        board = db.get_board(board_id)
        if not board:
            raise HTTPException(404, "board not found")
        title = body.title or f"Resume {body.agent} {body.session_id[:8]}"
        prompt = body.prompt or "Continue where you left off."
        target_kind = "running" if body.run else "backlog"
        col = db.column_by_kind(board_id, target_kind) or db.columns(board_id)[0]
        card = db.create_card(board_id, col["id"], title, prompt, body.agent,
                              body.cwd, resume_of=body.session_id,
                              pin_runner=body.runner_id)
        if body.run:
            db.update_card(card["id"], status="queued")
            card = db.get_card(card["id"])
        await hub.broadcast({"type": "card.created", "card": card})
        if body.run:
            await hub.try_dispatch()
        return card

    # -- boards ------------------------------------------------------------
    @app.get("/api/boards")
    async def list_boards():
        return {"boards": db.list_boards()}

    @app.post("/api/boards")
    async def create_board(body: BoardCreate):
        board = db.create_board(body.name, body.repo_path)
        await hub.broadcast({"type": "board.created", "board": board})
        return board_state(board["id"])

    @app.get("/api/boards/{board_id}")
    async def get_board(board_id: str):
        return board_state(board_id)

    @app.delete("/api/boards/{board_id}")
    async def delete_board(board_id: str):
        db.delete_board(board_id)
        await hub.broadcast({"type": "board.deleted", "board_id": board_id})
        return {"ok": True}

    # -- cards -------------------------------------------------------------
    @app.post("/api/boards/{board_id}/cards")
    async def create_card(board_id: str, body: CardCreate):
        board = db.get_board(board_id)
        if not board:
            raise HTTPException(404, "board not found")
        column_id = body.column_id
        if not column_id:
            col = db.column_by_kind(board_id, "backlog") or db.columns(board_id)[0]
            column_id = col["id"]
        cwd = body.cwd or board.get("repo_path", "")
        card = db.create_card(board_id, column_id, body.title, body.prompt,
                              body.agent, cwd, loop_max=body.loop_max,
                              loop_until=body.loop_until, profile=body.profile,
                              command=body.command)
        await hub.broadcast({"type": "card.created", "card": card})
        await enqueue_if_needed(card, "idle")
        return card

    @app.patch("/api/cards/{card_id}")
    async def patch_card(card_id: str, body: CardPatch):
        fields = {k: v for k, v in body.dict().items() if v is not None}
        if "auto_advance" in fields:
            fields["auto_advance"] = 1 if fields["auto_advance"] else 0
        card = db.update_card(card_id, **fields)
        if not card:
            raise HTTPException(404, "card not found")
        await hub.broadcast({"type": "card.updated", "card": card})
        return card

    @app.post("/api/cards/{card_id}/move")
    async def move_card(card_id: str, body: CardMove):
        prev = db.get_card(card_id)
        if not prev:
            raise HTTPException(404, "card not found")
        card = db.move_card(card_id, body.column_id, body.position)
        await enqueue_if_needed(card, prev["status"])
        card = db.get_card(card_id)
        await hub.broadcast({"type": "card.updated", "card": card})
        return card

    @app.post("/api/cards/{card_id}/run")
    async def run_card(card_id: str):
        card = db.get_card(card_id)
        if not card:
            raise HTTPException(404, "card not found")
        col = db.column_by_kind(card["board_id"], "running")
        if not col:
            raise HTTPException(400, "board has no running column")
        card = db.move_card(card_id, col["id"], db._next_position(col["id"]))
        db.update_card(card_id, status="queued")
        card = db.get_card(card_id)
        await hub.broadcast({"type": "card.updated", "card": card})
        await hub.try_dispatch()
        return card

    @app.delete("/api/cards/{card_id}")
    async def delete_card(card_id: str):
        db.delete_card(card_id)
        await hub.broadcast({"type": "card.deleted", "card_id": card_id})
        return {"ok": True}

    @app.get("/api/cards/{card_id}/insights")
    async def card_insights(card_id: str):
        card = db.get_card(card_id)
        if not card:
            raise HTTPException(404, "card not found")
        results = []
        for tag in card.get("tags", []):
            if tag.get("insight"):
                res = compute(card, tag["insight"], tag.get("config") or {})
                res["tag"] = {"id": tag["id"], "name": tag["name"], "color": tag["color"]}
                results.append(res)
        return {"insights": results}

    # -- workflows ---------------------------------------------------------
    @app.get("/api/workflow-templates")
    async def workflow_templates():
        """The built-in starter library — instantiate or import into any board."""
        return {"templates": starter_templates()}

    @app.get("/api/boards/{board_id}/workflows")
    async def list_workflows(board_id: str):
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        return {"workflows": db.list_workflows(board_id)}

    @app.post("/api/boards/{board_id}/workflows")
    async def create_workflow(board_id: str, body: WorkflowSave):
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        wf = db.save_workflow(board_id, body.name, body.description, body.agent,
                              body.cwd, [s.dict() for s in body.steps])
        await hub.broadcast({"type": "workflow.saved", "board_id": board_id, "workflow": wf})
        return wf

    @app.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str):
        wf = db.get_workflow(workflow_id)
        if not wf:
            raise HTTPException(404, "workflow not found")
        return wf

    @app.put("/api/workflows/{workflow_id}")
    async def update_workflow(workflow_id: str, body: WorkflowSave):
        existing = db.get_workflow(workflow_id)
        if not existing:
            raise HTTPException(404, "workflow not found")
        wf = db.save_workflow(existing["board_id"], body.name, body.description,
                              body.agent, body.cwd, [s.dict() for s in body.steps],
                              workflow_id=workflow_id)
        await hub.broadcast({"type": "workflow.saved", "board_id": wf["board_id"], "workflow": wf})
        return wf

    @app.delete("/api/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: str):
        wf = db.get_workflow(workflow_id)
        if not wf:
            raise HTTPException(404, "workflow not found")
        db.delete_workflow(workflow_id)
        await hub.broadcast({"type": "workflow.deleted", "board_id": wf["board_id"],
                             "workflow_id": workflow_id})
        return {"ok": True}

    @app.get("/api/workflows/{workflow_id}/export")
    async def export_workflow(workflow_id: str):
        """A portable, id-free template dict — share it or import it elsewhere."""
        tpl = db.workflow_template(workflow_id)
        if not tpl:
            raise HTTPException(404, "workflow not found")
        return tpl

    @app.post("/api/workflows/{workflow_id}/clone")
    async def clone_workflow(workflow_id: str, body: WorkflowClone):
        wf = db.clone_workflow(workflow_id, body.name or None)
        if not wf:
            raise HTTPException(404, "workflow not found")
        await hub.broadcast({"type": "workflow.saved", "board_id": wf["board_id"], "workflow": wf})
        return wf

    @app.post("/api/boards/{board_id}/workflows/import")
    async def import_workflow(board_id: str, body: WorkflowImport):
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        t = body.template or {}
        if not t.get("name") or not isinstance(t.get("steps"), list):
            raise HTTPException(400, "template needs a name and a steps list")
        wf = db.save_workflow(board_id, t["name"], t.get("description", ""),
                              t.get("agent", "auto"), t.get("cwd", ""), t["steps"])
        await hub.broadcast({"type": "workflow.saved", "board_id": board_id, "workflow": wf})
        return wf

    @app.post("/api/boards/{board_id}/workflows/extract")
    async def extract_workflow(board_id: str, body: WorkflowExtract):
        """Extract workflow(s) from one or more discovered Claude/Codex sessions.

        A session is not always one workflow: split=True segments by topic into
        several candidate workflows; passing multiple session_ids merges them
        (in order) into one extraction. Returns previews by default; save=True
        persists them."""
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        ids = body.session_ids or ([body.session_id] if body.session_id else [])
        if not ids:
            raise HTTPException(400, "need session_id or session_ids")
        by_id = {s.get("session_id"): s for s in hub.all_agent_sessions()}
        sessions = [by_id[i] for i in ids if i in by_id]
        if not sessions:
            raise HTTPException(404, "no matching agent sessions")
        templates = extract_workflows(sessions, split=body.split)
        if not body.save:
            return {"segments": templates}
        saved = []
        for tpl in templates:
            wf = db.save_workflow(board_id, tpl["name"], tpl["description"],
                                  tpl["agent"], tpl["cwd"], tpl["steps"])
            saved.append(wf)
            await hub.broadcast({"type": "workflow.saved", "board_id": board_id, "workflow": wf})
        return {"workflows": saved}

    @app.post("/api/boards/{board_id}/workflows/suggest")
    async def suggest_workflows(board_id: str):
        """Analyze every discovered session and propose automations to save."""
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        return {"suggestions": suggest_automations(hub.all_agent_sessions())}

    @app.post("/api/workflows/distill")
    async def distill_workflow(body: WorkflowImport):
        """Use the local `claude` CLI to turn a raw, session-derived draft into a
        clean reusable workflow with short, generalized, guided step prompts."""
        if not claude_available():
            raise HTTPException(503, "claude CLI not found on the server host")
        t = body.template or {}
        if not isinstance(t.get("steps"), list) or not t["steps"]:
            raise HTTPException(400, "template needs steps to distill")
        out = await asyncio.to_thread(distill_template, t)
        if not out:
            raise HTTPException(502, "distillation failed (claude returned nothing usable)")
        return {"template": out, "distilled": True}

    @app.post("/api/workflows/{workflow_id}/run")
    async def run_workflow(workflow_id: str, body: WorkflowRun):
        wf = db.get_workflow(workflow_id)
        if not wf:
            raise HTTPException(404, "workflow not found")
        if not wf.get("steps"):
            raise HTTPException(400, "workflow has no steps")
        card = await hub.start_workflow(wf, cwd=body.cwd, title=body.title, run=body.run)
        return card

    # -- tags --------------------------------------------------------------
    @app.post("/api/boards/{board_id}/tags")
    async def create_tag(board_id: str, body: TagCreate):
        tag = db.create_tag(board_id, body.name, body.color, body.insight, body.config)
        await hub.broadcast({"type": "tag.created", "board_id": board_id, "tag": tag})
        return tag

    @app.delete("/api/tags/{tag_id}")
    async def delete_tag(tag_id: str):
        db.delete_tag(tag_id)
        await hub.broadcast({"type": "tag.deleted", "tag_id": tag_id})
        return {"ok": True}

    @app.post("/api/cards/{card_id}/tags")
    async def attach_tag(card_id: str, body: TagAttach):
        db.add_card_tag(card_id, body.tag_id)
        card = db.get_card(card_id)
        await hub.broadcast({"type": "card.updated", "card": card})
        return card

    @app.delete("/api/cards/{card_id}/tags/{tag_id}")
    async def detach_tag(card_id: str, tag_id: str):
        db.remove_card_tag(card_id, tag_id)
        card = db.get_card(card_id)
        await hub.broadcast({"type": "card.updated", "card": card})
        return card

    # -- sessions ----------------------------------------------------------
    @app.get("/api/sessions")
    async def list_sessions(board_id: Optional[str] = None, card_id: Optional[str] = None):
        return {"sessions": db.list_sessions(board_id=board_id, card_id=card_id)}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, after: int = 0):
        sess = db.get_session(session_id)
        if not sess:
            raise HTTPException(404, "session not found")
        return {"session": sess, "events": db.events(session_id, after_id=after)}

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str):
        await hub.cancel_session(session_id)
        return {"ok": True}

    # -- websockets --------------------------------------------------------
    @app.websocket("/ws/web")
    async def ws_web(ws: WebSocket):
        await ws.accept()
        await hub.add_web(ws)
        try:
            await ws.send_text(json.dumps({"type": "hello", "version": __version__}))
            while True:
                await ws.receive_text()  # clients are read-only; ignore content
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.remove_web(ws)

    @app.websocket("/ws/runner")
    async def ws_runner(ws: WebSocket):
        token = ws.query_params.get("token", "")
        if SERVER_TOKEN and token != SERVER_TOKEN:
            await ws.close(code=4401)
            return
        await ws.accept()
        conn: Optional[RunnerConn] = None
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "hello":
                    rid = msg.get("runner_id") or msg.get("id") or "runner"
                    conn = RunnerConn(rid, ws)
                    conn.name = msg.get("name", rid)
                    conn.host = msg.get("host", "")
                    conn.capabilities = msg.get("capabilities", [])
                    conn.max_concurrency = int(msg.get("max_concurrency", 2))
                    conn.auto_approve = bool(msg.get("auto_approve", True))
                    await hub.register_runner(conn)
                    await ws.send_text(json.dumps({"type": "welcome", "runner_id": rid}))
                elif mtype == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                    if conn:
                        db.set_runner_status(conn.runner_id, "busy" if conn.free_slots == 0 else "online")
                elif mtype == "session.start":
                    await hub.session_started(msg["session_id"])
                elif mtype == "log":
                    await hub.session_log(msg["session_id"], msg.get("stream", "stdout"),
                                          msg.get("text", ""))
                elif mtype == "session.end":
                    status = msg.get("status", "success")
                    await hub.finish_session(msg["session_id"], status=status,
                                             exit_code=msg.get("exit_code"))
                elif mtype == "agent.sessions" and conn:
                    await hub.set_agent_sessions(conn.runner_id, msg.get("sessions", []))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if conn:
                await hub.deregister_runner(conn.runner_id)

    # -- image uploads (paste/drag on the board) ---------------------------
    @app.post("/api/uploads")
    async def upload(body: UploadRequest):
        """Save a pasted/dropped image and return a local path the agent can read."""
        data = body.data or ""
        mime, b64 = "image/png", data
        if data.startswith("data:"):
            head, _, b64 = data.partition(",")
            mime = head[5:].split(";", 1)[0] or mime
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HTTPException(400, "invalid image data")
        if len(raw) > 25 * 1024 * 1024:
            raise HTTPException(413, "image too large (max 25MB)")
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
               "image/webp": "webp"}.get(mime, "png")
        fname = f"{int(_time.time())}-{os.urandom(3).hex()}.{ext}"
        path = uploads_dir / fname
        path.write_bytes(raw)
        return {"path": str(path), "url": f"/uploads/{fname}", "name": body.name}

    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    # -- static UI ---------------------------------------------------------
    if STATIC_DIR.exists():
        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
