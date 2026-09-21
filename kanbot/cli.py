"""KanBot command-line interface.

  kanbot               # the terminal app: agents on the left, live agent on the right
  kanbot up            # start server + a local runner together (best first run)
  kanbot server        # just the web server / API / board
  kanbot runner        # just the background runner (connects to a server)
  kanbot agents        # show which CLI coding agents are detected here
  kanbot config        # view / set server URL, token, runner name
  kanbot open          # open the board in your browser
  kanbot ps            # live agents on this machine (state, pane id)
  kanbot attach ID     # your terminal becomes that agent's pane (Ctrl-] detaches)
  kanbot agent …       # start / prompt / keys / read / wait / kill an agent
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import webbrowser

from . import __version__
from .config import Config, config_path, db_path
from .update import update_notice


def _rich():
    try:
        from rich.console import Console
        return Console()
    except Exception:
        return None


def cmd_server(args) -> int:
    import uvicorn
    from .server.app import create_app

    app = create_app(db_path=args.db)
    url = f"http://{args.host}:{args.port}"
    print(f"KanBot server v{__version__}  →  {url}")
    print(f"  db:    {args.db or db_path()}")
    print(f"  open:  {url}  (then run `kanbot runner` on any machine)")
    if notice := update_notice():
        print(f"  {notice}")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_runner(args) -> int:
    from .runner.worker import Runner

    cfg = Config.load()
    if args.server:
        cfg.server_url = args.server
    if args.token:
        cfg.token = args.token
    if args.name:
        cfg.runner_name = args.name
    if args.concurrency:
        cfg.max_concurrency = args.concurrency
    if getattr(args, "safe", False):
        cfg.auto_approve = False
    cfg.save()

    runner = Runner(cfg)
    try:
        asyncio.run(runner.run_forever())
    except KeyboardInterrupt:
        print("\nrunner stopped.")
    return 0


def cmd_up(args) -> int:
    """Start the server in-process and attach a local runner. One command demo."""
    import uvicorn
    from .runner.worker import Runner
    from .server.app import create_app

    app = create_app(db_path=args.db)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)

    def serve():
        asyncio.run(server.serve())

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    # wait for the server to come up
    import httpx
    base = f"http://{args.host}:{args.port}"
    for _ in range(50):
        try:
            httpx.get(base + "/api/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    print(f"KanBot is up  →  {base}")
    print(f"  open:  {base}")
    if notice := update_notice():
        print(f"  {notice}")
    if not args.no_open:
        try:
            # Open the local board directly: the server serves the UI and the API
            # same-origin, so the browser talks straight to this backend.
            # ponytail: localhost, not the hosted client — the UI uses relative
            # paths, so it only works served from the server that owns the API.
            webbrowser.open(base)
        except Exception:
            pass

    cfg = Config.load()
    cfg.server_url = base
    if args.name:
        cfg.runner_name = args.name
    if args.concurrency:
        cfg.max_concurrency = args.concurrency
    if getattr(args, "safe", False):
        cfg.auto_approve = False
    cfg.save()
    runner = Runner(cfg)
    mode = "auto-approve" if cfg.auto_approve else "SAFE (no auto-approve flags)"
    print(f"Local runner '{cfg.runner_name}' [{mode}] attaching with agents: "
          f"{', '.join(runner.agents) or '(none — install claude/codex/etc.)'}")
    print("Press Ctrl-C to stop.\n")
    try:
        asyncio.run(runner.run_forever())
    except KeyboardInterrupt:
        print("\nshutting down.")
        server.should_exit = True
    return 0


def cmd_agents(args) -> int:
    from .runner.agents import detect_agents
    from .agents import BUILTIN_AGENTS

    cfg = Config.load()
    found = detect_agents(cfg)
    console = _rich()
    if console:
        from rich.table import Table
        table = Table(title="CLI agents on this machine")
        table.add_column("agent")
        table.add_column("status")
        table.add_column("description")
        for spec in BUILTIN_AGENTS:
            ok = spec.name in found
            disabled = spec.name in cfg.disabled_agents
            status = "[green]available[/green]" if ok else (
                "[yellow]disabled[/yellow]" if disabled else "[dim]not found[/dim]")
            table.add_row(spec.name, status, spec.description)
        console.print(table)
    else:
        for spec in BUILTIN_AGENTS:
            mark = "✓" if spec.name in found else "·"
            print(f" {mark} {spec.name:14} {spec.description}")
    print(f"\nadvertised capabilities: {', '.join(found) or '(none)'}")

    # Models + which providers have an API key configured.
    print("\nmodels & keys (set with: kanbot config --set-key <agent>=<key>):")
    for spec in BUILTIN_AGENTS:
        if not spec.models:
            continue
        has_key = spec.name in cfg.provider_keys
        keyed = "🔑" if has_key else ("env" if spec.api_key_env in os.environ else "—")
        models = ", ".join(m + (" *" if m == spec.default_model else "") for m in spec.models)
        print(f"  {keyed:>3} {spec.name:12} [{spec.api_key_env or 'n/a'}]  {models}")

    # Session trackers: which TUIs KanBot can see / revive.
    from .runner.discovery import active_providers, builtin_providers
    trackers = active_providers(cfg.discovery_sources)
    active_names = {t["name"] for t in trackers}
    print("\nsession trackers (TUIs KanBot watches):")
    for p in builtin_providers():
        mark = "✓" if p.name in active_names else "·"
        state = "tracking" if p.name in active_names else "no sessions found"
        print(f"  {mark} {p.label:14} {p.root}  [{state}]")
    for t in trackers:
        if t["name"] not in ("claude", "codex"):
            print(f"  ✓ {t['label']:14} {t['root']}  [custom]")
    print("\nTrack another agent: add to discovery_sources in "
          f"{config_path()}, e.g.\n"
          '  {"name": "hermes", "label": "Hermes", "root": "~/.hermes/sessions",\n'
          '   "pattern": "*.jsonl", "recursive": true, "fmt": "claude"}')
    return 0


def cmd_config(args) -> int:
    from .agents import BUILTIN_BY_NAME
    cfg = Config.load()
    changed = False
    if args.server:
        cfg.server_url = args.server; changed = True
    if args.token is not None:
        cfg.token = args.token; changed = True
    if args.name:
        cfg.runner_name = args.name; changed = True
    if args.concurrency:
        cfg.max_concurrency = args.concurrency; changed = True
    if args.disable:
        for a in args.disable:
            if a not in cfg.disabled_agents:
                cfg.disabled_agents.append(a)
        changed = True
    if args.enable:
        cfg.disabled_agents = [a for a in cfg.disabled_agents if a not in args.enable]
        changed = True
    if args.safe:
        cfg.auto_approve = False; changed = True
    if args.unsafe:
        cfg.auto_approve = True; changed = True
    for pair in (args.set_key or []):
        agent, _, key = pair.partition("=")
        agent, key = agent.strip(), key.strip()
        if not agent or not key:
            print(f"bad --set-key '{pair}' (use agent=KEY, e.g. glm=sk-...)"); continue
        cfg.provider_keys[agent] = key; changed = True
    for agent in (args.unset_key or []):
        cfg.provider_keys.pop(agent, None); changed = True
    if changed:
        cfg.save()
        print(f"saved {config_path()}")
    print(f"server_url      : {cfg.server_url}")
    print(f"token           : {'(set)' if cfg.token else '(none)'}")
    print(f"runner_name     : {cfg.runner_name}")
    print(f"runner_id       : {cfg.runner_id}")
    print(f"max_concurrency : {cfg.max_concurrency}")
    print(f"disabled_agents : {', '.join(cfg.disabled_agents) or '(none)'}")
    print(f"mode            : {'auto-approve (agents act unattended)' if cfg.auto_approve else 'SAFE (no auto-approve flags)'}")
    keyed = ", ".join(f"{a} ({BUILTIN_BY_NAME[a].api_key_env})" if a in BUILTIN_BY_NAME else a
                      for a in cfg.provider_keys) if cfg.provider_keys else "(none)"
    print(f"provider_keys   : {keyed}")
    return 0


def cmd_open(args) -> int:
    cfg = Config.load()
    url = args.server or cfg.server_url
    print(f"opening {url}")
    webbrowser.open(url)
    return 0


# -- the local socket API: agent control -----------------------
def _api(method: str, **params):
    from .runner.api import call
    try:
        return call(method, **params)
    except (ConnectionRefusedError, FileNotFoundError):
        print("no runner on this machine (start one with `kanbot up` or `kanbot runner`)",
              file=sys.stderr)
        raise SystemExit(2)


def _age(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h"


def cmd_ps(args) -> int:
    agents = _api("agent.list")["agents"]
    if getattr(args, "json", False):
        print(json.dumps(agents, indent=2)); return 0
    if not agents:
        print("no agents running. try: kanbot agent start claude \"fix the tests\" --cwd ~/repo")
        return 0
    marks = {"working": "●", "blocked": "◆", "idle": "○", "done": "✓", "unknown": "?"}
    print(f"{'ID':9} {'STATE':9} {'AGENT':9} {'AGE':5} {'CWD':28} TITLE")
    for a in agents:
        cwd = a["cwd"]; cwd = "…" + cwd[-27:] if len(cwd) > 28 else cwd
        print(f"{a['id']:9} {marks.get(a['state'], '?')} {a['state']:7} {a['agent']:9} "
              f"{_age(a['started_at']):5} {cwd:28} {a['title'][:50]}")
    return 0


def cmd_attach(args) -> int:
    from .runner.api import attach
    try:
        return attach(args.id)
    except (ConnectionRefusedError, FileNotFoundError):
        print("no runner on this machine (start one with `kanbot up`)", file=sys.stderr)
        return 2


def cmd_agent(args) -> int:
    sub = args.agent_cmd
    if sub == "list":
        return cmd_ps(args)
    if sub == "start":
        res = _api("agent.start", agent=args.agent, prompt=args.prompt or "",
                   cwd=os.path.abspath(args.cwd) if args.cwd else "",
                   interactive=not args.headless, resume=args.resume or "",
                   title=args.title or "")
        a = res["agent"]
        print(f"{a['id']}  ({a['agent']} · {a['state']})  attach: kanbot attach {a['id']}")
        if args.attach:
            return cmd_attach(argparse.Namespace(id=a["id"]))
        return 0
    if sub == "prompt":
        _api("agent.prompt", agent=args.id, text=args.text, enter=not args.no_enter); return 0
    if sub == "keys":
        _api("agent.send_keys", agent=args.id, keys=args.keys); return 0
    if sub == "read":
        res = _api("agent.read", agent=args.id, lines=args.lines)
        print(res["text"]); return 0
    if sub == "wait":
        res = _api("agent.wait", agent=args.id, until=args.until, timeout=args.timeout,
                   _timeout=(args.timeout + 5) if args.timeout else None)
        print(res["state"] + (f" (exit {res['exit_code']})" if res["exit_code"] is not None else ""))
        return 0 if res["state"] == args.until or args.until == "any" else 1
    if sub == "kill":
        _api("agent.kill", agent=args.id); return 0
    if sub == "rm":
        _api("agent.remove", agent=args.id); return 0
    if sub == "get":
        print(json.dumps(_api("agent.get", agent=args.id)["agent"], indent=2)); return 0
    return 1


def cmd_tui(args) -> int:
    """The terminal app. Starts the stack in the background first if needed."""
    from .tui import ensure_stack, main as tui_main, runner_alive
    if not runner_alive():
        import socket as _s
        port = args.port
        with _s.socket() as probe:
            probe.settimeout(0.3)
            busy = probe.connect_ex(("127.0.0.1", port)) == 0
        if busy:
            try:
                import httpx
                ok = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).json().get("ok")
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                print(f"port {port} is taken by something that isn't KanBot. "
                      f"Free it, or run: kanbot --port {port + 1}", file=sys.stderr)
                return 2
        print("starting the KanBot server + runner in the background…")
        if not ensure_stack(port):
            print("could not start a runner (see ~/.kanbot/up.log)", file=sys.stderr)
            return 2
    return tui_main()


def cmd_hook(args) -> int:
    """Claude Code lifecycle hook → runner state. Must be fast and never fail."""
    pane = os.environ.get("KANBOT_PANE_ID")
    sock = os.environ.get("KANBOT_SOCK")
    if not pane or not sock:
        return 0
    try:
        sys.stdin.read()          # the hook payload; we don't need it
    except Exception:  # noqa: BLE001
        pass
    state = args.state
    try:
        from .runner.api import call
        call("pane.report_state", path=sock, pane_id=pane, state=state, source="hook", _timeout=2)
    except Exception:  # noqa: BLE001
        pass
    return 0


def cmd_review(args) -> int:
    import asyncio
    import os
    import subprocess

    repo = os.path.abspath(args.repo)
    if args.base:
        diff = subprocess.run(["git", "-C", repo, "diff", f"{args.base}...HEAD"],
                              capture_output=True, text=True).stdout
    else:
        # Everything not yet on HEAD: staged + unstaged.
        diff = subprocess.run(["git", "-C", repo, "diff", "HEAD"],
                              capture_output=True, text=True).stdout
    if not diff.strip():
        print("no changes to review (try --base <ref>)")
        return 1

    if getattr(args, "gate", False):
        from .review import gate_review
        out = asyncio.run(gate_review(diff, repo_path=repo, title=args.title))
    else:
        from .review import review
        out = asyncio.run(review(diff, repo_path=repo, title=args.title, depth=args.depth))
    print(out.markdown)
    # Exit 2 on REQUEST_CHANGES so the runner sees a Gate rejection (non-zero) and
    # loops the work back. 0 means the gate passed.
    return 0 if out.event != "REQUEST_CHANGES" else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kanbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"kanbot {__version__}")
    sub = p.add_subparsers(dest="cmd")
    from .swarm_cli import add_parser as add_swarm_parser
    add_swarm_parser(sub)

    sp = sub.add_parser("up", help="start server + local runner (recommended first run)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--db", default=None)
    sp.add_argument("--name", default=None, help="runner name")
    sp.add_argument("--concurrency", type=int, default=None)
    sp.add_argument("--no-open", action="store_true", help="don't open the browser")
    sp.add_argument("--safe", action="store_true", help="safe mode: drop agent auto-approve flags")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("server", help="run the web server / API only")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--db", default=None)
    sp.add_argument("--log-level", default="info")
    sp.set_defaults(func=cmd_server)

    sp = sub.add_parser("runner", help="run the background runner only")
    sp.add_argument("--server", default=None, help="server URL (e.g. http://host:8787)")
    sp.add_argument("--token", default=None)
    sp.add_argument("--name", default=None)
    sp.add_argument("--concurrency", type=int, default=None)
    sp.add_argument("--safe", action="store_true", help="safe mode: drop agent auto-approve flags")
    sp.set_defaults(func=cmd_runner)

    sp = sub.add_parser("agents", help="show detected CLI agents")
    sp.set_defaults(func=cmd_agents)

    sp = sub.add_parser("config", help="view or set configuration")
    sp.add_argument("--server", default=None)
    sp.add_argument("--token", default=None)
    sp.add_argument("--name", default=None)
    sp.add_argument("--concurrency", type=int, default=None)
    sp.add_argument("--disable", nargs="*", help="agent names to disable")
    sp.add_argument("--enable", nargs="*", help="agent names to re-enable")
    sp.add_argument("--safe", action="store_true", help="enable safe mode (no auto-approve flags)")
    sp.add_argument("--unsafe", action="store_true", help="disable safe mode (auto-approve, default)")
    sp.add_argument("--set-key", nargs="*", metavar="AGENT=KEY",
                    help="set a provider API key, e.g. --set-key glm=sk-... kimi=sk-...")
    sp.add_argument("--unset-key", nargs="*", metavar="AGENT", help="remove provider API key(s)")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("open", help="open the board in a browser")
    sp.add_argument("--server", default=None)
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("tui", help="the terminal app (what bare `kanbot` runs)")
    sp.add_argument("--port", type=int, default=8787, help="server port if the stack must be started")
    sp.set_defaults(func=cmd_tui)

    sp = sub.add_parser("ps", help="live agents on this machine")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ps)

    sp = sub.add_parser("attach", help="attach your terminal to an agent's pane (Ctrl-] detaches)")
    sp.add_argument("id", nargs="?", default="latest", help="pane id / prefix (default: latest)")
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("agent", help="control agents: start / prompt / keys / read / wait / kill")
    asub = sp.add_subparsers(dest="agent_cmd", required=True)
    a = asub.add_parser("list", help="same as kanbot ps"); a.add_argument("--json", action="store_true")
    a = asub.add_parser("start", help="open an agent in a new pane")
    a.add_argument("agent", help="claude · codex · gemini · shell · …")
    a.add_argument("prompt", nargs="?", default="")
    a.add_argument("--cwd", default="")
    a.add_argument("--resume", default="", help="agent session id to resume")
    a.add_argument("--title", default="")
    a.add_argument("--headless", action="store_true", help="print/exec mode instead of the TUI")
    a.add_argument("--attach", action="store_true", help="attach right away")
    a = asub.add_parser("prompt", help="type a prompt into the agent and press Enter")
    a.add_argument("id"); a.add_argument("text"); a.add_argument("--no-enter", action="store_true")
    a = asub.add_parser("keys", help="send keys, tmux-style: y Enter · Escape · C-c")
    a.add_argument("id"); a.add_argument("keys", nargs="+")
    a = asub.add_parser("read", help="print the last lines of the pane")
    a.add_argument("id"); a.add_argument("--lines", type=int, default=40)
    a = asub.add_parser("wait", help="block until the agent reaches a state")
    a.add_argument("id"); a.add_argument("--until", default="idle",
                                         choices=["idle", "blocked", "working", "done", "any"])
    a.add_argument("--timeout", type=float, default=None)
    a = asub.add_parser("kill", help="terminate the agent"); a.add_argument("id")
    a = asub.add_parser("rm", help="drop an exited pane"); a.add_argument("id")
    a = asub.add_parser("get", help="pane details as JSON"); a.add_argument("id")
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("_hook", help=argparse.SUPPRESS)
    sp.add_argument("state")
    sp.set_defaults(func=cmd_hook)

    sp = sub.add_parser("review", help="AI code review of local changes (multi-agent)")
    sp.add_argument("--repo", default=".", help="repo path to review (default: cwd)")
    sp.add_argument("--base", default="", help="base ref to diff against (default: staged+unstaged vs HEAD)")
    sp.add_argument("--depth", default="auto", choices=["auto", "quick", "standard", "deep"])
    sp.add_argument("--gate", action="store_true",
                    help="fast single-pass verdict (for use as a chain Gate); exits 2 if it blocks")
    sp.add_argument("--title", default="", help="title for the change set")
    sp.set_defaults(func=cmd_review)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    # Bare `kanbot` (or `uvx kanbot`) opens the terminal app, starting the
    # server + runner in the background if they aren't up. `kanbot --port N` too.
    raw = sys.argv[1:] if argv is None else argv
    if not raw or raw[0] == "--port":
        raw = ["tui"] + raw
    args = parser.parse_args(raw)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
