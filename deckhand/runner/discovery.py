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


def _name_from_cwd(cwd: str, fallback: str) -> str:
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
    return fallback


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
    n_user = 0
    try:
        with path.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > MAX_LINES_SCAN and preview and cwd:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
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
    return {
        "agent": "claude",
        "session_id": path.stem,
        "cwd": cwd or "",
        "name": _name_from_cwd(cwd or "", f"claude·{path.stem[:6]}"),
        "title": _truncate(preview),
        "turns": n_user,
    }


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
    n_user = 0
    try:
        with path.open("r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > MAX_LINES_SCAN and preview and cwd:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = d.get("payload", d)
                if not isinstance(payload, dict):
                    continue
                if not cwd and payload.get("cwd"):
                    cwd = payload["cwd"]
                # user turns appear as response_item/event_msg with role user
                role = payload.get("role")
                if role == "user" or payload.get("type") == "user_message":
                    n_user += 1
                    if preview is None:
                        txt = _extract_text(payload.get("content") or payload.get("message"))
                        if not _is_noise(txt):
                            preview = txt
    except OSError:
        return None
    return {
        "agent": "codex",
        "session_id": sid,
        "cwd": cwd or "",
        "name": _name_from_cwd(cwd or "", f"codex·{sid[:6]}"),
        "title": _truncate(preview),
        "turns": n_user,
    }


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
