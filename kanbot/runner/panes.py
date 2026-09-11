"""Panes: the runner owns a real PTY per agent, Herdr-style.

A Pane is one process (an agent TUI, a headless `claude -p`, or a shell) attached
to a pseudo-terminal that *the runner* owns. Clients (the web board, `kanbot
attach`, the socket API) come and go; the process keeps running and its
scrollback stays in the pane's ring buffer, so anyone can reattach later and see
exactly where it left off.

State is the point. Each pane reports a semantic agent state:

    working   the agent is doing something
    blocked   the agent is waiting on a human (permission prompt, question)
    idle      the agent is ready and waiting for a prompt
    done      the process exited (exit_code is set)

Two sources feed it: **hooks** (Claude Code lifecycle hooks call back into the
runner — exact, no guessing) and **screen rules** (regexes over the last lines
of the terminal — for agents with no hook support). Hooks win whenever they
have reported at least once for a pane.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import re
import signal
import struct
import termios
import time
import uuid
from typing import Awaitable, Callable, Dict, List, Optional

try:
    import pyte                     # a real VT100 screen per pane: what a human sees
except ImportError:                 # pragma: no cover
    pyte = None

SCROLLBACK_BYTES = 512 * 1024   # per pane; replayed to anyone who attaches
STATES = ("working", "blocked", "idle", "done", "unknown")

# Words that mean "the terminal is waiting on a person". Each agent phrases its
# prompts differently, so this is one union across claude/codex/gemini/aider.
_BLOCKED = re.compile(
    r"\(y/n\)|\[y/N\]|\[Y/n\]|Do you want to|Allow .*\?|Yes, and don't ask|"
    r"Press Enter to (?:confirm|continue)|Approve\?|Grant access|Would you like to|"
    r"❯ 1\. Yes|Allow command|Continue\? |Proceed\?|Trust this|Accept edits|"
    r"needs your (?:input|approval)|permission", re.I)
_WORKING = re.compile(
    r"esc to interrupt|Thinking|Working|Running|Generating|Pondering|"
    r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒]|\.\.\.\s*$", re.I)
# The final line ends in a shell/agent prompt character: ready for a human.
_PROMPT_LINE = re.compile(r"(?:[❯>$%#›]|│\s*>)\s*$")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|[\x00-\x08\x0b-\x1f\x7f]")


def strip_ansi(data: str) -> str:
    return _ANSI.sub("", data).replace("\r", "")


def classify_screen(text: str, quiet_for: float) -> str:
    """Screen-rule state from the last lines of stripped terminal text.

    `quiet_for` is seconds since the last output byte: a terminal that is still
    printing is working unless it is visibly asking a question.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "working" if quiet_for < 8 else "idle"
    if quiet_for < 1.5:
        return "working"                      # output is still flowing
    last, last4 = lines[-1], "\n".join(lines[-4:])
    at_prompt = bool(_PROMPT_LINE.search(last)) and not _BLOCKED.search(last)
    if _BLOCKED.search(last4) and not at_prompt:
        return "blocked"                      # a question is the newest thing on screen
    if at_prompt:
        return "idle"
    if _WORKING.search("\n".join(lines[-6:])) and quiet_for < 30:
        return "working"
    return "idle" if quiet_for >= 8 else "working"


def _ansi_text(buf: bytes) -> str:
    return strip_ansi(buf.decode("utf-8", "replace"))


