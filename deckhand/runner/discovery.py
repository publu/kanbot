"""Discover the agents' own sessions on this machine.

Claude Code and Codex each keep a local transcript store. The runner lives on
the same machine, so it can enumerate those sessions, tell which are *actively
being written* (i.e. an agent is working right now), and offer them up to the
board for monitoring or reviving (`claude --resume` / `codex exec resume`).

We parse defensively and cheaply: stat for recency, read only the head/first
user turn for a title, and cap how many files we touch.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# A session whose transcript was written within this window is treated as "working".
ACTIVE_WINDOW_S = 45
# Surface sessions touched within this window (older ones are listed too).
RECENT_WINDOW_S = 60 * 24 * 3600
MAX_SESSIONS_PER_AGENT = 60
MAX_LINES_SCAN = 250


def _truncate(s: Optional[str], n: int = 120) -> str:
    if not s:
        return ""
    s = " ".join(s.split())
    return s[: n - 1] + "…" if len(s) > n else s


# First-user-message text that is actually system noise, not a real prompt.
_NOISE = (
    "a session-scoped stop hook",
    "caveat:",
    "<command",
    "<system-reminder",
    "<local-command",
    "[request interrupted",
    "this session is being continued",
    "please continue",
    "⏺",
)


def _is_noise(text: Optional[str]) -> bool:
    if not text:
        return True
    t = text.strip().lower()
    if not t or t.startswith("<"):
        return True
    if any(t.startswith(p) for p in _NOISE):
        return True
    if "session-scoped stop hook" in t or "system-reminder" in t:
        return True
    return False


def _read_tail(path: Path, nbytes: int = 80000) -> List[str]:
    """Return the last lines of a file cheaply (for 'where did it leave off')."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
                fh.readline()  # discard partial line
            data = fh.read()
        return data.decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _name_from_cwd(cwd: str, fallback: str) -> str:
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
    return fallback


