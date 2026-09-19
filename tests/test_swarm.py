"""Durability, peer delegation, activation and capacity tests without model spend."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from kanbot.swarm import Store, Swarm, SwarmHTTPError, parse_turn, stable, workspace_url
from kanbot.runner.api import ApiServer
from kanbot.runner.panes import PaneManager


class FakeAPI:
    def __init__(self):
        self.agents = {}
        self.posts = {}
        self.pages = {}
        self.starts = 0
        self.drop_result = False
        self.drop_registration = False
        self.tasks = {}

    async def close(self):
        pass

    async def call(self, agent, path, body=None, invite=False):
        if path == "/agents":
            if body is None:
                return {"agents": list(self.agents.values())}
            self.starts += 1
            key = stable(body["name"])
            item = {"id": key, **body}
            if any(a["name"] == body["name"] for a in self.agents.values()):
                raise SwarmHTTPError(409)
            self.agents[key] = item
            if self.drop_registration:
                raise httpx.ReadError("lost registration response")
            return {"agent": item, "token": "test-token-" + key}
        if path == "/me":
            return {"id": agent["id"]}
        if path == "/posts":
            item = self.posts.setdefault(body["id"], {**body, "author": agent["id"], "mentions": []})
            if body.get("parent") and self.drop_result:
                self.drop_result = False
                raise httpx.ReadError("lost result response")
            return item
        if path.startswith("/threads/"):
            item = self.posts[path.split("/")[-1]]
            root = self.posts.get(item.get("parent"), item)
            return {"root": root, "replies": [p for p in self.posts.values() if p.get("parent") == root["id"]]}
        if path.startswith("/inbox"):
            return {"events": [], "hasMore": False}
        if path == "/wiki":
            if body["id"] in self.pages:
                raise SwarmHTTPError(409)
            self.pages[body["id"]] = body
            return body
        if path.startswith("/wiki/page"):
            from urllib.parse import unquote
            return self.pages[unquote(path.split("=", 1)[1])]
        if path == "/tasks":
            if body:
                self.tasks.setdefault(body["id"], {**body, "status": "todo", "version": 1})
                return self.tasks[body["id"]]
            return {"tasks": list(self.tasks.values())}
        if path in ("/claim", "/task-status"):
            item = self.tasks[body["id"]]
            item.update(status=body.get("status", "doing"), version=item["version"] + 1)
            return item
        return {"ok": True}


class QuietSwarm(Swarm):
    def watch_agents(self):
        pass

    async def heartbeat(self):
        await self.refresh_directory()
        await asyncio.Event().wait()


class SwarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Path(__file__).resolve().parent.parent.joinpath("test-results").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent / "test-results")
        self.store = Store(self.temp.name)
        self.store.put("config", "main", {
            "api": "https://example.test/api/w/test", "workspace": "https://example.test/w/test",
            "allow": ["human-owner"], "runtimes": ["claude", "codex", "kimi"], "max_agents": 100,
            "concurrency": 2, "max_turns": 200, "max_depth": 8, "mode": "read",
            "directory": self.temp.name, "timeout": 10,
        })
        self.api = FakeAPI()
        self.runs = []
        async def driver(job, agent, prompt):
            self.runs.append((job["id"], agent["runtime"], job.get("session")))
            return {"text": "Completed", "session": "session-" + job["id"]}
        self.driver = driver
        self.swarm = QuietSwarm(None, self.store, self.api, driver)
        self.which = patch("kanbot.swarm.shutil.which", side_effect=lambda s: "/fake/" + s)
        self.which.start()
        self.agent = await self.swarm.register("fable", "claude")

    async def asyncTearDown(self):
        await self.swarm.stop()
        self.which.stop()
        self.store.db.close()
        self.temp.cleanup()

    async def wait_status(self, key, status="done"):
        for _ in range(400):
            job = self.store.get("job", key)
            if job and job["status"] == status:
                return job
            await asyncio.sleep(0.01)
        self.fail(f"job did not reach {status}: {self.store.get('job', key)}")

    async def test_branching_mixed_runtime_peers_release_slots_and_resume_exact_sessions(self):
        seen, active, peak = [], 0, 0
        async def driver(job, agent, prompt):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            seen.append((job["prompt"], job["round"], job.get("session")))
            await asyncio.sleep(0.02)
            active -= 1
            if job["prompt"] == "root" and job["round"] == 0:
                turn = {"message": "Splitting work", "delegate": [
                    {"runtime": "codex", "request": "build"}, {"runtime": "kimi", "request": "review"}]}
            elif job["prompt"] == "build" and job["round"] == 0:
                turn = {"message": "Need specialist", "delegate": [{"runtime": "claude", "request": "specialist"}]}
            else:
                turn = {"message": "Finished " + job["prompt"]}
            return {"text": json.dumps(turn), "session": "native-" + job["id"]}
        self.swarm.driver = driver
        await self.swarm.start()
        sent = await self.swarm.submit("fable", "root", "root-id")
        result = await self.wait_status(sent["job"])
        self.assertEqual(result["result"], "Finished root")
        self.assertEqual(peak, 2)
        self.assertEqual(len(self.store.all("agent")), 4)
        self.assertTrue(all(session for _, turn, session in seen if turn > 0))
        self.assertEqual(self.store.get("budget", sent["job"]), 6)

    async def test_lost_result_retries_delivery_without_repeating_model(self):
        self.api.drop_result = True
        job = self.swarm.new_job("job-lost", self.agent["id"], "work", "thread-lost")
        self.swarm.save_job(job)
        await self.swarm.process(job["id"])
        self.assertEqual(self.store.get("job", job["id"])["status"], "delivering")
        await self.swarm.process(job["id"])
        self.assertEqual(self.store.get("job", job["id"])["status"], "done")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(len(self.api.posts), 2)

    async def test_registration_lost_response_does_not_create_second_agent(self):
        self.api.drop_registration = True
        with self.assertRaises(httpx.ReadError):
            await self.swarm.register("reviewer", "codex")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            await self.swarm.register("reviewer", "codex")
        self.assertEqual(self.api.starts, 2)  # entry agent plus one ambiguous registration

    async def test_unmentioned_thread_reply_and_untrusted_sender_do_not_activate(self):
        self.api.posts["root"] = {"id": "root", "body": "FYI", "room": "general", "author": "human-owner", "mentions": []}
        event = {"id": 1, "type": "message", "actor": "human-owner", "objectId": "root"}
        await self.swarm.ingest(self.agent, event)
        self.assertEqual(self.store.all("job"), [])
        self.api.posts["root"]["mentions"] = [self.agent["id"]]
        event["actor"] = "untrusted"
        await self.swarm.ingest(self.agent, event)
        self.assertEqual(self.store.all("job"), [])
        event["actor"] = "human-owner"
        await self.swarm.ingest(self.agent, event)
        await self.swarm.ingest(self.agent, event)
        self.assertEqual(len(self.store.all("job")), 1)

    async def test_internal_delegation_post_is_not_executed_twice(self):
        self.api.posts["own"] = {"id": "own", "body": "@fable Work", "room": "general", "author": "human-owner", "mentions": [self.agent["id"]]}
        self.store.put("ownpost", "own", True)
        await self.swarm.ingest(self.agent, {"id": 1, "actor": "human-owner", "type": "message", "objectId": "own"})
        self.assertEqual(self.store.all("job"), [])

    async def test_failed_ack_is_retried_after_saved_cursor_without_duplicate_job(self):
        self.api.posts["request"] = {"id": "request", "body": "@fable work", "room": "general", "author": "human-owner", "mentions": [self.agent["id"]]}
        original = self.api.call
        acks = []
        async def call(agent, path, body=None, invite=False):
            if path == "/inbox?after=0":
                return {"events": [{"id": 1, "actor": "human-owner", "type": "message", "objectId": "request"}], "hasMore": False}
            if path == "/ack":
                acks.append(body["ids"])
                if len(acks) == 1:
                    raise httpx.ReadError("ack response lost")
            return await original(agent, path, body, invite)
        self.api.call = call
        self.swarm.running = True
        with self.assertRaises(httpx.ReadError):
            await self.swarm.drain(self.agent["id"])
        self.assertEqual(self.store.get("agent", self.agent["id"])["cursor"], 1)
        await self.swarm.drain(self.agent["id"])
        self.assertEqual(acks, [[1], [1]])
        self.assertEqual(self.store.get("ack", self.agent["id"]), [])
        self.assertEqual(len(self.store.all("job")), 1)

    async def test_duplicate_submission_and_payload_conflict(self):
        await self.swarm.start()
        one = await self.swarm.submit("fable", "task", "stable")
        two = await self.swarm.submit("fable", "task", "stable")
        self.assertEqual(one["job"], two["job"])
        with self.assertRaisesRegex(ValueError, "different work"):
            await self.swarm.submit("fable", "different", "stable")

    async def test_concurrent_starts_share_one_scheduler_and_state_owner(self):
        await asyncio.gather(self.swarm.start(), self.swarm.start())
        self.assertEqual(len(self.swarm.background), 2)  # scheduler + heartbeat
        other = QuietSwarm(None, Store(self.temp.name), self.api, self.driver)
        try:
            with self.assertRaises(BlockingIOError):
                await other.start()
            self.assertTrue(self.swarm.running)
        finally:
            other.store.db.close()

    async def test_restart_keeps_interrupted_execution_uncertain(self):
        job = self.swarm.new_job("interrupted", self.agent["id"], "work", "thread")
        job["status"] = "running"
        self.swarm.save_job(job)
        await self.swarm.start()
        self.assertEqual(self.store.get("job", job["id"])["status"], "uncertain")
        self.assertEqual(self.runs, [])

    async def test_restart_recovers_saved_result_and_session(self):
        job = self.swarm.new_job("completed", self.agent["id"], "work", "thread")
        job["status"] = "running"
        self.swarm.save_job(job)
        path = self.swarm.result_path(job)
        path.parent.mkdir()
        path.write_text(json.dumps({"ok": True, "text": "Recovered", "session": "native-id"}))
        await self.swarm.start()
        result = await self.wait_status(job["id"])
        self.assertEqual(result["result"], "Recovered")
        self.assertEqual(result["session"], "native-id")
        self.assertEqual(self.runs, [])

    async def test_root_budget_and_cycle_limit(self):
        job = self.swarm.new_job("budget", self.agent["id"], "work", "thread")
        self.store.put("budget", "budget", 200)
        self.swarm.save_job(job)
        await self.swarm.process(job["id"])
        self.assertEqual(self.store.get("job", job["id"])["status"], "blocked")
        self.assertEqual(self.runs, [])
        job.update(status="delivering", turn={"text": json.dumps({"delegate": [{"to": "fable", "request": "loop"}]})})
        self.swarm.save_job(job)
        await self.swarm.process(job["id"])
        self.assertIn("Cyclic", self.store.get("job", job["id"])["error"])

    async def test_unknown_runtime_never_falls_back_to_shell(self):
        with self.assertRaisesRegex(ValueError, "no fallback"):
            await self.swarm.register("bad", "shell")
        self.assertEqual(self.api.starts, 1)

    async def test_cancel_stops_active_task_and_children(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()
        async def driver(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.swarm.driver = driver
        await self.swarm.start()
        sent = await self.swarm.submit("fable", "long")
        await entered.wait()
        await self.swarm.cancel(sent["job"])
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.store.get("job", sent["job"])["status"], "cancelled")

    async def test_hundred_agents_bound_active_capacity(self):
        cfg = self.store.config
        cfg["concurrency"] = 10
        self.store.put("config", "main", cfg)
        for i in range(99):
            await self.swarm.register("worker-" + str(i), ["claude", "codex", "kimi"][i % 3])
        peak, active = 0, 0
        async def driver(job, agent, prompt):
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"text": "done", "session": job["id"]}
        self.swarm.driver = driver
        await self.swarm.start()
        ids = [(await self.swarm.submit(a["name"], "work"))["job"] for a in self.store.all("agent")]
        await asyncio.gather(*(self.wait_status(key) for key in ids))
        self.assertEqual(peak, 10)
        self.assertEqual(len(self.store.all("job")), 100)
        cfg["concurrency"] = 100
        self.store.put("config", "main", cfg)
        peak = 0
        ids = [(await self.swarm.submit(a["name"], "second task"))["job"] for a in self.store.all("agent")]
        await asyncio.gather(*(self.wait_status(key) for key in ids))
        self.assertEqual(peak, 100)
        with self.assertRaisesRegex(ValueError, "limit"):
            await self.swarm.register("one-too-many", "claude")

    async def test_large_result_retry_recovers_existing_wiki_page(self):
        async def driver(*_):
            return {"text": "result " * 1200, "session": "native"}
        self.swarm.driver = driver
        self.api.drop_result = True
        job = self.swarm.new_job("large", self.agent["id"], "work", "thread")
        self.swarm.save_job(job)
        await self.swarm.process(job["id"])
        await self.swarm.process(job["id"])
        self.assertEqual(self.store.get("job", job["id"])["status"], "done")
        self.assertEqual(len(self.api.pages), 1)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_servers_cannot_replace_live_socket(self):
        # Keep under macOS's Unix socket path length limit.
        Path("test-results").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ks-", dir="test-results") as temp:
            path = str(Path(temp) / "runner.sock")
            first, second = ApiServer(PaneManager(), path=path), ApiServer(PaneManager(), path=path)
            try:
                await first.start()
                with self.assertRaisesRegex(RuntimeError, "already owns"):
                    await second.start()
                await second.stop()
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(b'{"method":"ping","id":1}\n')
                await writer.drain()
                self.assertTrue(json.loads(await reader.readline())["pong"])
                writer.close()
                await writer.wait_closed()
            finally:
                await first.stop()


class ContractTests(unittest.TestCase):
    def test_url_and_output_validation(self):
        self.assertEqual(workspace_url("https://example.test/w/demo#invite=secret")["invite"], "secret")
        for url in ("https://example.test/", "http://remote.test/w/demo", "https://user:pass@example.test/w/demo"):
            with self.assertRaises(ValueError):
                workspace_url(url)
        self.assertEqual(parse_turn("plain answer")["message"], "plain answer")
        with self.assertRaises(ValueError):
            parse_turn('{"delegate":[{"runtime":"shell","request":"bad"}]}')


if __name__ == "__main__":
    Path(__file__).resolve().parent.parent.joinpath("test-results").mkdir(exist_ok=True)
    unittest.main()