class Pane:
    def __init__(self, pane_id: str, argv: List[str], cwd: str, agent: str,
                 title: str = "", interactive: bool = True, session_id: str = "",
                 card_id: str = "", rows: int = 40, cols: int = 140):
        self.id = pane_id
        self.argv = argv
        self.cwd = cwd
        self.agent = agent
        self.title = title or " ".join(argv)[:80]
        self.interactive = interactive
        self.session_id = session_id
        self.card_id = card_id
        self.rows, self.cols = rows, cols
        self.fd: int = -1
        self.pid: int = 0
        self.buf = bytearray()
        self.state = "unknown"
        self.state_source = ""          # hook | screen | exit
        self.hook_seen = False
        self.started_at = time.time()
        self.last_output_at = self.started_at
        self.last_input_at = 0.0
        self.ended_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.seen_at = 0.0              # last time a human looked at it (done → examined)
        self.subscribers: List[Callable[[bytes], None]] = []
        self._line_buf = b""
        # ponytail: pyte keeps a rendered screen so TUIs (ink/ratatui apps that
        # paint with cursor moves) read back as text; without it we fall back to
        # stripping escapes from the raw tail, which loses spacing.
        self._screen = pyte.Screen(cols, rows) if pyte else None
        self._stream = pyte.ByteStream(self._screen) if pyte else None
        self._exit = asyncio.get_event_loop().create_future()
        self._state_waiters: List[asyncio.Future] = []

    # -- serialisation ------------------------------------------------------
    def info(self) -> dict:
        return {
            "id": self.id, "agent": self.agent, "title": self.title, "cwd": self.cwd,
            "argv": self.argv, "interactive": self.interactive,
            "session_id": self.session_id, "card_id": self.card_id,
            "state": self.state, "state_source": self.state_source,
            "pid": self.pid, "rows": self.rows, "cols": self.cols,
            "started_at": self.started_at, "last_output_at": self.last_output_at,
            "ended_at": self.ended_at, "exit_code": self.exit_code,
            "alive": self.exit_code is None and self.fd >= 0,
        }

    @property
    def alive(self) -> bool:
        return self.exit_code is None and self.fd >= 0

    def read_text(self, lines: int = 60) -> str:
        """The last `lines` of the screen as plain text — what a human would see."""
        if self._screen is not None:
            out = [ln.rstrip() for ln in self._screen.display]
        else:
            out = [ln.rstrip() for ln in _ansi_text(bytes(self.buf[-64 * 1024:])).splitlines()]
        while out and not out[-1]:
            out.pop()
        return "\n".join(out[-lines:])

    def feed(self, data: bytes) -> None:
        if self._stream is not None:
            try:
                self._stream.feed(data)
            except Exception:  # noqa: BLE001  — never let a bad escape kill the reader
                pass

    # -- io -----------------------------------------------------------------
    def write(self, data: bytes) -> None:
        if not self.alive:
            return
        self.last_input_at = time.time()
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = max(2, int(rows)), max(10, int(cols))
        self.rows, self.cols = rows, cols
        if self._screen is not None:
            self._screen.resize(rows, cols)
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    def kill(self, sig: int = signal.SIGTERM) -> None:
        """Hang up the terminal (what closing a tmux pane does), then TERM.
        Interactive shells ignore TERM but exit on HUP and pass it to their jobs."""
        if not (self.pid and self.exit_code is None):
            return
        sigs = (signal.SIGHUP, sig) if sig == signal.SIGTERM else (sig,)
        for s in sigs:
            for send in (lambda: os.killpg(self.pid, s), lambda: os.kill(self.pid, s)):
                try:
                    send()
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    async def terminate(self, grace: float = 5.0) -> None:
        """kill(), then SIGKILL anything still alive after `grace` seconds."""
        self.kill()
        if await self.wait(grace) is None:
            self.kill(signal.SIGKILL)

    async def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return await asyncio.wait_for(asyncio.shield(self._exit), timeout)
        except asyncio.TimeoutError:
            return None

    async def wait_state(self, until: str, timeout: Optional[float] = None) -> str:
        """Block until the state equals `until` ('any' = any change). Returns the state."""
        if until == "any" or self.state != until:
            if until == "done" and self.exit_code is not None:
                return self.state
            fut = asyncio.get_event_loop().create_future()
            self._state_waiters.append(fut)
            try:
                await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                self._state_waiters.remove(fut)
                return self.state
            if until != "any" and self.state != until:
                return await self.wait_state(until, timeout)  # woke on another transition
        return self.state

    # -- state ----------------------------------------------------------------
    def set_state(self, state: str, source: str) -> bool:
        if state not in STATES:
            return False
        if source == "hook":
            self.hook_seen = True
        elif source == "screen" and self.hook_seen:
            return False            # hooks are authoritative once they've spoken
        if self.exit_code is not None and state != "done":
            return False
        changed = state != self.state
        self.state, self.state_source = state, source
        if changed:
            for fut in self._state_waiters:
                if not fut.done():
                    fut.set_result(state)
            self._state_waiters.clear()
        return changed


