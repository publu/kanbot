"""The runner's local socket API — the same surface agents and the CLI drive.

Unix socket at ~/.kanbot/runner.sock. Protocol: newline-delimited JSON. Each
request is `{"method": "...", "id": n, ...params}`; each reply is
`{"id": n, "ok": true, ...result}` or `{"id": n, "ok": false, "error": "..."}`.

Two methods switch the connection into streaming mode:
  events.subscribe  → one `{"event": "pane", "pane": {...}}` line per state change
  pane.attach       → server→client bytes are RAW terminal output (scrollback
                      first, then live); client→server stays JSON lines:
                      {"input": "<base64>"} and {"resize": [rows, cols]}.

Agent control methods:
  ping · agent.list · agent.get · agent.read · agent.start · agent.prompt ·
  agent.send_keys · agent.wait · agent.kill · agent.remove · pane.report_state
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
from typing import Any, Awaitable, Callable, Dict, Optional

from ..config import config_dir
from .panes import PaneManager, keys_to_bytes

Starter = Callable[[dict], Awaitable[dict]]   # runner-provided: start an agent pane from params


def sock_path() -> str:
    if os.environ.get("KANBOT_SOCK"):
        return os.environ["KANBOT_SOCK"]
    path = str(config_dir() / "runner.sock")
    if len(path) > 100:   # AF_UNIX paths are capped (~104 bytes on macOS)
        import hashlib
        path = f"/tmp/kanbot-{hashlib.sha1(path.encode()).hexdigest()[:10]}.sock"
    return path


class ApiServer:
    def __init__(self, panes: PaneManager, starter: Optional[Starter] = None, path: str = "", extension=None):
        self.panes = panes
        self.starter = starter
        self.path = path or sock_path()
        self._server: Optional[asyncio.AbstractServer] = None
        self.extension = extension
        self._lock = None

    async def start(self) -> None:
        # Never unlink a live runner's socket. Hold the lock for the complete
        # server lifetime, including configuration and swarm ownership.
        import fcntl
        self._lock = open(self.path + ".lock", "a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise RuntimeError("A Kanbot runner already owns this socket")
        try:
            _, writer = await asyncio.open_unix_connection(self.path)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        else:
            writer.close()
            await writer.wait_closed()
            self._lock.close()
            self._lock = None
            raise RuntimeError("A Kanbot runner already owns this socket")
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        # asyncio's default line limit is 64 KiB; a 60000-character swarm request
        # with escaped non-ASCII text is larger and would drop the connection.
        self._server = await asyncio.start_unix_server(self._client, path=self.path, limit=1 << 20)
        os.chmod(self.path, 0o600)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            try:
                os.unlink(self.path)
            except OSError:
                pass
        if self._lock:
            self._lock.close()
            self._lock = None

    # -- one client -----------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def reply(obj: dict) -> None:
            writer.write((json.dumps(obj) + "\n").encode())
            await writer.drain()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    await reply({"ok": False, "error": "bad json"})
                    continue
                rid = req.get("id")
                method = req.get("method", "")
                try:
                    if method == "events.subscribe":
                        await reply({"id": rid, "ok": True})
                        await self._stream_events(writer)
                        break
                    if method == "pane.attach":
                        await self._attach(req, reader, writer)
                        break
                    res = await self.handle(method, req)
                    await reply({"id": rid, "ok": True, **res})
                except Exception as e:  # noqa: BLE001
                    await reply({"id": rid, "ok": False, "error": str(e)})
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def handle(self, method: str, p: dict) -> dict:
        pm = self.panes
        if method.startswith("swarm.") and self.extension:
            return await self.extension(method, p)
        if method == "ping":
            return {"pong": True, "panes": len(pm.panes), "pid": os.getpid()}
        if method == "agent.list":
            return {"agents": pm.list()}
        if method == "pane.report_state":
            ok = await pm.report(p.get("pane_id", ""), p.get("state", ""), p.get("source", "hook"))
            return {"reported": ok}
        if method == "agent.start":
            if not self.starter:
                raise RuntimeError("this runner cannot start agents")
            return await self.starter(p)
        # everything below needs a pane
        pane = pm.get(p.get("agent") or p.get("pane_id") or p.get("id") or "")
        if not pane:
            raise LookupError("no such pane")
        if method == "agent.get":
            return {"agent": pane.info()}
        if method == "agent.read":
            return {"text": pane.read_text(int(p.get("lines", 60))), "state": pane.state}
        if method == "agent.prompt":
            text = p.get("text", "")
            pane.write(text.encode() + (b"\r" if p.get("enter", True) else b""))
            return {"sent": len(text)}
        if method == "agent.send_keys":
            keys = p.get("keys") or []
            if isinstance(keys, str):
                keys = shlex.split(keys)
            pane.write(keys_to_bytes(keys))
            return {"sent": keys}
        if method == "agent.wait":
            until = p.get("until", "idle")
            timeout = p.get("timeout")
            if until in ("done", "exit"):
                await pane.wait(timeout)
            else:
                await pane.wait_state(until, timeout)
            return {"state": pane.state, "exit_code": pane.exit_code}
        if method == "agent.kill":
            asyncio.ensure_future(pane.terminate())
            return {"killed": pane.id}
        if method == "agent.remove":
            pm.remove(pane.id)
            return {"removed": pane.id}
        if method == "agent.resize":
            pane.resize(int(p.get("rows", 40)), int(p.get("cols", 140)))
            return {"rows": pane.rows, "cols": pane.cols}
        raise ValueError(f"unknown method {method!r}")

    async def _stream_events(self, writer: asyncio.StreamWriter) -> None:
        q: asyncio.Queue = asyncio.Queue()
        self.panes.listen(q.put_nowait)
        try:
            while True:
                info = await q.get()
                writer.write((json.dumps({"event": "pane", "pane": info}) + "\n").encode())
                await writer.drain()
        finally:
            self.panes.unlisten(q.put_nowait)

    async def _attach(self, req: dict, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        pane = self.panes.get(req.get("agent") or req.get("pane_id") or "latest")
        if not pane:
            writer.write((json.dumps({"id": req.get("id"), "ok": False, "error": "no such pane"}) + "\n").encode())
            await writer.drain()
            return
        writer.write((json.dumps({"id": req.get("id"), "ok": True, "agent": pane.info()}) + "\n").encode())
        if req.get("rows") and req.get("cols"):
            pane.resize(int(req["rows"]), int(req["cols"]))
        writer.write(bytes(pane.buf))       # scrollback replay
        await writer.drain()
        import time
        pane.seen_at = time.time()

        def on_data(data: bytes) -> None:
            writer.write(data)

        pane.subscribers.append(on_data)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "input" in msg:
                    pane.write(base64.b64decode(msg["input"]))
                elif "resize" in msg:
                    pane.resize(*msg["resize"])
                elif msg.get("detach"):
                    break
        finally:
            if on_data in pane.subscribers:
                pane.subscribers.remove(on_data)


# -- client side -----------------------------------------------------------------
def call(method: str, path: str = "", **params: Any) -> dict:
    """Blocking one-shot request from the CLI / hooks. Raises on error."""
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(params.pop("_timeout", None))
    s.connect(path or sock_path())
    s.sendall((json.dumps({"method": method, "id": 1, **params}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    res = json.loads(buf or b"{}")
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "request failed"))
    return res


def attach(ref: str = "latest", path: str = "") -> int:
    """Interactive attach: your terminal becomes the pane. Ctrl-] detaches."""
    import select
    import signal
    import socket
    import struct
    import sys
    import termios
    import tty
    import fcntl

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path or sock_path())
    fd = sys.stdin.fileno()

    def size():
        try:
            r, c, _, _ = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
            return r, c
        except OSError:
            return 40, 140

    rows, cols = size()
    s.sendall((json.dumps({"method": "pane.attach", "id": 1, "agent": ref,
                           "rows": rows, "cols": cols}) + "\n").encode())
    head = b""
    while not head.endswith(b"\n"):
        chunk = s.recv(1)
        if not chunk:
            print("runner closed the connection", file=sys.stderr)
            return 1
        head += chunk
    res = json.loads(head)
    if not res.get("ok"):
        print(res.get("error", "attach failed"), file=sys.stderr)
        return 1
    info = res["agent"]
    sys.stderr.write(f"[kanbot] attached to {info['id']} ({info['agent']}, {info['state']}). Ctrl-] detaches.\r\n")

    def on_winch(*_):
        r, c = size()
        try:
            s.sendall((json.dumps({"resize": [r, c]}) + "\n").encode())
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, on_winch)
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        while True:
            r, _, _ = select.select([fd, s], [], [])
            if s in r:
                data = s.recv(65536)
                if not data:
                    break
                os.write(1, data)
            if fd in r:
                data = os.read(fd, 4096)
                if not data or b"\x1d" in data:   # Ctrl-]
                    break
                s.sendall((json.dumps({"input": base64.b64encode(data).decode()}) + "\n").encode())
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        s.close()
        sys.stderr.write("\r\n[kanbot] detached — the agent keeps running.\r\n")
    return 0
