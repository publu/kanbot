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
from dataclasses import dataclass
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


_CODEX_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


@dataclass
class Provider:
    """A place where some agent TUI stores its session transcripts.

    Built-ins cover Claude Code and Codex; users add more (Hermes, OpenCode,
    Gemini, or any home-grown agent) via `discovery_sources` in the config —
    no code change needed, as long as the agent logs newline-delimited JSON.
    """
    name: str            # agent id shown on cards, e.g. "claude"
    label: str           # display label, e.g. "Claude Code"
    root: Path           # base dir to scan
    pattern: str         # glob for session files
    recursive: bool      # walk subdirectories
    fmt: str             # "claude" (flat records) or "codex" (payload-nested)


def _sid_for(path: Path) -> str:
    m = _CODEX_UUID.search(path.name)
    return m.group(1) if m else path.stem


def _scan(path: Path, sid: str, agent: str, fmt: str) -> Optional[Dict[str, Any]]:
    """Parse one transcript: head for cwd/start/first-prompt, tail for recap."""
    line_fn = _codex_line_msg if fmt == "codex" else _claude_line_msg
    cwd = preview = started_at = None
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
                payload = d.get("payload", d) if fmt == "codex" else d
                if not cwd and isinstance(payload, dict) and payload.get("cwd"):
                    cwd = payload["cwd"]
                m = line_fn(d)
                if m and m[0] == "user":
                    n_user += 1
                    if preview is None:
                        preview = m[1]
    except OSError:
        return None
    msgs = _tail_msgs(path, line_fn)
    return _build(agent, sid, cwd, preview, msgs, n_user, started_at,
                  _name_from_cwd(cwd or "", f"{agent}·{sid[:6]}"))


def _discover_provider(p: Provider) -> List[Dict[str, Any]]:
    if not p.root.is_dir():
        return []
    try:
        it = p.root.rglob(p.pattern) if p.recursive else p.root.glob(p.pattern)
        files = [f for f in it if f.is_file()]
    except OSError:
        return []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out: List[Dict[str, Any]] = []
    nowt = time.time()
    for f in files[:MAX_SESSIONS_PER_AGENT]:
        mtime = f.stat().st_mtime
        if nowt - mtime > RECENT_WINDOW_S:
            break
        info = _scan(f, _sid_for(f), p.name, p.fmt)
        if not info:
            continue
        info["mtime"] = mtime
        info["active"] = (nowt - mtime) <= ACTIVE_WINDOW_S
        info["duration"] = max(0, mtime - (info.get("started_at") or mtime))
        # Path + format let the server deep-read the FULL transcript on demand
        # (discovery only keeps a short tail for the board).
        info["path"] = str(f)
        info["fmt"] = p.fmt
        out.append(info)
    return out


def all_user_turns(path: str, fmt: str = "claude", limit: int = 80) -> List[str]:
    """Every human turn in a transcript (not just the tail), de-noised and
    de-duped — the real material to distill a session into workflows."""
    if not path:
        return []
    line_fn = _codex_line_msg if fmt == "codex" else _claude_line_msg
    out: List[str] = []
    try:
        with Path(path).open("r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = line_fn(d)
                if m and m[0] == "user":
                    t = " ".join((m[1] or "").split())
                    if t and (not out or out[-1] != t):
                        out.append(t)
                        if len(out) >= limit:
                            break
    except OSError:
        return []
    return out


def builtin_providers() -> List[Provider]:
    return [
        Provider("claude", "Claude Code", _claude_home() / "projects",
                 "*/*.jsonl", False, "claude"),
        Provider("codex", "Codex", Path.home() / ".codex" / "sessions",
                 "rollout-*.jsonl", True, "codex"),
    ]


def _providers_from_config(custom_sources: Optional[List[dict]]) -> List[Provider]:
    providers: List[Provider] = []
    for src in (custom_sources or []):
        if not isinstance(src, dict) or "root" not in src:
            continue
        providers.append(Provider(
            name=src.get("name", "custom"),
            label=src.get("label", src.get("name", "custom")),
            root=Path(os.path.expanduser(src["root"])),
            pattern=src.get("pattern", "*.jsonl"),
            recursive=bool(src.get("recursive", True)),
            fmt=src.get("fmt", "claude"),
        ))
    return providers


def active_providers(custom_sources: Optional[List[dict]] = None) -> List[Dict[str, str]]:
    """Which trackers actually have a session store present (for `kanbot agents`)."""
    out = []
    for p in builtin_providers() + _providers_from_config(custom_sources):
        if p.root.is_dir():
            out.append({"name": p.name, "label": p.label, "root": str(p.root)})
    return out


def discover_all(available_agents: Optional[List[str]] = None,
                 custom_sources: Optional[List[dict]] = None) -> List[Dict[str, Any]]:
    """Discover sessions from every known store. `available_agents` is ignored
    for tracking — we surface whatever transcripts exist, regardless of which
    agents this runner can execute."""
    sessions: List[Dict[str, Any]] = []
    for p in builtin_providers() + _providers_from_config(custom_sources):
        try:
            sessions.extend(_discover_provider(p))
        except Exception:
            continue
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


# Back-compat thin wrappers.
def discover_claude() -> List[Dict[str, Any]]:
    return _discover_provider(builtin_providers()[0])


def discover_codex() -> List[Dict[str, Any]]:
    return _discover_provider(builtin_providers()[1])
