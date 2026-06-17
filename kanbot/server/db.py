"""SQLite data layer for Deckhand.

Plain sqlite3 (stdlib) with a thin helper layer — no ORM, so the package stays
lightweight and trivially installable. All rows are returned as plain dicts.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    repo_path   TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    archived    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS columns (
    id          TEXT PRIMARY KEY,
    board_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'custom',  -- backlog|queued|running|review|done|custom
    position    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,
    board_id    TEXT NOT NULL,
    column_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    prompt      TEXT DEFAULT '',
    agent       TEXT DEFAULT 'auto',
    cwd         TEXT DEFAULT '',
    status      TEXT DEFAULT 'idle',  -- idle|queued|running|review|done|failed|cancelled
    position    INTEGER NOT NULL DEFAULT 0,
    auto_advance INTEGER DEFAULT 1,
    resume_of   TEXT DEFAULT '',   -- external agent session id this card resumes
    pin_runner  TEXT DEFAULT '',   -- if set, only this runner may execute the card
    loop_max    INTEGER DEFAULT 1, -- Ralph loop: max fresh-context iterations (1 = run once)
    loop_until  TEXT DEFAULT '',   -- shell predicate; exit 0 in cwd => stop the loop early
    profile     TEXT DEFAULT '',   -- prompt mode prepended to the prompt (e.g. 'lean')
    command     TEXT DEFAULT '',   -- raw command override (argv template, {prompt}); empty = use agent default
    workflow_id TEXT DEFAULT '',   -- if set, this card is a run of that workflow
    step_index  INTEGER DEFAULT 0, -- workflow runs: 0-based index of the active step
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tags (
    id          TEXT PRIMARY KEY,
    board_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#6b7280',
    insight     TEXT DEFAULT '',  -- '' = plain label, else an insight-provider key
    config      TEXT DEFAULT '{}',
    FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS card_tags (
    card_id     TEXT NOT NULL,
    tag_id      TEXT NOT NULL,
    PRIMARY KEY (card_id, tag_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    card_id     TEXT NOT NULL,
    board_id    TEXT NOT NULL,
    runner_id   TEXT DEFAULT '',
    runner_name TEXT DEFAULT '',
    agent       TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',  -- pending|assigned|running|success|failed|cancelled
    prompt      TEXT DEFAULT '',
    cwd         TEXT DEFAULT '',
    exit_code   INTEGER,
    started_at  REAL,
    ended_at    REAL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    stream      TEXT NOT NULL,  -- stdout|stderr|system
    text        TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runners (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    host          TEXT DEFAULT '',
    capabilities  TEXT DEFAULT '[]',
    status        TEXT DEFAULT 'offline',  -- online|busy|offline
    active        INTEGER DEFAULT 0,
    max_concurrency INTEGER DEFAULT 2,
    auto_approve  INTEGER DEFAULT 1,  -- 0 = safe mode (no auto-approve flags)
    last_seen     REAL,
    created_at    REAL NOT NULL
);

-- A workflow is a saved, reusable chain of agent steps. Running one creates a
-- single card that walks its steps in order (a session per step), which is how
-- Deckhand drives long (1-5hr) autonomous runs from Claude/Codex.
CREATE TABLE IF NOT EXISTS workflows (
    id          TEXT PRIMARY KEY,
    board_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    cwd         TEXT DEFAULT '',        -- default working dir for runs
    agent       TEXT DEFAULT 'auto',    -- default agent for steps that don't override
    source_tokens INTEGER DEFAULT 0,    -- est. input tokens of the conversation this distilled
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id              TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    name            TEXT NOT NULL,
    prompt          TEXT DEFAULT '',
    agent           TEXT DEFAULT '',    -- '' = inherit the workflow's agent
    profile         TEXT DEFAULT '',
    command         TEXT DEFAULT '',
    loop_max        INTEGER DEFAULT 1,  -- Ralph loop for this step
    loop_until      TEXT DEFAULT '',
    carry_context   INTEGER DEFAULT 1,  -- inject prior step's final output into this prompt
    continue_on_fail INTEGER DEFAULT 0, -- advance to the next step even if this one fails
    FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

-- Self-improvement (Part 2): proven workflows become few-shot exemplars that
-- steer future distillation; evals log how each scored so the loop can learn.
CREATE TABLE IF NOT EXISTS workflow_exemplars (
    id          TEXT PRIMARY KEY,
    board_id    TEXT DEFAULT '',
    name        TEXT NOT NULL,
    template    TEXT NOT NULL,      -- JSON workflow template (id-free)
    score       REAL DEFAULT 0,     -- 0-100 eval score that earned its place
    source_tokens INTEGER DEFAULT 0,
    metrics     TEXT DEFAULT '{}',  -- JSON breakdown
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_evals (
    id          TEXT PRIMARY KEY,
    board_id    TEXT DEFAULT '',
    session_id  TEXT DEFAULT '',
    name        TEXT DEFAULT '',
    score       REAL DEFAULT 0,
    breakdown   TEXT DEFAULT '{}',  -- JSON: fidelity, reusability, prompting_reduction, verdict
    critique    TEXT DEFAULT '',
    critic      TEXT DEFAULT '',    -- which agent judged
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_board ON cards(board_id);
CREATE INDEX IF NOT EXISTS idx_sessions_card ON sessions(card_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id);
CREATE INDEX IF NOT EXISTS idx_workflows_board ON workflows(board_id);
CREATE INDEX IF NOT EXISTS idx_steps_workflow ON workflow_steps(workflow_id);
CREATE INDEX IF NOT EXISTS idx_exemplars_score ON workflow_exemplars(score);
"""

