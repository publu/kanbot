"""`kanbot` — the terminal app. A tmux for agents.

Left: every agent on this machine with its live state. Right: the selected
agent's terminal, rendered from the runner's pane (so it keeps running when you
quit). Keys go straight to the agent; Ctrl-] flips to the sidebar.

  sidebar mode:  j/k or ↑/↓ move · Enter/Tab focus the agent · n new agent
                 x kill · d drop a finished pane · r refresh · ? help · q quit
  agent mode:    everything is typed into the agent · Ctrl-] back to sidebar

Rendering: the runner streams raw bytes; a local pyte screen turns them into
cells, and curses paints them with the nearest of 256 colours. stdlib + pyte,
no TUI framework.
"""
from __future__ import annotations

import base64
import curses
import json
import os
import queue
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

import pyte

from .runner.api import call, sock_path

SIDEBAR_W = 34
STATE_ORDER = {"blocked": 0, "working": 1, "idle": 2, "unknown": 3, "done": 4}
MARK = {"blocked": "◆", "working": "●", "idle": "○", "done": "✓", "unknown": "?"}

# pyte colour name → curses colour index
_NAMED = {"black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3, "blue": 4, "magenta": 5,
          "cyan": 6, "white": 7, "brightblack": 8, "brightred": 9, "brightgreen": 10,
          "brightbrown": 11, "brightyellow": 11, "brightblue": 12, "brightmagenta": 13,
          "brightcyan": 14, "brightwhite": 15}
_CUBE = [0, 95, 135, 175, 215, 255]


def _hex_to_256(h: str) -> int:
    """Nearest xterm-256 index for 'rrggbb'."""
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return -1
    if abs(r - g) < 10 and abs(g - b) < 10:            # grey ramp
        if r < 4: return 16
        if r > 246: return 231
        return 232 + min(23, max(0, round((r - 8) / 10)))
    q = lambda v: min(range(6), key=lambda i: abs(_CUBE[i] - v))
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


def _color(c: str, ncolors: int) -> int:
    if c == "default":
        return -1
    if c in _NAMED:
        v = _NAMED[c]
        return v if v < ncolors else v % 8
    v = _hex_to_256(c)
    return v if v < ncolors else -1


