"""Test discovery -> /api/agent-sessions -> revive card (run=False, no real agent spawned)."""
import asyncio, os, tempfile, threading, time
import httpx, uvicorn

os.environ["DECKHAND_DB"] = tempfile.mktemp(suffix=".db")
os.environ["DECKHAND_HOME"] = tempfile.mkdtemp()

from deckhand.server.app import create_app
from deckhand.runner.worker import Runner
from deckhand.config import Config

PORT = 8801
BASE = f"http://127.0.0.1:{PORT}"


def serve():
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="error")


def main():
    threading.Thread(target=serve, daemon=True).start()
    for _ in range(60):
        try: httpx.get(BASE + "/api/health", timeout=0.5); break
        except Exception: time.sleep(0.1)

    cfg = Config(); cfg.server_url = BASE; cfg.runner_name = "disc-runner"
    runner = Runner(cfg, verbose=False)
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(runner.run_forever()), daemon=True).start()

    c = httpx.Client(base_url=BASE, timeout=15)
    # wait for the runner to connect AND push a discovery batch
    sessions = []
    for _ in range(80):
        sessions = c.get("/api/agent-sessions").json()["sessions"]
        if sessions: break
        time.sleep(0.2)

    print(f"discovered agent sessions via API: {len(sessions)}")
    assert sessions, "no agent sessions surfaced through the server"
    by_agent = {}
    for s in sessions:
        by_agent.setdefault(s["agent"], 0)
        by_agent[s["agent"]] += 1
    print("  by agent:", by_agent)
    print("  active (working now):", sum(1 for s in sessions if s["active"]))
    sample = sessions[0]
    for k in ("agent", "session_id", "cwd", "title", "turns", "active", "runner_id", "runner_name"):
        assert k in sample, f"missing key {k}"
    print("  sample:", sample["agent"], sample["session_id"][:8], "|", sample["title"][:50])

    board = c.get("/api/boards").json()["boards"]
    bid = board[0]["id"] if board else c.post("/api/boards", json={"name": "T"}).json()["board"]["id"]

    # revive WITHOUT running (run=False) so no real agent process is spawned
    card = c.post(f"/api/boards/{bid}/revive", json={
        "runner_id": sample["runner_id"], "agent": sample["agent"],
        "session_id": sample["session_id"], "cwd": sample["cwd"],
        "title": "test revive", "prompt": "continue", "run": False,
    }).json()
    print("revive card created:", card["id"], "resume_of:", card["resume_of"][:8],
          "pin_runner:", card["pin_runner"], "status:", card["status"])
    assert card["resume_of"] == sample["session_id"]
    assert card["pin_runner"] == sample["runner_id"]
    assert card["status"] == "idle"  # run=False -> stays in backlog

    # verify build_argv produces a real resume command for claude/codex
    from deckhand.runner.agents import detect_agents, build_argv
    agents = detect_agents(cfg)
    for name in ("claude", "codex"):
        if name in agents:
            argv = build_argv(agents[name], "do the thing", resume_of="abc-123")
            print(f"  {name} resume argv:", argv)
            assert "abc-123" in argv and "do the thing" in argv

    print("\n✅ REVIVE E2E PASSED: sessions discovered, surfaced via API, revive card pinned + resume-tagged.")


if __name__ == "__main__":
    main()
