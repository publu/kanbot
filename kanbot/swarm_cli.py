"""Short, non-blocking CLI for Kanbot's built-in swarm connection."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import config_dir, Config
from .runner.api import call, sock_path
from .update import latest_version, newer
from .swarm import Store, Swarm, SwarmAPI, gc_trees, workspace_url, RUNTIMES


def add_parser(sub):
    parser = sub.add_parser("swarm", help="connect Kanbot to a swarm and delegate across agents")
    commands = parser.add_subparsers(dest="swarm_command", required=True)
    connect = commands.add_parser("connect", help="save a swarm connection and register its entry agent")
    connect.add_argument("url")
    connect.add_argument("--name", default="fable")
    connect.add_argument("--runtime", choices=sorted(RUNTIMES), default="claude")
    connect.add_argument("--model", default="")
    connect.add_argument("--directory", default=os.getcwd())
    connect.add_argument("--mode", choices=["read", "work"], default="read")
    connect.add_argument("--allow-from", required=True, help="comma-separated trusted sender IDs or exact registered names")
    connect.add_argument("--runtimes", default="claude,codex,kimi", help="installed runtimes agents may request")
    connect.add_argument("--invite-file", help="private invitation token or invitation URL in a file")
    connect.add_argument("--instructions", help="local project scope text file")
    connect.add_argument("--concurrency", type=int, default=4)
    connect.add_argument("--max-agents", type=int, default=100)
    connect.add_argument("--max-turns", type=int, default=200)
    connect.add_argument("--max-depth", type=int, default=8)
    connect.add_argument("--timeout", type=int, default=600)
    connect.add_argument("--no-start", action="store_true")
    for cmd in ("start", "pause", "status"):
        commands.add_parser(cmd)
    send = commands.add_parser("send", help="give a managed agent work from any terminal or coding session")
    send.add_argument("to")
    group = send.add_mutually_exclusive_group()
    group.add_argument("--text")
    group.add_argument("--file")
    send.add_argument("--task", help="work on this saved swarm task instead of creating another task")
    send.add_argument("--request-id", help="reuse this ID to safely retry submission")
    for cmd in ("job", "cancel"):
        p = commands.add_parser(cmd)
        p.add_argument("id")
    gc = commands.add_parser("gc", help="archive agent files of finished job trees, then remove their worktrees")
    gc.add_argument("--root", action="extend", nargs="+", metavar="ID", help="only these root jobs (default: every finished tree)")
    gc.add_argument("--apply", action="store_true", help="archive, verify and remove; without it this is a dry-run report")
    commands.add_parser("_serve", help=__import__('argparse').SUPPRESS)
    parser.set_defaults(func=main)


async def configure(args):
    target = workspace_url(args.url)
    if args.invite_file:
        secret = Path(args.invite_file).read_text().strip()
        if "://" in secret:
            invited = workspace_url(secret)
            if invited["workspace"] != target["workspace"]:
                raise ValueError("Invitation is for a different swarm")
            secret = invited["invite"]
        target["invite"] = secret
    directory = Path(args.directory).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("Choose a project directory")
    if not shutil.which("node"):
        raise ValueError("Node.js 22.13+ is required for runtime adapters")
    version = subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v").split(".")
    if tuple(map(int, version[:2])) < (22, 13):
        raise ValueError("Node.js 22.13+ is required")
    requested = args.runtimes.split(",")
    if not set(requested) <= RUNTIMES:
        raise ValueError("Supported swarm runtimes: claude,codex,kimi")
    runtimes = [r for r in requested if shutil.which(r)]
    if args.runtime not in runtimes:
        raise ValueError("Entry runtime must be installed and enabled")
    if not (1 <= args.concurrency <= 100 and args.concurrency <= args.max_agents <= 10000 and 1 <= args.max_turns <= 10000
            and 1 <= args.max_depth <= 20 and 10 <= args.timeout <= 3600):
        raise ValueError("Use concurrency <= 100, concurrency <= max-agents <= 10000, max-turns 1–10000, depth 1–20, timeout 10–3600")
    store = Store()
    old = store.config
    if old:
        if old["workspace"] != target["workspace"] or old["name"] != args.name:
            raise ValueError("This Kanbot home already belongs to another swarm/entry agent. Use a separate KANBOT_HOME.")
        if old.get("ready"):
            return {"reused": True, "workspace": old["workspace"], "name": old["name"]}
    cfg = {**target, "name": args.name, "directory": str(directory), "mode": args.mode,
           "runtimes": runtimes, "concurrency": args.concurrency, "max_agents": args.max_agents,
           "max_turns": args.max_turns, "max_depth": args.max_depth, "timeout": args.timeout,
           "instructions": Path(args.instructions).read_text() if args.instructions else "", "allow": []}
    api = SwarmAPI(cfg)
    try:
        # Names are resolved once and pinned to stable IDs, never trusted again
        # solely because somebody reuses the same display name.
        peers = (await api.call(None, "/agents"))["agents"] if not cfg.get("invite") else []
        raw = [s.strip() for s in args.allow_from.split(",") if s.strip()]
        if not raw:
            raise ValueError("Choose specific trusted senders")
        for sender in raw:
            found = next((p for p in peers if not p.get("demo") and sender in (p["id"], p["name"])), None)
            if found:
                cfg["allow"].append(found["id"])
            elif sender.startswith(("human-", "account:")) or __import__('re').fullmatch(r"[0-9a-f-]{36}", sender):
                cfg["allow"].append(sender)
            elif cfg.get("invite"):
                # Resolve after private registration below; do not persist
                # names as execution authority.
                pass
            else:
                raise ValueError(f"Unknown sender {sender!r}; use a registered name or exact human ID")
        store.put("config", "main", cfg)
        swarm = Swarm(None, store, api)
        agent = await swarm.register(args.name, args.runtime, args.model)
        peers = (await api.call(agent, "/agents"))["agents"]
        for sender in raw:
            found = next((p for p in peers if not p.get("demo") and sender in (p["id"], p["name"])), None)
            if found:
                cfg["allow"].append(found["id"])
            elif sender not in cfg["allow"]:
                raise ValueError(f"Unknown sender {sender!r}; connection remains paused")
        cfg["allow"] = sorted(set(cfg["allow"]))
        cfg["ready"] = True
        store.put("config", "main", cfg)
        store.put("config", "paused", True)
        return {"connected": True, "workspace": cfg["workspace"], "name": agent["name"], "runtimes": runtimes}
    finally:
        await api.close()


async def serve():
    from .runner.worker import Runner
    runner = Runner(Config.load(), verbose=False)
    await runner._boot_panes()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        # A new process may activate only after an explicit swarm start.
        await stopped.wait()
    finally:
        await runner.swarm.stop()
        for pane in list(runner.panes.panes.values()):
            if pane.alive:
                await pane.terminate()
        await runner.api.stop()


def start():
    # Serialize simultaneous startup requests from different Claude sessions.
    with (config_dir() / "swarm-start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return call("swarm.start", _timeout=25)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        log_path = config_dir() / "swarm.log"
        with log_path.open("a") as log:
            os.chmod(log_path, 0o600)
            child = subprocess.Popen([sys.executable, "-m", "kanbot", "swarm", "_serve"],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(50):
            if child.poll() is not None:
                raise RuntimeError("Kanbot runner failed to start; inspect " + str(log_path))
            try:
                return call("swarm.start", _timeout=25)
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.1)
        raise RuntimeError("Kanbot is starting; run kanbot swarm status")


def main(args):
    try:
        cmd = args.swarm_command
        if cmd == "_serve":
            asyncio.run(serve())
            return 0
        if cmd == "connect":
            # No concurrent config writers from different interactive clients.
            with (config_dir() / "swarm-config.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = asyncio.run(configure(args))
            if not args.no_start:
                result["service"] = start()
        elif cmd == "start":
            result = start()
        elif cmd in ("job", "cancel"):
            result = call("swarm." + cmd, job=args.id, _timeout=25)
        elif cmd == "send":
            text = Path(args.file).read_text() if args.file else args.text
            if text is None and not args.task:
                raise ValueError("Supply --text, --file, or --task")
            if text is not None and (not text.strip() or len(text) > 60000):
                raise ValueError("Submit 1–60000 characters")
            with (config_dir() / "swarm-send.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                store = Store()
                pending = store.get("outbox", "send")
                if pending and not args.request_id and (pending["to"] != args.to or pending["text"] != text or pending.get("task") != args.task):
                    raise ValueError("A previous submission is unresolved; repeat it or use an explicit --request-id")
                request_id = args.request_id or (pending or {}).get("request_id") or str(__import__('uuid').uuid4())
                store.put("outbox", "send", {"to": args.to, "text": text, "task": args.task, "request_id": request_id})
                try:
                    result = call("swarm.send", to=args.to, text=text, task=args.task, request_id=request_id, _timeout=25)
                except RuntimeError:
                    store.put("outbox", "send", None)  # definite server rejection, not a lost transport response
                    raise
                store.put("outbox", "send", None)
        elif cmd == "gc":
            # Runs in this process, so a live runner needs no restart. It only
            # touches trees where every job is terminal; those never run again.
            with (config_dir() / "swarm-gc.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = gc_trees(Store(), args.root, args.apply)
        else:
            try:
                result = call("swarm." + cmd, _timeout=25)
            except (FileNotFoundError, ConnectionRefusedError):
                store = Store()
                if cmd == "pause":
                    store.put("config", "paused", True)
                result = {"running": False, "paused": store.get("config", "paused", True),
                          "workspace": store.config.get("workspace")}
        if cmd == "status":
            # Here, not in the runner: its event loop must not wait on the network.
            latest = latest_version()
            result.update(latestVersion=latest, updateAvailable=newer(latest))
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as error:
        print(str(error), file=sys.stderr)
        return 2
