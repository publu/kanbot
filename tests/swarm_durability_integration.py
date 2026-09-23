"""Real private swarm API; deterministic drivers and two independent host stores.
No production identities, external sends or paid models. Tests dropped delivery,
remote delegation/root accounting, cited synthesis, and fresh-host recovery.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from unittest.mock import patch

import httpx
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kanbot.swarm import Store, Swarm, SwarmAPI, stable


class DropResponse(SwarmAPI):
    dropped = False
    dropped_handoff = False

    async def call(self, agent, path, body=None, invite=False):
        result = await super().call(agent, path, body, invite)
        if path == "/executions" and body.get("action") == "result" and not self.dropped:
            self.dropped = True
            raise httpx.ReadError("Injected lost completion response")
        if path == "/executions" and body.get("action") == "handoff" and not self.dropped_handoff:
            self.dropped_handoff = True
            raise httpx.ReadError("Injected lost handoff response")
        return result


async def scenario(origin, directory):
    async with httpx.AsyncClient(base_url=origin) as owner:
        response = await owner.post("/api/workspaces", json={"id": "durability", "name": "Durability fixture", "visibility": "private"})
        response.raise_for_status()
        api_url = origin + "/api/w/durability"
        invite = (await owner.post(api_url + "/invites", json={"maxUses": 10})).json()["token"]
        await owner.post(api_url + "/wiki", json={"id": "facts", "title": "Recovery evidence", "body": "Completed output must survive a lost response. Unknown effects require reconciliation.", "expectedRevision": 0, "sources": []})
        cfg = {"api": api_url, "workspace": origin + "/w/durability", "invite": invite,
               "allow": ["fixture-owner"], "runtimes": ["claude", "codex", "kimi", "hermes"], "max_agents": 10,
               "concurrency": 4, "max_turns": 5, "max_depth": 5, "mode": "read", "directory": str(directory), "timeout": 30}
        stores = [Store(directory / name) for name in ("host-a", "host-b", "host-c")]
        for store in stores:
            store.put("config", "main", cfg)
        calls = []

        async def driver(job, agent, prompt):
            calls.append((job["id"], job["round"], agent["runtime"]))
            evidence = job.get("knowledge_context", {}).get("sources", [])
            assert evidence, "Agent must receive sourced swarm information"
            assert "untrusted" in prompt
            if job["prompt"] == "Recovery evidence synthesis" and not job["children"]:
                return {"text": json.dumps({"message": "Splitting recovery review", "delegate": [
                    {"to": "coder", "request": "Recovery evidence implementation"},
                    {"to": "researcher", "request": "Recovery evidence research"},
                    {"to": "reviewer", "request": "Recovery evidence independent review"}]})}
            return {"text": json.dumps({"message": "Verified recovery evidence", "knowledge": {
                "title": "Recovery findings", "body": "Saved results survive delivery loss; unknown effects need reconciliation.",
                "sources": [evidence[0]["url"]]}})}

        api_a, api_b = DropResponse(cfg), SwarmAPI(cfg)
        a, b = Swarm(None, stores[0], api_a, driver), Swarm(None, stores[1], api_b, driver)
        c = None
        try:
            with patch("kanbot.swarm.shutil.which", return_value="/fixture/runtime"):
                root_agent = await a.register("lead", "claude")
                await a.register("coder", "codex")
                await a.register("researcher", "kimi")
                remote = await b.register("reviewer", "hermes")
            stores[1].put("config", "main", {**cfg, "allow": [root_agent["id"]]})
            await a.refresh_directory()
            job = a.new_job("root-job", root_agent["id"], "Recovery evidence synthesis", "root-thread")
            a.save_job(job)
            await a.process(job["id"])
            assert stores[0].get("job", job["id"])["status"] == "delivering", stores[0].get("job", job["id"])
            assert len(calls) == 1
            # New runner instance reads the saved server result; no repeat model call.
            a = Swarm(None, stores[0], api_a, driver)
            await a.refresh_directory()
            await a.process(job["id"])
            assert stores[0].get("job", job["id"])["status"] == "delivering"
            a = Swarm(None, stores[0], api_a, driver)
            await a.refresh_directory()
            await a.process(job["id"])
            root = stores[0].get("job", job["id"])
            assert root["status"] == "waiting", root
            assert len(calls) == 1
            for child_id in root["children"]:
                await a.process(child_id)
                child = stores[0].get("job", child_id)
                if child["agent"] == remote["id"]:
                    assert child["status"] == "remote", child
                    await b.ingest(remote, {"id": 100, "type": "message", "actor": root_agent["id"], "objectId": child["thread"]})
                    await b.process(child_id)
                    remote_child = stores[1].get("job", child_id)
                    assert remote_child["status"] == "done", remote_child
                    assert remote_child["root"] == "root-job"
                    await a.ingest(root_agent, {"id": 101, "type": "reply", "actor": remote["id"], "objectId": stable(child_id, 0, "result")})
                else:
                    assert child["status"] == "done", child
            # The parent's host disappears while waiting. A fresh host with only
            # the parent identity recovers completed siblings from shared records.
            stores[2].put("agent", root_agent["id"], root_agent)
            api_c = SwarmAPI(cfg)
            c = Swarm(None, stores[2], api_c, driver)
            await c.recover_shared()
            await c.process(job["id"])
            waiting = stores[2].get("job", job["id"])
            assert waiting["status"] == "waiting", waiting
            assert all(stores[2].get("job", key)["status"] == "done" for key in waiting["children"])
            c.running = True
            c.schedule()
            await asyncio.gather(*list(c.active.values()))
            c.running = False
            # The original host reconnects too: it reuses the completed parent turn.
            a.running = True
            a.schedule()
            await asyncio.gather(*list(a.active.values()))
            a.running = False
            final = stores[0].get("job", job["id"])
            assert final["status"] == "done", final
            assert len(calls) == 5, calls
            assert {runtime for _, _, runtime in calls} == {"claude", "codex", "kimi", "hermes"}
            records = await api_a.call(root_agent, "/executions?root=root-job")
            assert records["budgets"]["root-job"]["used"] == 5
            assert records["budgets"]["root-job"]["limit"] == 5
            assert len(records["executions"]) == 5
            assert all(e["phase"] == "delivered" for e in records["executions"])
            pages = (await api_a.call(root_agent, "/wiki"))["pages"]
            assert len([p for p in pages if p["id"].startswith("insights/")]) == 4
            assert all(p["sources"] for p in pages if p["id"].startswith("insights/"))
            # A fresh host can reconstruct and redeliver completed jobs from the API.
            await api_c.close()
            stores.append(Store(directory / "host-d"))
            stores[3].put("config", "main", cfg)
            for agent in stores[0].all("agent"):
                stores[3].put("agent", agent["id"], agent)
            api_c = SwarmAPI(cfg)
            c = Swarm(None, stores[3], api_c, driver)
            await c.recover_shared()
            for recovered in stores[3].all("job"):
                await c.process(recovered["id"])
                assert stores[3].get("job", recovered["id"])["status"] == "done"
            assert len(calls) == 5, "Recovery repeated completed model work"
            thread = await api_a.call(root_agent, "/threads/root-thread")
            assert len(thread["replies"]) == 2, "Recovery duplicated a result post"
            print(json.dumps({"passed": True, "hosts": 4, "runtime_fixtures": 4, "turns": 5,
                "verified": ["lost completion response", "lost handoff response", "server result reuse", "cross-host delegation",
                "shared root budget", "waiting parent host loss", "single parent continuation", "cited wiki synthesis", "fresh-host recovery", "idempotent redelivery"]}, indent=2))
        finally:
            for swarm in (a, b, c):
                if swarm:
                    await swarm.stop()
            await api_a.close()
            await api_b.close()
            if c:
                await api_c.close()
            for store in stores:
                store.db.close()


def main():
    root = Path(__file__).resolve().parent.parent
    (root / "test-results").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="durability-", dir=root / "test-results") as temp:
        directory = Path(temp)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        env = {**os.environ, "BOTSPACE_LOCAL_DB": str(directory / "swarm.sqlite"), "BOTSPACE_PORT": str(port)}
        with (directory / "server.log").open("w") as log:
            server = subprocess.Popen(["node", "api/index.mjs"], cwd=root.parent / "botspace", env=env, stdout=log, stderr=log)
            try:
                import time
                for _ in range(100):
                    try:
                        httpx.get(origin + "/api/workspaces").raise_for_status(); break
                    except httpx.TransportError:
                        time.sleep(0.05)
                asyncio.run(scenario(origin, directory))
            finally:
                server.terminate(); server.wait(timeout=10)


if __name__ == "__main__":
    main()
