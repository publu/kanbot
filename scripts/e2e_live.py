"""Live e2e: run a real 'hello world' through KanBot with actual agents
(Claude + Codex), then resume the Claude session. Spawns real agent processes."""
import asyncio, os, tempfile, threading, time
import httpx, uvicorn

os.environ["DECKHAND_DB"] = tempfile.mktemp(suffix=".db")
os.environ["DECKHAND_HOME"] = tempfile.mkdtemp()

from deckhand.server.app import create_app
from deckhand.runner.worker import Runner
from deckhand.config import Config

PORT = 8804
BASE = f"http://127.0.0.1:{PORT}"
SCRATCH = tempfile.mkdtemp(prefix="kanbot-scratch-")


def serve():
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="error")


def wait_session(c, card_id, timeout=180):
    for _ in range(timeout * 5):
        s = c.get(f"/api/sessions?card_id={card_id}").json()["sessions"]
        if s and s[0]["status"] in ("success", "failed", "cancelled"):
            return s[0]
        time.sleep(0.2)
    return s[0] if s else None


def logtext(c, sid):
    return "\n".join(e["text"] for e in c.get(f"/api/sessions/{sid}").json()["events"])


def run_task(c, bid, agent, prompt):
    card = c.post(f"/api/boards/{bid}/cards", json={
        "title": f"{agent} hello", "prompt": prompt, "agent": agent, "cwd": SCRATCH}).json()
    c.post(f"/api/cards/{card['id']}/run")
    sess = wait_session(c, card["id"])
    return card, sess


def main():
    threading.Thread(target=serve, daemon=True).start()
    for _ in range(60):
        try: httpx.get(BASE + "/api/health", timeout=0.5); break
        except Exception: time.sleep(0.1)

    cfg = Config(); cfg.server_url = BASE; cfg.runner_name = "live-runner"
    runner = Runner(cfg, verbose=True)
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(runner.run_forever()), daemon=True).start()
    time.sleep(1.0)

    c = httpx.Client(base_url=BASE, timeout=30)
    caps = c.get("/api/runners").json()["runners"][0]["capabilities"]
    print("runner caps:", caps, "| scratch:", SCRATCH)
    bid = c.post("/api/boards", json={"name": "Live", "repo_path": SCRATCH}).json()["board"]["id"]

    results = {}

    # ---- Claude hello world ----
    if "claude" in caps:
        print("\n[1] Claude hello world …")
        prompt = "Reply with exactly the two words: hello world. Do not use any tools. Do not read, create, or modify any files."
        _, sess = run_task(c, bid, "claude", prompt)
        out = logtext(c, sess["id"]).lower()
        ok = sess["status"] == "success" and "hello world" in out
        print(f"    status={sess['status']} exit={sess['exit_code']}  hello-world-in-output={'hello world' in out}")
        results["claude run"] = ok

        # ---- resume that Claude session (close the resume loop, live) ----
        print("[2] Resume the Claude session …")
        rsid = None
        for _ in range(40):
            for s in c.get("/api/agent-sessions").json()["sessions"]:
                if s["agent"] == "claude" and os.path.realpath(s["cwd"]) == os.path.realpath(SCRATCH):
                    rsid = s; break
            if rsid: break
            time.sleep(0.5)
        if rsid:
            card = c.post(f"/api/boards/{bid}/revive", json={
                "runner_id": rsid["runner_id"], "agent": "claude", "session_id": rsid["session_id"],
                "cwd": SCRATCH, "title": "resume", "prompt": "Now reply with exactly: goodbye world", "run": True}).json()
            sess = wait_session(c, card["id"])
            out = logtext(c, sess["id"]).lower()
            ok = sess["status"] == "success" and "goodbye" in out
            print(f"    resume status={sess['status']}  goodbye-in-output={'goodbye' in out}")
            results["claude resume"] = ok
        else:
            print("    (could not locate the new claude session to resume)")
            results["claude resume"] = False

    # ---- Codex hello world ----
    if "codex" in caps:
        print("\n[3] Codex hello world …")
        prompt = "Reply with exactly the two words: hello world. Do not modify any files."
        _, sess = run_task(c, bid, "codex", prompt)
        out = logtext(c, sess["id"]).lower()
        ok = sess["status"] == "success" and "hello world" in out
        print(f"    status={sess['status']} exit={sess['exit_code']}  hello-world-in-output={'hello world' in out}")
        results["codex run"] = ok

    print("\n==== RESULTS ====")
    for k, v in results.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print("ALL PASS" if results and all(results.values()) else "SOME FAILED")


if __name__ == "__main__":
    main()