def _parse_ts(s: Any) -> Optional[float]:
    """Parse an ISO8601 timestamp to epoch seconds, tolerantly."""
    if not isinstance(s, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _claude_line_msg(d: dict):
    t = d.get("type")
    if t == "user" and not d.get("isMeta"):
        txt = _extract_text((d.get("message") or {}).get("content"))
        if not _is_noise(txt):
            return ("user", txt)
    elif t == "assistant":
        txt = _extract_text((d.get("message") or {}).get("content"))
        if txt and txt.strip():
            return ("assistant", txt)
    return None


def _codex_line_msg(d: dict):
    payload = d.get("payload", d)
    if not isinstance(payload, dict):
        return None
    role = payload.get("role")
    content = payload.get("content") or payload.get("message")
    if role == "user" or payload.get("type") == "user_message":
        txt = _extract_text(content)
        if not _is_noise(txt):
            return ("user", txt)
    if role == "assistant" or payload.get("type") in ("agent_message", "assistant_message"):
        txt = _extract_text(content)
        if txt and txt.strip():
            return ("assistant", txt)
    return None


def _tail_msgs(path: Path, line_fn, want: int = 5) -> List:
    """Collect recent text-bearing messages, scanning further back if the
    immediate tail is all tool calls (so the recap is never just the 1st msg)."""
    size = path.stat().st_size
    msgs: List = []
    for window in (150_000, 800_000):
        lines = _read_tail(path, window)
        msgs = []
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = line_fn(d)
            if m:
                msgs.append(m)
        if len(msgs) >= want or window >= size:
            break
    return msgs


def _build(agent: str, sid: str, cwd: Optional[str], preview: Optional[str],
           msgs: List, n_user: int, started_at: Optional[float], name: str) -> Dict[str, Any]:
    """Assemble a session record from a parsed head preview + message tail."""
    tail = [{"role": r, "text": _truncate(t, 1400)} for (r, t) in msgs[-12:]]
    last_user = next((m["text"] for m in reversed(tail) if m["role"] == "user"), None)
    last_text = next((m["text"] for m in reversed(tail) if m["role"] == "assistant"), None)
    # recap = the chronologically last message (whichever role), so it tracks
    # the session's current state — not always the last assistant turn.
    recap = tail[-1]["text"] if tail else _truncate(preview)
    recap_role = tail[-1]["role"] if tail else "user"
    return {
        "agent": agent,
        "session_id": sid,
        "cwd": cwd or "",
        "name": name,
        "title": _truncate(preview),          # first prompt (for reference)
        "recap": recap,
        "recap_role": recap_role,
        "last_user": last_user,
        "last_text": last_text,
        "tail": tail,                           # recent conversation for the modal
        "turns": n_user,
        "started_at": started_at,
    }


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def _extract_text(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                return part
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text")
    return None


def _scan_claude_file(path: Path) -> Optional[Dict[str, Any]]:
    cwd = None
    preview = None
    started_at = None
    n_user = 0
    try:
        with path.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > MAX_LINES_SCAN and preview and cwd and started_at:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if started_at is None and d.get("timestamp"):
                    started_at = _parse_ts(d["timestamp"])
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("type") == "user" and not d.get("isMeta"):
                    n_user += 1
                    if preview is None:
                        txt = _extract_text((d.get("message") or {}).get("content"))
                        if not _is_noise(txt):
                            preview = txt
    except OSError:
        return None
    msgs = _tail_msgs(path, _claude_line_msg)
    return _build("claude", path.stem, cwd, preview, msgs, n_user, started_at,
                  _name_from_cwd(cwd or "", f"claude·{path.stem[:6]}"))


def discover_claude() -> List[Dict[str, Any]]:
    root = _claude_home() / "projects"
    if not root.is_dir():
        return []
    files: List[os.DirEntry] = []
    try:
        for proj in os.scandir(root):
            if not proj.is_dir():
                continue
            for entry in os.scandir(proj.path):
                if entry.name.endswith(".jsonl"):
                    files.append(entry)
    except OSError:
        return []
    files.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    out: List[Dict[str, Any]] = []
    nowt = time.time()
    for entry in files[:MAX_SESSIONS_PER_AGENT]:
        mtime = entry.stat().st_mtime
        if nowt - mtime > RECENT_WINDOW_S:
            break
        info = _scan_claude_file(Path(entry.path))
        if not info:
            continue
        info["mtime"] = mtime
        info["active"] = (nowt - mtime) <= ACTIVE_WINDOW_S
        info["duration"] = max(0, mtime - (info.get("started_at") or mtime))
        out.append(info)
    return out


_CODEX_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _scan_codex_file(path: Path) -> Optional[Dict[str, Any]]:
    m = _CODEX_UUID.search(path.name)
    if not m:
        return None
    sid = m.group(1)
    cwd = None
    preview = None
    started_at = None
    n_user = 0
    try:
        with path.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > MAX_LINES_SCAN and preview and cwd and started_at:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if started_at is None and d.get("timestamp"):
                    started_at = _parse_ts(d["timestamp"])
                payload = d.get("payload", d)
                if not isinstance(payload, dict):
                    continue
                if not cwd and payload.get("cwd"):
                    cwd = payload["cwd"]
                role = payload.get("role")
                if role == "user" or payload.get("type") == "user_message":
                    n_user += 1
                    if preview is None:
                        txt = _extract_text(payload.get("content") or payload.get("message"))
                        if not _is_noise(txt):
                            preview = txt
    except OSError:
        return None
    msgs = _tail_msgs(path, _codex_line_msg)
    return _build("codex", sid, cwd, preview, msgs, n_user, started_at,
                  _name_from_cwd(cwd or "", f"codex·{sid[:6]}"))


def discover_codex() -> List[Dict[str, Any]]:
    root = Path.home() / ".codex" / "sessions"
    if not root.is_dir():
        return []
    files: List[Path] = []
    for p in root.rglob("rollout-*.jsonl"):
        files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[Dict[str, Any]] = []
    nowt = time.time()
    for p in files[:MAX_SESSIONS_PER_AGENT]:
        mtime = p.stat().st_mtime
        if nowt - mtime > RECENT_WINDOW_S:
            break
        info = _scan_codex_file(p)
        if not info:
            continue
        info["mtime"] = mtime
        info["active"] = (nowt - mtime) <= ACTIVE_WINDOW_S
        info["duration"] = max(0, mtime - (info.get("started_at") or mtime))
        out.append(info)
    return out


def discover_all(available_agents: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sessions: List[Dict[str, Any]] = []
    avail = set(available_agents or ["claude", "codex"])
    if "claude" in avail or "glm" in avail:
        sessions.extend(discover_claude())
    if "codex" in avail:
        sessions.extend(discover_codex())
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions
