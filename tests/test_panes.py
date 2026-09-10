"""Smallest checks that fail if the pane layer breaks. Run: python tests/test_panes.py"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kanbot.runner.panes import PaneManager, classify_screen, keys_to_bytes, strip_ansi


def test_classify():
    assert classify_screen("Edit file?\nDo you want to proceed? (y/n)", 5) == "blocked"
    assert classify_screen("compiling...\n", 0.5) == "working"
    assert classify_screen("done.\n❯ ", 5) == "idle"
    assert classify_screen("go? (y/n) y\na=y\nhost:tmp me$ ", 5) == "idle"      # answered → idle
    assert classify_screen("Do you want to proceed?\n❯ 1. Yes\n  2. No", 3) == "blocked"
    assert classify_screen("Allow command? [y/N]", 0.2) == "working"           # still printing
    assert classify_screen("● Thinking… (esc to interrupt)", 5) == "working"
    assert keys_to_bytes(["y", "Enter", "C-c", "Escape"]) == b"y\r\x03\x1b"
    assert strip_ansi("\x1b[32mhi\x1b[0m\r\n") == "hi\n"


async def _pane_roundtrip():
    lines, states = [], []
    async def on_line(p, t): lines.append(t)
    async def on_state(p, s): states.append(s)
    pm = PaneManager(on_line=on_line, on_state=on_state)
    pane = pm.spawn(["bash", "-c", "echo hello; read x; echo got:$x; exit 3"],
                    agent="shell", interactive=False)
    got = bytearray(); pane.subscribers.append(got.extend)
    for _ in range(50):
        if b"hello" in pane.buf: break
        await asyncio.sleep(0.05)
    assert b"hello" in pane.buf and b"hello" in got
    assert pane.alive and pane.pid > 0
    pane.write(b"world\r")
    rc = await pane.wait(timeout=5)
    assert rc == 3, rc
    assert pane.state == "done" and pane.exit_code == 3
    assert "hello" in lines and any(l.startswith("got:world") for l in lines), lines
    assert "done" in states
    assert "got:world" in pane.read_text()
    # hooks beat screen rules
    p2 = pm.spawn(["sleep", "5"], agent="shell")
    await pm.report(p2.id, "blocked", "hook")
    assert p2.state == "blocked" and not p2.set_state("idle", "screen")
    w = asyncio.ensure_future(p2.wait_state("idle", timeout=3))
    await asyncio.sleep(0.05); await pm.report(p2.id, "idle")
    assert await w == "idle"
    assert pm.get(p2.id[:4]) is p2 and pm.get("latest") is p2
    p2.kill(); assert await p2.wait(3) is not None and not p2.alive
    pm.prune(max_age=0); assert not pm.panes


def test_pane():
    asyncio.run(_pane_roundtrip())


if __name__ == "__main__":
    test_classify(); test_pane(); print("ok")
