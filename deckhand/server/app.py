"""Deckhand FastAPI application: REST API, realtime WebSockets, and static UI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..agents import catalog
from .db import DB, now
from .hub import Hub, RunnerConn
from .insights import PROVIDER_META, compute
from .schemas import (BoardCreate, CardCreate, CardMove, CardPatch, TagAttach,
                      TagCreate)

STATIC_DIR = Path(__file__).parent / "static"
SERVER_TOKEN = os.environ.get("DECKHAND_TOKEN", "")


def create_app(db_path: Optional[str] = None) -> FastAPI:
    from ..config import db_path as default_db_path

    db = DB(Path(db_path) if db_path else default_db_path())
    hub = Hub(db)
    app = FastAPI(title="Deckhand", version=__version__)
    app.state.db = db
    app.state.hub = hub

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
        """If a card landed in a queued column, mark it queued and dispatch."""
        col = db.get_column(card["column_id"])
        if not col:
            return
        kind = col["kind"]
        mapping = {"backlog": "idle", "queued": "queued", "review": "review",
                   "done": "done"}
        if kind in mapping and card["status"] not in ("running",):
            new_status = mapping[kind]
            if new_status != card["status"]:
                db.update_card(card["id"], status=new_status)
        if kind == "queued":
            await hub.try_dispatch()

    # -- meta --------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__, "runners": hub.runner_count()}

    @app.get("/api/agents")
    async def agents():
        return {"agents": catalog(), "insights": PROVIDER_META}

    @app.get("/api/runners")
    async def runners():
        return {"runners": db.list_runners()}

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
                              body.agent, cwd)
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
        col = db.column_by_kind(card["board_id"], "queued")
        if not col:
            raise HTTPException(400, "board has no queued column")
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
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if conn:
                await hub.deregister_runner(conn.runner_id)

    # -- static UI ---------------------------------------------------------
    if STATIC_DIR.exists():
        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