class Attach:
    """One live attachment to a runner pane: a socket, a reader thread, a pyte screen."""

    def __init__(self, pane_id: str, rows: int, cols: int, on_data):
        self.pane_id = pane_id
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sock_path())
        self.sock.sendall((json.dumps({"method": "pane.attach", "id": 1, "agent": pane_id,
                                       "rows": rows, "cols": cols}) + "\n").encode())
        head = b""
        while not head.endswith(b"\n"):
            ch = self.sock.recv(1)
            if not ch:
                raise ConnectionError("runner closed")
            head += ch
        res = json.loads(head)
        if not res.get("ok"):
            raise LookupError(res.get("error", "attach failed"))
        self.info = res["agent"]
        self.on_data = on_data
        self.alive = True
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        try:
            while self.alive:
                data = self.sock.recv(65536)
                if not data:
                    break
                with self.lock:
                    try:
                        self.stream.feed(data)
                    except Exception:  # noqa: BLE001
                        pass
                self.on_data()
        except OSError:
            pass
        self.alive = False
        self.on_data()

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall((json.dumps({"input": base64.b64encode(data).decode()}) + "\n").encode())
        except OSError:
            self.alive = False

    def resize(self, rows: int, cols: int) -> None:
        with self.lock:
            self.screen.resize(rows, cols)
        try:
            self.sock.sendall((json.dumps({"resize": [rows, cols]}) + "\n").encode())
        except OSError:
            pass

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class App:
    def __init__(self, stdscr):
        self.scr = stdscr
        self.agents: List[dict] = []
        self.sel = 0
        self.mode = "sidebar"           # sidebar | agent
        self.attach: Optional[Attach] = None
        self.dirty = threading.Event()
        self.msg = ""
        self.msg_at = 0.0
        self.pairs: Dict[Tuple[int, int], int] = {}
        self.quit = False
        self.runner_ok = True
        self.help = False

    # -- setup ----------------------------------------------------------------
    def run(self) -> None:
        curses.raw()
        curses.noecho()
        curses.curs_set(0)
        self.scr.keypad(False)
        self.scr.nodelay(True)
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        self.ncolors = getattr(curses, "COLORS", 8)
        threading.Thread(target=self._poll, daemon=True).start()
        self.refresh_agents()
        self.ensure_attach()                      # show something right away
        self.dirty.set()
        while not self.quit:
            if self.dirty.wait(0.5):
                self.dirty.clear()
                self.draw()
            self.handle_keys()
        if self.attach:
            self.attach.close()

    def _poll(self) -> None:
        while not self.quit:
            time.sleep(1.0)
            self.refresh_agents()
            self.dirty.set()

    def refresh_agents(self) -> None:
        try:
            agents = call("agent.list", _timeout=2)["agents"]
            self.runner_ok = True
        except Exception:  # noqa: BLE001
            self.runner_ok = False
            agents = []
        me = os.environ.get("KANBOT_PANE_ID")           # never show the pane we run in
        agents = [a for a in agents if a["id"] != me]
        agents.sort(key=lambda a: (STATE_ORDER.get(a["state"], 9), -a["started_at"]))
        cur = self.agents[self.sel]["id"] if self.agents and self.sel < len(self.agents) else None
        self.agents = agents
        if cur:
            for i, a in enumerate(agents):
                if a["id"] == cur:
                    self.sel = i
                    break
        self.sel = max(0, min(self.sel, len(agents) - 1))
        if self.attach and not any(a["id"] == self.attach.pane_id for a in agents):
            self.attach.close(); self.attach = None

    # -- geometry -------------------------------------------------------------
    def pane_geom(self) -> Tuple[int, int, int, int]:
        h, w = self.scr.getmaxyx()
        rows, cols = max(2, h - 1), max(10, w - SIDEBAR_W - 1)
        return 0, SIDEBAR_W + 1, rows, cols

    def ensure_attach(self) -> None:
        if not self.agents:
            return
        want = self.agents[self.sel]["id"]
        if self.attach and self.attach.pane_id == want and self.attach.alive:
            return
        if self.attach:
            self.attach.close()
            self.attach = None
        _, _, rows, cols = self.pane_geom()
        try:
            self.attach = Attach(want, rows, cols, self.dirty.set)
        except Exception as e:  # noqa: BLE001
            self.flash(f"cannot attach: {e}")

    # -- drawing --------------------------------------------------------------
    def pair(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        if key in self.pairs:
            return self.pairs[key]
        n = len(self.pairs) + 1
        if n >= curses.COLOR_PAIRS:
            return 0
        try:
            curses.init_pair(n, fg, bg)
        except curses.error:
            return 0
        self.pairs[key] = n
        return n

    def flash(self, text: str) -> None:
        self.msg, self.msg_at = text, time.time()
        self.dirty.set()

    def draw(self) -> None:
        scr = self.scr
        h, w = scr.getmaxyx()
        scr.erase()
        self.draw_sidebar(h, w)
        for y in range(h - 1):
            try: scr.addstr(y, SIDEBAR_W, "│", curses.color_pair(self.pair(8, -1)))
            except curses.error: pass
        self.draw_pane()
        self.draw_status(h, w)
        if self.help:
            self.draw_help(h, w)
        scr.refresh()

    def draw_sidebar(self, h: int, w: int) -> None:
        scr = self.scr
        dim = curses.color_pair(self.pair(8, -1))
        lime = curses.color_pair(self.pair(10, -1)) | curses.A_BOLD
        scr.addstr(0, 1, " KANBOT ", curses.color_pair(self.pair(0, 10)) | curses.A_BOLD)
        scr.addstr(0, 10, f"{len([a for a in self.agents if a['alive']])} live", dim)
        if not self.runner_ok:
            scr.addstr(2, 1, "no runner on this machine", curses.color_pair(self.pair(1, -1)))
            scr.addstr(3, 1, "run:  kanbot up", dim)
            return
        if not self.agents:
            scr.addstr(2, 1, "no agents yet", dim)
            scr.addstr(3, 1, "n  start one", dim)
            return
        y = 2
        last_group = None
        for i, a in enumerate(self.agents):
            if y >= h - 2:
                break
            group = "NEEDS YOU" if a["state"] == "blocked" else ("FINISHED" if not a["alive"] else "AGENTS")
            if group != last_group:
                scr.addstr(y, 1, group, dim | curses.A_BOLD); y += 1; last_group = group
            if y >= h - 2:
                break
            col = {"blocked": 9, "working": 10, "idle": 11, "done": 8}.get(a["state"], 7)
            selected = i == self.sel
            attr = curses.A_REVERSE if selected and self.mode == "sidebar" else (curses.A_BOLD if selected else 0)
            title = (a["title"] or a["agent"])[: SIDEBAR_W - 6]
            try:
                scr.addstr(y, 1, " " * (SIDEBAR_W - 1), attr)
                scr.addstr(y, 1, MARK.get(a["state"], "?"), curses.color_pair(self.pair(col, -1)) | attr)
                scr.addstr(y, 3, title, attr)
                age = int(time.time() - a["started_at"])
                age_s = f"{age}s" if age < 60 else f"{age // 60}m" if age < 3600 else f"{age // 3600}h"
                sub = f"{a['agent']} · {os.path.basename(a['cwd'].rstrip('/')) or '~'} · {age_s}"
                scr.addstr(y + 1, 3, sub[: SIDEBAR_W - 4], dim | (attr & curses.A_REVERSE))
            except curses.error:
                pass
            y += 2
        if time.time() - self.msg_at < 4 and self.msg:
            try: scr.addstr(h - 2, 1, self.msg[: SIDEBAR_W - 2], lime)
            except curses.error: pass

    def draw_pane(self) -> None:
        y0, x0, rows, cols = self.pane_geom()
        scr = self.scr
        if not self.attach:
            hint = "select an agent · Enter to focus · n to start one" if self.agents else "press n to start an agent"
            try: scr.addstr(y0 + rows // 2, x0 + max(0, (cols - len(hint)) // 2), hint, curses.color_pair(self.pair(8, -1)))
            except curses.error: pass
            return
        at = self.attach
        with at.lock:
            if at.screen.lines != rows or at.screen.columns != cols:
                at.resize(rows, cols)
            buf = at.screen.buffer
            cursor = (at.screen.cursor.x, at.screen.cursor.y, at.screen.cursor.hidden)
            for y in range(rows):
                line = buf[y]
                x = 0
                while x < cols:
                    ch = line[x]
                    attr = curses.color_pair(self.pair(_color(ch.fg, self.ncolors), _color(ch.bg, self.ncolors)))
                    if ch.bold: attr |= curses.A_BOLD
                    if ch.reverse: attr |= curses.A_REVERSE
                    if ch.underscore: attr |= curses.A_UNDERLINE
                    if getattr(ch, "italics", False) and hasattr(curses, "A_ITALIC"): attr |= curses.A_ITALIC
                    # run of cells with the same attributes → one addstr
                    run = [ch.data]
                    x2 = x + 1
                    while x2 < cols:
                        c2 = line[x2]
                        if (c2.fg, c2.bg, c2.bold, c2.reverse, c2.underscore) != (ch.fg, ch.bg, ch.bold, ch.reverse, ch.underscore):
                            break
                        run.append(c2.data); x2 += 1
                    text = "".join(run)
                    try:
                        scr.addstr(y0 + y, x0 + x, text, attr)
                    except curses.error:
                        pass
                    x = x2
        cx, cy, hidden = cursor
        if self.mode == "agent" and not hidden and cy < rows and cx < cols:
            try:
                scr.chgat(y0 + cy, x0 + cx, 1, curses.A_REVERSE)
            except curses.error:
                pass

    def draw_status(self, h: int, w: int) -> None:
        a = self.agents[self.sel] if self.agents else None
        if self.mode == "agent" and a:
            left = f" {MARK.get(a['state'],'?')} {a['agent']} · {a['state']} · {a['id']} "
            right = " Ctrl-] sidebar "
        else:
            left = " j/k move · Enter focus · n new · x kill · d drop · ? help · q quit "
            right = f" {a['cwd']} " if a else ""
        try:
            self.scr.addstr(h - 1, 0, " " * (w - 1), curses.color_pair(self.pair(0, 8)))
            self.scr.addstr(h - 1, 0, left[: w - 1], curses.color_pair(self.pair(0, 8)))
            if right and len(left) + len(right) < w:
                self.scr.addstr(h - 1, w - len(right) - 1, right, curses.color_pair(self.pair(0, 8)))
        except curses.error:
            pass

    def draw_help(self, h: int, w: int) -> None:
        lines = ["KANBOT — keys", "",
                 "sidebar   j/k ↑/↓  move        Enter/Tab  focus the agent",
                 "          n  new agent          x  kill      d  drop finished",
                 "          r  refresh            q  quit (agents keep running)",
                 "agent     type normally; Ctrl-] returns to the sidebar",
                 "", "attach from anywhere:  kanbot attach <id>", "any key to close"]
        bw = max(len(l) for l in lines) + 4; bh = len(lines) + 2
        y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        win = curses.newwin(bh, bw, y0, x0)
        win.bkgd(" ", curses.color_pair(self.pair(15, 0)))
        win.box()
        for i, l in enumerate(lines):
            try: win.addstr(1 + i, 2, l)
            except curses.error: pass
        win.refresh()

    # -- input ----------------------------------------------------------------
    def read_keys(self) -> bytes:
        """Drain stdin into raw bytes (curses gives us ints; keypad is off)."""
        out = b""
        while True:
            try:
                c = self.scr.getch()
            except curses.error:
                break
            if c == -1:
                break
            if c == curses.KEY_RESIZE:
                self.dirty.set(); continue
            out += bytes([c & 0xFF]) if c < 256 else b""
            if c == 0x1B:                       # give an escape sequence 15ms to complete
                time.sleep(0.015)
        return out

    def handle_keys(self) -> None:
        data = self.read_keys()
        if not data:
            return
        if self.help:
            self.help = False; self.dirty.set(); return
        if self.mode == "agent":
            if b"\x1d" in data:                 # Ctrl-]
                before, _, _ = data.partition(b"\x1d")
                if before and self.attach: self.attach.send(before)
                self.mode = "sidebar"; curses.curs_set(0); self.dirty.set()
                return
            if self.attach:
                self.attach.send(data)
            return
        for key in self._sidebar_keys(data):
            self._sidebar_key(key)

    @staticmethod
    def _sidebar_keys(data: bytes):
        i = 0
        while i < len(data):
            if data[i:i + 3] in (b"\x1b[A", b"\x1bOA"): yield "up"; i += 3
            elif data[i:i + 3] in (b"\x1b[B", b"\x1bOB"): yield "down"; i += 3
            elif data[i] == 0x1B: yield "esc"; i += 1
            else: yield chr(data[i]); i += 1

    def _sidebar_key(self, k: str) -> None:
        if k in ("q", "\x03"):
            self.quit = True
        elif k in ("j", "down"):
            self.sel = min(len(self.agents) - 1, self.sel + 1) if self.agents else 0; self.dirty.set(); self.ensure_attach()
        elif k in ("k", "up"):
            self.sel = max(0, self.sel - 1); self.dirty.set(); self.ensure_attach()
        elif k in ("\r", "\n", "\t", "l", "\x1d"):
            self.ensure_attach()
            if self.attach and self.attach.info.get("alive", True):
                self.mode = "agent"; self.dirty.set()
        elif k == "n":
            self.new_agent()
        elif k == "x" and self.agents:
            a = self.agents[self.sel]
            try: call("agent.kill", agent=a["id"]); self.flash(f"killing {a['id']}")
            except Exception as e: self.flash(str(e))  # noqa: BLE001
        elif k == "d" and self.agents:
            a = self.agents[self.sel]
            if not a["alive"]:
                try: call("agent.remove", agent=a["id"]); self.refresh_agents(); self.dirty.set()
                except Exception as e: self.flash(str(e))  # noqa: BLE001
            else:
                self.flash("still running — x to kill first")
        elif k == "r":
            self.refresh_agents(); self.dirty.set()
        elif k == "?":
            self.help = True; self.dirty.set()

    def prompt(self, y: int, label: str, default: str = "") -> Optional[str]:
        h, w = self.scr.getmaxyx()
        curses.echo(); curses.curs_set(1); self.scr.nodelay(False)
        try:
            self.scr.addstr(y, 0, " " * (w - 1), curses.color_pair(self.pair(0, 10)))
            self.scr.addstr(y, 0, f" {label} ", curses.color_pair(self.pair(0, 10)) | curses.A_BOLD)
            if default:
                self.scr.addstr(y, len(label) + 3, f"[{default}] ", curses.color_pair(self.pair(0, 10)))
            self.scr.refresh()
            x = len(label) + 3 + (len(default) + 3 if default else 0)
            raw = self.scr.getstr(y, x, max(1, w - x - 2))
            text = raw.decode("utf-8", "replace").strip()
            return text or default
        except (curses.error, KeyboardInterrupt):
            return None
        finally:
            curses.noecho(); curses.curs_set(0); self.scr.nodelay(True)

    def new_agent(self) -> None:
        h, w = self.scr.getmaxyx()
        agent = self.prompt(h - 3, "agent", "claude")
        if agent is None: self.dirty.set(); return
        text = self.prompt(h - 2, "prompt", "")
        if text is None: self.dirty.set(); return
        cwd = self.prompt(h - 1, "cwd", os.getcwd())
        if cwd is None: self.dirty.set(); return
        try:
            res = call("agent.start", agent=agent, prompt=text, cwd=os.path.expanduser(cwd), interactive=True)
            self.refresh_agents()
            for i, a in enumerate(self.agents):
                if a["id"] == res["agent"]["id"]:
                    self.sel = i
            self.ensure_attach()
            self.mode = "agent"
            self.flash(f"started {res['agent']['id']}")
        except Exception as e:  # noqa: BLE001
            self.flash(f"start failed: {e}")
        self.dirty.set()


def main() -> int:
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(lambda scr: App(scr).run())
    except KeyboardInterrupt:
        pass
    return 0


def runner_alive() -> bool:
    try:
        call("ping", _timeout=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_stack(port: int = 8787) -> bool:
    """Start `kanbot up` detached if no runner answers. Returns True when a runner is up."""
    if runner_alive():
        return True
    import subprocess, sys
    from .config import config_dir
    log = open(config_dir() / "up.log", "ab")
    subprocess.Popen([sys.executable, "-m", "kanbot", "up", "--no-open", "--port", str(port)],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(60):
        time.sleep(0.25)
        if runner_alive():
            return True
    return False
