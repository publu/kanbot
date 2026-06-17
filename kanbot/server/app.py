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
from ..distill import distill_available, distill_workflows, distill_workflows_stream
from ..runner.discovery import all_user_turns
from ..training.evaluator import evaluate_workflow
from ..training.optimizer import run_improvement_pass
from ..workflows import extract_workflows, starter_templates, suggest_automations

EXEMPLAR_BAR = 75   # eval score at/above which a workflow becomes a few-shot exemplar
from .db import DB, gen_id, now
from .hub import Hub, RunnerConn
from .insights import PROVIDER_META, compute
from .schemas import (BoardCreate, BuildRequest, CardCreate, CardMove, CardPatch,
                      FromSession, ImproveRequest, ReviveRequest, TagAttach, TagCreate,
                      UploadRequest, WorkflowClone, WorkflowEval, WorkflowExtract,
                      WorkflowImport, WorkflowRun, WorkflowSave)

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
    async def headers(request, call_next):
        resp = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        # Chrome Private Network Access: let a hosted (public) page like the Vercel
        # demo reach this local server. Without this the connect fetch is blocked.
        if request.headers.get("access-control-request-private-network"):
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
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
                "profiles": list_profiles(),
                "distill": distill_available(hub.available_agents())}

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
                              t.get("agent", "auto"), t.get("cwd", ""), t["steps"],
                              source_tokens=int(t.get("source_tokens") or 0))
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

    @app.post("/api/boards/{board_id}/workflows/from-session")
    async def workflows_from_session(board_id: str, body: FromSession):
        """Deep-read a session's FULL transcript and split it into clean,
        generalized workflows (a session often holds several). Cached per
        (session_id, mtime) so a re-open is instant."""
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        ids = body.session_ids or ([body.session_id] if body.session_id else [])
        if not ids:
            raise HTTPException(400, "need session_id or session_ids")
        by_id = {s.get("session_id"): s for s in hub.all_agent_sessions()}
        sessions = [by_id[i] for i in ids if i in by_id]
        if not sessions:
            raise HTTPException(404, "no matching agent sessions")

        key = tuple((s.get("session_id"), int(s.get("mtime", 0))) for s in sessions)
        if not body.refresh and key in hub.distill_cache:
            wfs = hub.distill_cache[key]
            return {"workflows": wfs, "cached": True,
                    "by": (wfs[0].get("_distilled_by") if wfs else None)}

        # Full transcript turns (all sessions, in order) — the real material.
        turns: list = []
        for s in sessions:
            turns.extend(all_user_turns(s.get("path", ""), s.get("fmt", "claude")))
        if not turns:  # fall back to the cached tail if the file is unreadable
            for s in sessions:
                turns.extend(m.get("text", "") for m in (s.get("tail") or [])
                             if m.get("role") == "user" and m.get("text"))
        turns = [t for t in turns if t]
        if not turns:
            raise HTTPException(422, "no human turns found in that session")

        draft = {
            "agent": sessions[0].get("agent", "auto") or "auto",
            "cwd": sessions[0].get("cwd", "") or "",
            "_context": sessions[0].get("title") or sessions[0].get("recap") or "",
            "steps": [{"prompt": t} for t in turns],
        }
        avail = hub.available_agents()
        if distill_available(avail):
            exemplars = [e["template"] for e in db.top_exemplars(3, board_id)]
            wfs = await asyncio.to_thread(distill_workflows, draft, avail, 300, exemplars)
        else:
            wfs = extract_workflows(sessions, split=True)   # heuristic fallback
        # Part 3 metric: how much conversation each workflow compresses. ~4 chars/token.
        source_tokens = sum(len(t) for t in turns) // 4
        for w in wfs:
            w["source_tokens"] = source_tokens
        hub.distill_cache[key] = wfs
        if len(hub.distill_cache) > 128:        # bound memory on a long run
            hub.distill_cache.pop(next(iter(hub.distill_cache)))
        return {"workflows": wfs, "by": (wfs[0].get("_distilled_by") if wfs else None)}

    @app.post("/api/boards/{board_id}/workflows/build")
    async def build_automations(board_id: str, body: BuildRequest):
        """Auto-analyze the given focus sessions and STREAM the agent's real work
        (per-line stdout) + each extracted workflow over /ws/web as it happens, so
        the UI is a live terminal feed — not a fake spinner."""
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        avail = hub.available_agents()
        if not distill_available(avail):
            raise HTTPException(503, "no reasoning agent available")
        by_id = {s.get("session_id"): s for s in hub.all_agent_sessions()}
        sessions = [by_id[i] for i in (body.session_ids or []) if i in by_id][:5]
        if not sessions:
            raise HTTPException(422, "no matching sessions")
        job = gen_id()
        loop = asyncio.get_running_loop()

        async def run_job():
            try:
                for i, s in enumerate(sessions):
                    cwd = s.get("cwd", "") or ""
                    repo = os.path.basename(cwd.rstrip("/")) if cwd and os.path.isdir(cwd) else ""
                    await hub.broadcast({"type": "build.session", "job": job,
                                         "name": s.get("name") or "session", "repo": repo,
                                         "i": i + 1, "n": len(sessions)})
                    turns = all_user_turns(s.get("path", ""), s.get("fmt", "claude")) or \
                        [m.get("text", "") for m in (s.get("tail") or []) if m.get("role") == "user"]
                    turns = [t for t in turns if t]
                    if not turns:
                        continue
                    src = sum(len(t) for t in turns) // 4
                    draft = {"agent": s.get("agent", "auto") or "auto", "cwd": s.get("cwd", "") or "",
                             "_context": s.get("title") or s.get("recap") or "",
                             "steps": [{"prompt": t} for t in turns]}
                    exemplars = [e["template"] for e in db.top_exemplars(3, board_id)]

                    def on_line(line, _job=job):
                        if line.strip():
                            asyncio.run_coroutine_threadsafe(
                                hub.broadcast({"type": "build.log", "job": _job, "line": line}), loop)

                    wfs = await asyncio.to_thread(distill_workflows_stream, draft, avail, on_line, 300, exemplars)
                    for w in wfs:
                        w["source_tokens"] = src
                        w["_from"] = s.get("name")
                        await hub.broadcast({"type": "build.workflow", "job": job, "workflow": w})
                await hub.broadcast({"type": "build.done", "job": job})
            except Exception as e:
                await hub.broadcast({"type": "build.done", "job": job, "error": str(e)})

        asyncio.create_task(run_job())
        return {"job": job}

    @app.post("/api/boards/{board_id}/workflows/eval")
    async def eval_workflow(board_id: str, body: WorkflowEval):
        """Judge a distilled workflow against its source session (grounded in the
        repo). Logs the eval; if it clears the bar, banks it as a few-shot
        exemplar that steers future distillation (Part 2 self-improvement)."""
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        t = body.template or {}
        if not isinstance(t.get("steps"), list) or not t["steps"]:
            raise HTTPException(400, "template needs steps to evaluate")
        session = next((s for s in hub.all_agent_sessions()
                        if s.get("session_id") == body.session_id), {})
        reduction = int((t.get("source_tokens") or 0) / 25)
        avail = hub.available_agents()
        if not distill_available(avail):
            raise HTTPException(503, "no reasoning agent available to evaluate with")
        res = await asyncio.to_thread(evaluate_workflow, t, session, avail, reduction, 240, body.sandbox)
        if not res:
            raise HTTPException(502, "evaluation failed (agent returned nothing usable)")
        db.log_eval(board_id, body.session_id, t.get("name", ""), res["score"],
                    res["breakdown"], res["critique"], res["critic"])
        banked = False
        if body.keep and res["score"] >= EXEMPLAR_BAR and res["breakdown"].get("verdict") != "reject":
            db.add_exemplar(board_id, t.get("name", ""), {
                "name": t.get("name"), "description": t.get("description"),
                "agent": t.get("agent", "auto"), "cwd": t.get("cwd", ""),
                "steps": t.get("steps"),
            }, res["score"], int(t.get("source_tokens") or 0), res["breakdown"])
            banked = True
        return {**res, "banked_as_exemplar": banked, "bar": EXEMPLAR_BAR}

    @app.get("/api/boards/{board_id}/exemplars")
    async def list_exemplars(board_id: str):
        return {"exemplars": db.list_exemplars(board_id), "evals": db.list_evals(board_id, 30)}

    @app.post("/api/boards/{board_id}/workflows/improve")
    async def improve_workflows(board_id: str, body: ImproveRequest):
        """One self-improvement pass: distill (steered by current exemplars) ->
        evaluate against ground truth -> bank what clears the bar, over the top
        `limit` substantial sessions. Slow (real agent calls); cost-capped."""
        if not db.get_board(board_id):
            raise HTTPException(404, "board not found")
        avail = hub.available_agents()
        if not distill_available(avail):
            raise HTTPException(503, "no reasoning agent available to improve with")
        limit = max(1, min(8, int(body.limit or 2)))
        summary = await asyncio.to_thread(run_improvement_pass, db, board_id,
                                          hub.all_agent_sessions(), avail, EXEMPLAR_BAR,
                                          limit, body.sandbox)
        return {"results": summary, "exemplars": db.list_exemplars(board_id)}

    @app.delete("/api/exemplars/{exemplar_id}")
    async def delete_exemplar(exemplar_id: str):
        db.delete_exemplar(exemplar_id)
        return {"ok": True}

    @app.post("/api/workflows/distill")
    async def distill_workflow(body: WorkflowImport):
        """Use any available agent (claude/codex/glm/gemini/…) to turn a raw,
        session-derived draft into a clean reusable workflow with short,
        generalized, guided step prompts."""
        avail = hub.available_agents()
        if not distill_available(avail):
            raise HTTPException(503, "no reasoning agent available to distill with")
        t = body.template or {}
        if not isinstance(t.get("steps"), list) or not t["steps"]:
            raise HTTPException(400, "template needs steps to distill")
        out = await asyncio.to_thread(distill_workflows, t, avail)
        if not out:
            raise HTTPException(502, "distillation failed (agent returned nothing usable)")
        return {"workflows": out, "by": out[0].get("_distilled_by"),
                "template": out[0], "distilled": True}

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