OnLine = Callable[[Pane, str], Awaitable[None]]
OnState = Callable[[Pane, str], Awaitable[None]]


class PaneManager:
    """Owns every pane on this runner. One instance per runner process.

    ponytail: panes die with the runner (like tmux's server). Run the runner as
    a service (launchd/systemd, or `kanbot up` in a tmux) to keep them alive.
    """

    def __init__(self, on_line: Optional[OnLine] = None, on_state: Optional[OnState] = None,
                 sock_path: str = ""):
        self.panes: Dict[str, Pane] = {}
        self.on_line = on_line
        self.on_state = on_state
        self.sock_path = sock_path
        self._listeners: List[Callable[[dict], None]] = []
        self._classify_task: Optional[asyncio.Task] = None

    # -- lifecycle ----------------------------------------------------------
    def spawn(self, argv: List[str], cwd: str = "", env: Optional[dict] = None,
              agent: str = "shell", title: str = "", interactive: bool = True,
              session_id: str = "", card_id: str = "", rows: int = 40, cols: int = 140,
              pane_id: str = "") -> Pane:
        pane = Pane(pane_id or uuid.uuid4().hex[:8], argv, cwd or os.getcwd(), agent,
                    title, interactive, session_id, card_id, rows, cols)
        full_env = os.environ.copy()
        full_env.update(env or {})
        # A runner started from inside a Claude Code session must not make its
        # agents think they are nested child sessions.
        for k in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT"):
            full_env.pop(k, None)
        full_env.update({"KANBOT_PANE_ID": pane.id, "KANBOT_SOCK": self.sock_path,
                         "TERM": "xterm-256color", "COLORTERM": "truecolor"})
        pid, fd = os.forkpty()
        if pid == 0:  # child
            try:
                os.setsid()
            except OSError:
                pass
            try:
                if pane.cwd:
                    os.chdir(pane.cwd)
                os.execvpe(argv[0], argv, full_env)
            except Exception as e:  # noqa: BLE001
                os.write(2, f"kanbot: cannot start {argv[0]}: {e}\r\n".encode())
            os._exit(127)
        pane.pid, pane.fd = pid, fd
        pane.resize(rows, cols)
        os.set_blocking(fd, False)
        loop = asyncio.get_event_loop()
        loop.add_reader(fd, self._on_readable, pane)
        self.panes[pane.id] = pane
        pane.set_state("working", "screen")
        self._emit(pane)
        self._ensure_classifier()
        return pane

    def _on_readable(self, pane: Pane) -> None:
        try:
            data = os.read(pane.fd, 65536)
        except BlockingIOError:
            return
        except OSError:          # EIO: the slave side closed → process is gone
            data = b""
        if not data:
            asyncio.get_event_loop().remove_reader(pane.fd)
            asyncio.ensure_future(self._reap(pane))
            return
        pane.last_output_at = time.time()
        pane.buf += data
        pane.feed(data)
        if len(pane.buf) > SCROLLBACK_BYTES:
            del pane.buf[: len(pane.buf) - SCROLLBACK_BYTES]
        for cb in list(pane.subscribers):
            try:
                cb(data)
            except Exception:  # noqa: BLE001
                pane.subscribers.remove(cb)
        if self.on_line and not pane.interactive:
            pane._line_buf += data
            *lines, pane._line_buf = pane._line_buf.split(b"\n")
            for raw in lines:
                text = strip_ansi(raw.decode("utf-8", "replace")).rstrip()
                if text:
                    asyncio.ensure_future(self.on_line(pane, text))

    async def _reap(self, pane: Pane) -> None:
        rc = None
        for _ in range(100):
            try:
                pid, status = os.waitpid(pane.pid, os.WNOHANG)
            except ChildProcessError:
                pid, status = pane.pid, 0
            if pid:
                rc = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
                    else (os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status))
                break
            await asyncio.sleep(0.05)
        if pane._line_buf and self.on_line and not pane.interactive:
            text = strip_ansi(pane._line_buf.decode("utf-8", "replace")).rstrip()
            if text:
                await self.on_line(pane, text)
            pane._line_buf = b""
        try:
            os.close(pane.fd)
        except OSError:
            pass
        pane.fd = -1
        pane.exit_code = rc if rc is not None else -1
        pane.ended_at = time.time()
        pane.set_state("done", "exit")
        if not pane._exit.done():
            pane._exit.set_result(pane.exit_code)
        await self._state_changed(pane)

    def get(self, ref: str) -> Optional[Pane]:
        """Find a pane by id, id prefix, or session/card id; 'latest' = newest."""
        if not ref:
            return None
        if ref in self.panes:
            return self.panes[ref]
        if ref == "latest":
            live = [p for p in self.panes.values()]
            return max(live, key=lambda p: p.started_at) if live else None
        for p in self.panes.values():
            if p.id.startswith(ref) or p.session_id == ref or p.card_id == ref:
                return p
        return None

    def list(self) -> List[dict]:
        return [p.info() for p in sorted(self.panes.values(), key=lambda p: p.started_at)]

    def remove(self, pane_id: str) -> None:
        p = self.panes.pop(pane_id, None)
        if p and p.alive:
            p.kill(signal.SIGKILL)

    def prune(self, max_age: float = 2 * 3600, keep: int = 50) -> None:
        """Drop exited panes older than max_age (or beyond `keep`)."""
        done = sorted((p for p in self.panes.values() if not p.alive),
                      key=lambda p: p.ended_at or 0)
        now = time.time()
        for i, p in enumerate(done):
            if (now - (p.ended_at or now)) > max_age or (len(done) - i) > keep:
                self.panes.pop(p.id, None)

    # -- state --------------------------------------------------------------
    def listen(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def unlisten(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _emit(self, pane: Pane) -> None:
        info = pane.info()
        for cb in list(self._listeners):
            try:
                cb(info)
            except Exception:  # noqa: BLE001
                self._listeners.remove(cb)

    async def _state_changed(self, pane: Pane) -> None:
        self._emit(pane)
        if self.on_state:
            await self.on_state(pane, pane.state)

    async def report(self, pane_ref: str, state: str, source: str = "hook") -> bool:
        pane = self.get(pane_ref)
        if not pane:
            return False
        if pane.set_state(state, source):
            await self._state_changed(pane)
        return True

    def _ensure_classifier(self) -> None:
        if self._classify_task is None or self._classify_task.done():
            self._classify_task = asyncio.ensure_future(self._classify_loop())

    async def _classify_loop(self) -> None:
        """Screen rules, once a second, for panes no hook has claimed."""
        while any(p.alive for p in self.panes.values()):
            now = time.time()
            for p in list(self.panes.values()):
                if not p.alive or p.hook_seen:
                    continue
                st = classify_screen(p.read_text(30), now - p.last_output_at)
                if p.set_state(st, "screen"):
                    await self._state_changed(p)
            await asyncio.sleep(1.0)


# Keys understood by `send_keys` (tmux-style names) → bytes.
KEYS = {
    "Enter": b"\r", "Return": b"\r", "Tab": b"\t", "Escape": b"\x1b", "Esc": b"\x1b",
    "Space": b" ", "BSpace": b"\x7f", "Backspace": b"\x7f", "Up": b"\x1b[A", "Down": b"\x1b[B",
    "Right": b"\x1b[C", "Left": b"\x1b[D", "Home": b"\x1b[H", "End": b"\x1b[F",
    "PageUp": b"\x1b[5~", "PageDown": b"\x1b[6~",
}


def keys_to_bytes(tokens: List[str]) -> bytes:
    """['y', 'Enter'] → b'y\\r'; 'C-c' → ctrl-c; anything else is literal text."""
    out = b""
    for t in tokens:
        if t in KEYS:
            out += KEYS[t]
        elif len(t) == 3 and t[:2] == "C-":
            out += bytes([ord(t[2].upper()) & 0x1F])     # C-c → 0x03, C-] → 0x1d
        else:
            out += t.encode()
    return out
