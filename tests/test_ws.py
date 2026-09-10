"""Web socket proxy check against a running `kanbot up` (KANBOT_URL, default :8787)."""
import asyncio, base64, json, os, sys, urllib.request
URL = os.environ.get("KANBOT_URL", "http://127.0.0.1:8787")

def post(path, body):
    r = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(r))

async def main():
    import websockets
    pane = post("/api/panes/start", {"agent": "shell", "prompt": 'echo ws-hello; read -p "ok? (y/n) " a; echo a=$a; sleep 1', "cwd": "/tmp", "title": "ws-test"})
    pid = pane["id"]
    async with websockets.connect(URL.replace("http", "ws") + "/ws/web") as ws:
        await ws.send(json.dumps({"type": "pane.attach", "pane_id": pid, "rows": 24, "cols": 80}))
        got, blocked, deadline = b"", False, asyncio.get_event_loop().time() + 15
        while asyncio.get_event_loop().time() < deadline:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("type") == "pane.data" and m["pane_id"] == pid:
                got += base64.b64decode(m["data"])
            if m.get("type") == "agent.blocked" and m["pane"]["id"] == pid:
                blocked = True
                await ws.send(json.dumps({"type": "pane.input", "pane_id": pid, "keys": ["y", "Enter"]}))
            if b"a=y" in got:
                break
        assert b"ws-hello" in got, got
        assert blocked, "never saw agent.blocked"
        assert b"a=y" in got, got
        await ws.send(json.dumps({"type": "pane.detach"}))
    print("ws ok")

if __name__ == "__main__":
    asyncio.run(main())