DEFAULT_COLUMNS = [
    ("Backlog", "backlog"),
    ("Running", "running"),
    ("Done", "done"),
]


def now() -> float:
    return time.time()


def gen_id() -> str:
    return uuid.uuid4().hex[:12]


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._reconcile_orphans()
        self.conn.commit()

    def _reconcile_orphans(self) -> None:
        """On startup no in-flight agent task survives, so any session still
        marked pending/assigned/running is a zombie. Fail it and return its card
        to the backlog so the board doesn't show a permanent 'running…'."""
        ts = now()
        self.conn.execute(
            "UPDATE sessions SET status='failed', ended_at=? "
            "WHERE status IN ('pending','assigned','running')", (ts,))
        stuck = self.q("SELECT id, board_id FROM cards WHERE status IN ('running','assigned')")
        backlog: Dict[str, Optional[str]] = {}
        for card in stuck:
            bid = card["board_id"]
            if bid not in backlog:
                col = self.one("SELECT id FROM columns WHERE board_id=? AND kind='backlog'", (bid,))
                backlog[bid] = col["id"] if col else None
            if backlog[bid]:
                self.conn.execute("UPDATE cards SET status='idle', column_id=?, updated_at=? WHERE id=?",
                                  (backlog[bid], ts, card["id"]))
            else:
                self.conn.execute("UPDATE cards SET status='idle', updated_at=? WHERE id=?", (ts, card["id"]))

    def _migrate(self) -> None:
        """Additive migrations for DBs created by an earlier version."""
        cols = {r["name"] for r in self.q("PRAGMA table_info(cards)")}
        for name, ddl in (("resume_of", "TEXT DEFAULT ''"),
                          ("pin_runner", "TEXT DEFAULT ''"),
                          ("loop_max", "INTEGER DEFAULT 1"),
                          ("loop_until", "TEXT DEFAULT ''"),
                          ("profile", "TEXT DEFAULT ''"),
                          ("command", "TEXT DEFAULT ''"),
                          ("workflow_id", "TEXT DEFAULT ''"),
                          ("step_index", "INTEGER DEFAULT 0")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE cards ADD COLUMN {name} {ddl}")
        rcols = {r["name"] for r in self.q("PRAGMA table_info(runners)")}
        if "auto_approve" not in rcols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN auto_approve INTEGER DEFAULT 1")
        wfcols = {r["name"] for r in self.q("PRAGMA table_info(workflows)")}
        if wfcols and "source_tokens" not in wfcols:
            self.conn.execute("ALTER TABLE workflows ADD COLUMN source_tokens INTEGER DEFAULT 0")
        # Drop deprecated columns from older boards, relocating any stray cards:
        #   info  -> backlog (sessions now live inline by recency)
        #   queued -> running (a card is queued via status, not a column)
        deprecated = {"info": "backlog", "queued": "running", "review": "done"}
        for board in self.q("SELECT id FROM boards"):
            bid = board["id"]
            changed = False
            for kind, into in deprecated.items():
                dead = self.q("SELECT id FROM columns WHERE board_id=? AND kind=?", (bid, kind))
                if not dead:
                    continue
                target = self.one("SELECT id FROM columns WHERE board_id=? AND kind=?", (bid, into))
                for col in dead:
                    if target:
                        self.conn.execute("UPDATE cards SET column_id=? WHERE column_id=?",
                                          (target["id"], col["id"]))
                    self.conn.execute("DELETE FROM columns WHERE id=?", (col["id"],))
                    changed = True
            if changed:
                for i, cc in enumerate(self.q(
                        "SELECT id FROM columns WHERE board_id=? ORDER BY position", (bid,))):
                    self.conn.execute("UPDATE columns SET position=? WHERE id=?", (i, cc["id"]))

    # -- low level ---------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
        cur = self.conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def exec(self, sql: str, args: tuple = ()) -> None:
        self.conn.execute(sql, args)
        self.conn.commit()

    # -- boards ------------------------------------------------------------
    def create_board(self, name: str, repo_path: str = "") -> Dict[str, Any]:
        bid = gen_id()
        self.exec(
            "INSERT INTO boards (id, name, repo_path, created_at) VALUES (?,?,?,?)",
            (bid, name, repo_path, now()),
        )
        for i, (cname, kind) in enumerate(DEFAULT_COLUMNS):
            self.exec(
                "INSERT INTO columns (id, board_id, name, kind, position) VALUES (?,?,?,?,?)",
                (gen_id(), bid, cname, kind, i),
            )
        return self.get_board(bid)

    def get_board(self, bid: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM boards WHERE id=?", (bid,))

    def list_boards(self) -> List[Dict[str, Any]]:
        return self.q("SELECT * FROM boards WHERE archived=0 ORDER BY created_at")

    def delete_board(self, bid: str) -> None:
        self.exec("DELETE FROM boards WHERE id=?", (bid,))

    def columns(self, bid: str) -> List[Dict[str, Any]]:
        return self.q("SELECT * FROM columns WHERE board_id=? ORDER BY position", (bid,))

    def get_column(self, cid: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM columns WHERE id=?", (cid,))

    def column_by_kind(self, bid: str, kind: str) -> Optional[Dict[str, Any]]:
        return self.one(
            "SELECT * FROM columns WHERE board_id=? AND kind=? ORDER BY position LIMIT 1",
            (bid, kind),
        )

    # -- cards -------------------------------------------------------------
    def create_card(self, board_id: str, column_id: str, title: str, prompt: str = "",
                     agent: str = "auto", cwd: str = "", resume_of: str = "",
                     pin_runner: str = "", loop_max: int = 1, loop_until: str = "",
                     profile: str = "", command: str = "", workflow_id: str = "",
                     step_index: int = 0) -> Dict[str, Any]:
        cid = gen_id()
        pos = self._next_position(column_id)
        ts = now()
        self.exec(
            """INSERT INTO cards (id, board_id, column_id, title, prompt, agent, cwd,
               status, position, resume_of, pin_runner, loop_max, loop_until, profile,
               command, workflow_id, step_index, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, board_id, column_id, title, prompt, agent, cwd, "idle", pos,
             resume_of, pin_runner, max(1, int(loop_max or 1)), loop_until, profile,
             command, workflow_id, step_index, ts, ts),
        )
        return self.get_card(cid)

    def _next_position(self, column_id: str) -> int:
        row = self.one("SELECT MAX(position) AS m FROM cards WHERE column_id=?", (column_id,))
        return (row["m"] + 1) if row and row["m"] is not None else 0

    def get_card(self, cid: str) -> Optional[Dict[str, Any]]:
        card = self.one("SELECT * FROM cards WHERE id=?", (cid,))
        if card:
            card["tags"] = self.card_tags(cid)
        return card

    def list_cards(self, board_id: str) -> List[Dict[str, Any]]:
        cards = self.q(
            "SELECT * FROM cards WHERE board_id=? ORDER BY position", (board_id,)
        )
        for c in cards:
            c["tags"] = self.card_tags(c["id"])
        return cards

    def update_card(self, cid: str, **fields) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get_card(cid)
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.exec(f"UPDATE cards SET {cols} WHERE id=?", (*fields.values(), cid))
        return self.get_card(cid)

    def move_card(self, cid: str, column_id: str, position: int) -> Optional[Dict[str, Any]]:
        self.exec(
            "UPDATE cards SET column_id=?, position=?, updated_at=? WHERE id=?",
            (column_id, position, now(), cid),
        )
        return self.get_card(cid)

    def delete_card(self, cid: str) -> None:
        self.exec("DELETE FROM cards WHERE id=?", (cid,))

    def cards_in_column_kind(self, board_id: str, kind: str) -> List[Dict[str, Any]]:
        col = self.column_by_kind(board_id, kind)
        if not col:
            return []
        return self.q(
            "SELECT * FROM cards WHERE column_id=? ORDER BY position", (col["id"],)
        )

    def cards_with_status(self, board_id: str, status: str) -> List[Dict[str, Any]]:
        return self.q(
            "SELECT * FROM cards WHERE board_id=? AND status=? ORDER BY position",
            (board_id, status),
        )

    # -- workflows ---------------------------------------------------------
    # A workflow is just (workflow row + ordered step rows). It is fully
    # self-describing, so the same dict shape doubles as an export/import
    # template — that's what makes extracting and sharing workflows trivial.
    STEP_FIELDS = ("name", "prompt", "agent", "profile", "command",
                   "loop_max", "loop_until", "carry_context", "continue_on_fail")

    @staticmethod
    def _normalize_step(raw: Dict[str, Any], position: int) -> Dict[str, Any]:
        return {
            "name": str(raw.get("name") or f"Step {position + 1}"),
            "prompt": str(raw.get("prompt") or ""),
            "agent": str(raw.get("agent") or ""),
            "profile": str(raw.get("profile") or ""),
            "command": str(raw.get("command") or ""),
            "loop_max": max(1, int(raw.get("loop_max") or 1)),
            "loop_until": str(raw.get("loop_until") or ""),
            "carry_context": 1 if raw.get("carry_context", True) else 0,
            "continue_on_fail": 1 if raw.get("continue_on_fail", False) else 0,
        }

    def save_workflow(self, board_id: str, name: str, description: str = "",
                      agent: str = "auto", cwd: str = "",
                      steps: Optional[List[dict]] = None,
                      workflow_id: Optional[str] = None,
                      source_tokens: int = 0) -> Dict[str, Any]:
        """Create or replace a workflow and its steps in one shot. Used by the
        builder (save), import (from a template), and extract (from a session)."""
        ts = now()
        wid = workflow_id or gen_id()
        st = max(0, int(source_tokens or 0))
        if self.get_workflow(wid):
            # keep an existing source_tokens if this update doesn't carry one
            self.exec(
                """UPDATE workflows SET name=?, description=?, agent=?, cwd=?,
                   source_tokens=COALESCE(NULLIF(?,0), source_tokens), updated_at=? WHERE id=?""",
                (name, description, agent or "auto", cwd, st, ts, wid),
            )
        else:
            self.exec(
                """INSERT INTO workflows (id, board_id, name, description, agent, cwd,
                   source_tokens, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (wid, board_id, name, description, agent or "auto", cwd, st, ts, ts),
            )
        self._replace_steps(wid, steps or [])
        return self.get_workflow(wid)

    def _replace_steps(self, workflow_id: str, steps: List[dict]) -> None:
        self.conn.execute("DELETE FROM workflow_steps WHERE workflow_id=?", (workflow_id,))
        for i, raw in enumerate(steps):
            s = self._normalize_step(raw, i)
            self.conn.execute(
                """INSERT INTO workflow_steps (id, workflow_id, position, name, prompt,
                   agent, profile, command, loop_max, loop_until, carry_context,
                   continue_on_fail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gen_id(), workflow_id, i, s["name"], s["prompt"], s["agent"],
                 s["profile"], s["command"], s["loop_max"], s["loop_until"],
                 s["carry_context"], s["continue_on_fail"]),
            )
        self.conn.commit()

    def workflow_steps(self, workflow_id: str) -> List[Dict[str, Any]]:
        return self.q(
            "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY position",
            (workflow_id,),
        )

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        wf = self.one("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if wf:
            wf["steps"] = self.workflow_steps(workflow_id)
        return wf

    def list_workflows(self, board_id: str) -> List[Dict[str, Any]]:
        wfs = self.q("SELECT * FROM workflows WHERE board_id=? ORDER BY name", (board_id,))
        for wf in wfs:
            wf["steps"] = self.workflow_steps(wf["id"])
        return wfs

    def delete_workflow(self, workflow_id: str) -> None:
        self.exec("DELETE FROM workflows WHERE id=?", (workflow_id,))

    def workflow_template(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """A portable, id-free dict of a workflow — copy/paste or share to clone
        the workflow anywhere."""
        wf = self.get_workflow(workflow_id)
        if not wf:
            return None
        return {
            "name": wf["name"], "description": wf["description"],
            "agent": wf["agent"], "cwd": wf.get("cwd", ""),
            "steps": [{k: s[k] for k in self.STEP_FIELDS} for s in wf["steps"]],
        }

    def clone_workflow(self, workflow_id: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        wf = self.get_workflow(workflow_id)
        if not wf:
            return None
        tpl = self.workflow_template(workflow_id)
        return self.save_workflow(wf["board_id"], name or f"{wf['name']} copy",
                                  tpl["description"], tpl["agent"], tpl["cwd"], tpl["steps"])

    # -- self-improvement: exemplars + evals -------------------------------
    def add_exemplar(self, board_id: str, name: str, template: dict, score: float,
                     source_tokens: int = 0, metrics: Optional[dict] = None) -> Dict[str, Any]:
        eid = gen_id()
        self.exec(
            """INSERT INTO workflow_exemplars (id, board_id, name, template, score,
               source_tokens, metrics, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (eid, board_id, name, json.dumps(template), float(score),
             int(source_tokens or 0), json.dumps(metrics or {}), now()),
        )
        return self.get_exemplar(eid)

    def get_exemplar(self, eid: str) -> Optional[Dict[str, Any]]:
        r = self.one("SELECT * FROM workflow_exemplars WHERE id=?", (eid,))
        if r:
            r["template"] = json.loads(r.get("template") or "{}")
            r["metrics"] = json.loads(r.get("metrics") or "{}")
        return r

    def list_exemplars(self, board_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = (self.q("SELECT * FROM workflow_exemplars WHERE board_id=? ORDER BY score DESC", (board_id,))
                if board_id else self.q("SELECT * FROM workflow_exemplars ORDER BY score DESC"))
        for r in rows:
            r["template"] = json.loads(r.get("template") or "{}")
            r["metrics"] = json.loads(r.get("metrics") or "{}")
        return rows

    def top_exemplars(self, k: int = 3, board_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.list_exemplars(board_id)[:max(0, k)]

    def delete_exemplar(self, eid: str) -> None:
        self.exec("DELETE FROM workflow_exemplars WHERE id=?", (eid,))

    def log_eval(self, board_id: str, session_id: str, name: str, score: float,
                 breakdown: dict, critique: str = "", critic: str = "") -> Dict[str, Any]:
        eid = gen_id()
        self.exec(
            """INSERT INTO workflow_evals (id, board_id, session_id, name, score,
               breakdown, critique, critic, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (eid, board_id, session_id, name, float(score), json.dumps(breakdown or {}),
             critique, critic, now()),
        )
        return {"id": eid}

    def list_evals(self, board_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        rows = (self.q("SELECT * FROM workflow_evals WHERE board_id=? ORDER BY created_at DESC LIMIT ?", (board_id, limit))
                if board_id else self.q("SELECT * FROM workflow_evals ORDER BY created_at DESC LIMIT ?", (limit,)))
        for r in rows:
            r["breakdown"] = json.loads(r.get("breakdown") or "{}")
        return rows

    # -- tags --------------------------------------------------------------
    def create_tag(self, board_id: str, name: str, color: str = "#6b7280",
                    insight: str = "", config: Optional[dict] = None) -> Dict[str, Any]:
        tid = gen_id()
        self.exec(
            "INSERT INTO tags (id, board_id, name, color, insight, config) VALUES (?,?,?,?,?,?)",
            (tid, board_id, name, color, insight, json.dumps(config or {})),
        )
        return self.get_tag(tid)

    def get_tag(self, tid: str) -> Optional[Dict[str, Any]]:
        t = self.one("SELECT * FROM tags WHERE id=?", (tid,))
        if t:
            t["config"] = json.loads(t.get("config") or "{}")
        return t

    def list_tags(self, board_id: str) -> List[Dict[str, Any]]:
        tags = self.q("SELECT * FROM tags WHERE board_id=? ORDER BY name", (board_id,))
        for t in tags:
            t["config"] = json.loads(t.get("config") or "{}")
        return tags

    def delete_tag(self, tid: str) -> None:
        self.exec("DELETE FROM card_tags WHERE tag_id=?", (tid,))
        self.exec("DELETE FROM tags WHERE id=?", (tid,))

    def card_tags(self, card_id: str) -> List[Dict[str, Any]]:
        tags = self.q(
            """SELECT t.* FROM tags t
               JOIN card_tags ct ON ct.tag_id = t.id
               WHERE ct.card_id=? ORDER BY t.name""",
            (card_id,),
        )
        for t in tags:
            t["config"] = json.loads(t.get("config") or "{}")
        return tags

    def add_card_tag(self, card_id: str, tag_id: str) -> None:
        self.exec(
            "INSERT OR IGNORE INTO card_tags (card_id, tag_id) VALUES (?,?)",
            (card_id, tag_id),
        )

    def remove_card_tag(self, card_id: str, tag_id: str) -> None:
        self.exec(
            "DELETE FROM card_tags WHERE card_id=? AND tag_id=?", (card_id, tag_id)
        )

    # -- sessions ----------------------------------------------------------
    def create_session(self, card: Dict[str, Any], agent: str) -> Dict[str, Any]:
        sid = gen_id()
        self.exec(
            """INSERT INTO sessions (id, card_id, board_id, agent, status, prompt, cwd, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sid, card["id"], card["board_id"], agent, "pending",
             card.get("prompt", ""), card.get("cwd", ""), now()),
        )
        return self.get_session(sid)

    def get_session(self, sid: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM sessions WHERE id=?", (sid,))

    def list_sessions(self, board_id: Optional[str] = None, card_id: Optional[str] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        if card_id:
            return self.q(
                "SELECT * FROM sessions WHERE card_id=? ORDER BY created_at DESC LIMIT ?",
                (card_id, limit),
            )
        if board_id:
            return self.q(
                "SELECT * FROM sessions WHERE board_id=? ORDER BY created_at DESC LIMIT ?",
                (board_id, limit),
            )
        return self.q("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,))

    def update_session(self, sid: str, **fields) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get_session(sid)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.exec(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))
        return self.get_session(sid)

    def add_event(self, session_id: str, stream: str, text: str) -> Dict[str, Any]:
        ts = now()
        cur = self.conn.execute(
            "INSERT INTO session_events (session_id, ts, stream, text) VALUES (?,?,?,?)",
            (session_id, ts, stream, text),
        )
        self.conn.commit()
        return {"id": cur.lastrowid, "session_id": session_id, "ts": ts,
                "stream": stream, "text": text}

    def events(self, session_id: str, after_id: int = 0, limit: int = 5000) -> List[Dict[str, Any]]:
        return self.q(
            "SELECT * FROM session_events WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
            (session_id, after_id, limit),
        )

    # -- runners -----------------------------------------------------------
    def upsert_runner(self, runner_id: str, name: str, host: str,
                      capabilities: List[str], max_concurrency: int = 2,
                      auto_approve: bool = True) -> Dict[str, Any]:
        aa = 1 if auto_approve else 0
        existing = self.one("SELECT * FROM runners WHERE id=?", (runner_id,))
        if existing:
            self.exec(
                """UPDATE runners SET name=?, host=?, capabilities=?, status='online',
                   max_concurrency=?, auto_approve=?, last_seen=? WHERE id=?""",
                (name, host, json.dumps(capabilities), max_concurrency, aa, now(), runner_id),
            )
        else:
            self.exec(
                """INSERT INTO runners (id, name, host, capabilities, status, max_concurrency,
                   auto_approve, last_seen, created_at) VALUES (?,?,?,?, 'online', ?,?,?,?)""",
                (runner_id, name, host, json.dumps(capabilities), max_concurrency, aa, now(), now()),
            )
        return self.get_runner(runner_id)

    def get_runner(self, runner_id: str) -> Optional[Dict[str, Any]]:
        r = self.one("SELECT * FROM runners WHERE id=?", (runner_id,))
        if r:
            r["capabilities"] = json.loads(r.get("capabilities") or "[]")
        return r

    def list_runners(self) -> List[Dict[str, Any]]:
        rows = self.q("SELECT * FROM runners ORDER BY name")
        for r in rows:
            r["capabilities"] = json.loads(r.get("capabilities") or "[]")
        return rows

    def set_runner_status(self, runner_id: str, status: str, active: Optional[int] = None) -> None:
        if active is None:
            self.exec(
                "UPDATE runners SET status=?, last_seen=? WHERE id=?",
                (status, now(), runner_id),
            )
        else:
            self.exec(
                "UPDATE runners SET status=?, active=?, last_seen=? WHERE id=?",
                (status, active, now(), runner_id),
            )
