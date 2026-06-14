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

CREATE INDEX IF NOT EXISTS idx_cards_board ON cards(board_id);
CREATE INDEX IF NOT EXISTS idx_sessions_card ON sessions(card_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id);
"""

DEFAULT_COLUMNS = [
    ("Backlog", "backlog"),
    ("Running", "running"),
    ("Review", "review"),
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
                          ("profile", "TEXT DEFAULT ''")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE cards ADD COLUMN {name} {ddl}")
        rcols = {r["name"] for r in self.q("PRAGMA table_info(runners)")}
        if "auto_approve" not in rcols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN auto_approve INTEGER DEFAULT 1")
        # Drop deprecated columns from older boards, relocating any stray cards:
        #   info  -> backlog (sessions now live inline by recency)
        #   queued -> running (a card is queued via status, not a column)
        deprecated = {"info": "backlog", "queued": "running"}
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
                     profile: str = "") -> Dict[str, Any]:
        cid = gen_id()
        pos = self._next_position(column_id)
        ts = now()
        self.exec(
            """INSERT INTO cards (id, board_id, column_id, title, prompt, agent, cwd,
               status, position, resume_of, pin_runner, loop_max, loop_until, profile,
               created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, board_id, column_id, title, prompt, agent, cwd, "idle", pos,
             resume_of, pin_runner, max(1, int(loop_max or 1)), loop_until, profile, ts, ts),
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
