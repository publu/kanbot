"""KanBot command-line interface.

  kanbot up            # start server + a local runner together (best first run)
  kanbot server        # just the web server / API / board
  kanbot runner        # just the background runner (connects to a server)
  kanbot agents        # show which CLI coding agents are detected here
  kanbot config        # view / set server URL, token, runner name
  kanbot open          # open the board in your browser
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
import webbrowser

from . import __version__
from .config import Config, config_path, db_path


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
    if not args.no_open:
        try:
            webbrowser.open(base)
        except Exception:
            pass

    cfg = Config.load()
    cfg.server_url = base
    if args.name:
        cfg.runner_name = args.name
    if args.concurrency:
        cfg.max_concurrency = args.concurrency
    cfg.save()
    runner = Runner(cfg)
    print(f"Local runner '{cfg.runner_name}' attaching with agents: "
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
    return 0


def cmd_config(args) -> int:
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
    if changed:
        cfg.save()
        print(f"saved {config_path()}")
    print(f"server_url      : {cfg.server_url}")
    print(f"token           : {'(set)' if cfg.token else '(none)'}")
    print(f"runner_name     : {cfg.runner_name}")
    print(f"runner_id       : {cfg.runner_id}")
    print(f"max_concurrency : {cfg.max_concurrency}")
    print(f"disabled_agents : {', '.join(cfg.disabled_agents) or '(none)'}")
    return 0


def cmd_open(args) -> int:
    cfg = Config.load()
    url = args.server or cfg.server_url
    print(f"opening {url}")
    webbrowser.open(url)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kanbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"kanbot {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("up", help="start server + local runner (recommended first run)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--db", default=None)
    sp.add_argument("--name", default=None, help="runner name")
    sp.add_argument("--concurrency", type=int, default=None)
    sp.add_argument("--no-open", action="store_true", help="don't open the browser")
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
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("open", help="open the board in a browser")
    sp.add_argument("--server", default=None)
    sp.set_defaults(func=cmd_open)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
