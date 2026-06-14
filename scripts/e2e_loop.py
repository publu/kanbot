"""E2E: Ralph loop — run a shell agent with fresh context until a stop predicate."""
import asyncio, os, tempfile, threading, time
import httpx, uvicorn

os.environ["KANBOT_DB"] = tempfile.mktemp(suffix=".db")
os.environ["KANBOT_HOME"] = tempfile.mkdtemp()

from kanbot.server.app import create_app
from kanbot.runner.worker import Runner
from kanbot.config import Config

PORT = 8806
BASE = f"http://127.0.0.1:{PORT}"
SCRATCH = tempfile.mkdtemp(prefix="kanbot-loop-")


def serve():
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="error")


def main():
    threading.Thread(target=serve, daemon=True).start()
    for _ in range(60):
        try: httpx.get(BASE + "/api/health", timeout=0.5); break
        except Exception: time.sleep(0.1)
    cfg = Config(); cfg.server_url = BASE; cfg.runner_name = "loop-runner"
    runner = Runner(cfg, verbose=True)
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(runner.run_forever()), daemon=True).start()
    time.sleep(1.0)

    c = httpx.Client(base_url=BASE, timeout=30)
    bid = c.post("/api/boards", json={"name": "Loop", "repo_path": SCRATCH}).json()["board"]["id"]

    # each iteration increments ./count; stop once count >= 2 (so it should run twice)
    prompt = 'n=$(cat count 2>/dev/null || echo 0); echo $((n+1)) > count; echo "ran, count=$(cat count)"'
    until = 'test "$(cat count 2>/dev/null || echo 0)" -ge 2'
    card = c.post(f"/api/boards/{bid}/cards", json={
        "title": "ralph loop", "prompt": prompt, "agent": "shell", "cwd": SCRATCH,
        "loop_max": 5, "loop_until": until}).json()
    print("card loop_max:", card["loop_max"], "loop_until:", card["loop_until"])
    c.post(f"/api/cards/{card['id']}/run")

    for _ in range(80):
        s = c.get(f"/api/sessions?card_id={card['id']}").json()["sessions"]
        if s and s[0]["status"] in ("success", "failed", "cancelled"): break
        time.sleep(0.2)
    sess = s[0]
    events = c.get(f"/api/sessions/{sess['id']}").json()["events"]
    log = "\n".join(e["text"] for e in events)
    print("--- loop log ---")
    for e in events:
        if e["stream"] == "system" or "count=" in e["text"]:
            print("  ", e["text"])

    iters = sum(1 for e in events if "━ iteration" in e["text"])
    final_count = int(open(os.path.join(SCRATCH, "count")).read().strip())
    stopped = "stop condition met" in log
    print(f"\nstatus={sess['status']} | iterations run={iters} | final count={final_count} | stopped_early={stopped}")
    assert sess["status"] == "success"
    assert iters == 2, f"expected 2 iterations, got {iters}"
    assert final_count == 2
    assert stopped
    print("\n✅ RALPH LOOP E2E PASSED: ran fresh-context iterations until the stop predicate.")


if __name__ == "__main__":
    main()
