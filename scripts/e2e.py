"""End-to-end smoke test: server + runner + a real shell task, no UI."""
import asyncio, os, tempfile, threading, time
import httpx, uvicorn

os.environ["KANBOT_DB"] = tempfile.mktemp(suffix=".db")
os.environ["KANBOT_HOME"] = tempfile.mkdtemp()

from kanbot.server.app import create_app
from kanbot.runner.worker import Runner
from kanbot.config import Config

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def serve():
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


def main():
    threading.Thread(target=serve, daemon=True).start()
    for _ in range(60):
        try:
            httpx.get(BASE + "/api/health", timeout=0.5); break
        except Exception:
            time.sleep(0.1)

    cfg = Config(); cfg.server_url = BASE; cfg.runner_name = "test-runner"
    runner = Runner(cfg, verbose=True)
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: (loop.run_until_complete(runner.run_forever())), daemon=True).start()
    time.sleep(1.0)  # let runner connect

    c = httpx.Client(base_url=BASE, timeout=10)
    health = c.get("/api/health").json()
    print("health:", health)
    runners = c.get("/api/runners").json()["runners"]
    print("runners:", [(r["name"], r["status"], r["capabilities"]) for r in runners])
    assert any(r["status"] != "offline" for r in runners), "runner did not connect"

    board = c.get("/api/boards").json()["boards"]
    if not board:
        board = [c.post("/api/boards", json={"name": "Test"}).json()["board"]]
    bid = board[0]["id"]

    card = c.post(f"/api/boards/{bid}/cards", json={
        "title": "say hello", "prompt": "echo HELLO_FROM_DECKHAND && pwd && echo done",
        "agent": "shell", "cwd": os.getcwd(),
    }).json()
    print("card created:", card["id"], card["status"])

    c.post(f"/api/cards/{card['id']}/run")
    print("queued; waiting for completion…")

    sess = None
    for _ in range(100):
        sessions = c.get(f"/api/sessions?card_id={card['id']}").json()["sessions"]
        if sessions:
            sess = sessions[0]
            if sess["status"] in ("success", "failed", "cancelled"):
                break
        time.sleep(0.2)

    assert sess, "no session created"
    print("session status:", sess["status"], "exit:", sess["exit_code"])
    detail = c.get(f"/api/sessions/{sess['id']}").json()
    print("--- session log ---")
    for ev in detail["events"]:
        print(f"  [{ev['stream']}] {ev['text']}")
    text = "\n".join(ev["text"] for ev in detail["events"])
    assert "HELLO_FROM_DECKHAND" in text, "expected output not found"
    assert sess["status"] == "success", f"expected success, got {sess['status']}"

    card_after = next(x for x in c.get(f"/api/boards/{bid}").json()["cards"] if x["id"] == card["id"])
    print("card status after run:", card_after["status"])
    assert card_after["status"] == "done", f"expected done, got {card_after['status']}"

    print("\n✅ E2E PASSED: task queued, executed by runner, streamed logs, landed in Done.")


if __name__ == "__main__":
    main()
