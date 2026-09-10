"""User/runner configuration stored in ~/.kanbot/config.json."""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Optional


def config_dir() -> Path:
    base = os.environ.get("KANBOT_HOME") or os.environ.get("DECKHAND_HOME")
    if base:
        path = Path(base).expanduser()
    else:
        path = Path.home() / ".kanbot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


def db_path() -> Path:
    env = os.environ.get("KANBOT_DB") or os.environ.get("DECKHAND_DB")
    if env:
        return Path(env).expanduser()
    return config_dir() / "kanbot.db"


@dataclass
class Config:
    """Local config used by both the runner and convenience commands."""

    server_url: str = "http://127.0.0.1:8787"
    token: str = ""
    runner_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    runner_name: str = field(default_factory=lambda: socket.gethostname())
    # Map of agent-name -> override command template. Empty = use built-in defaults.
    agent_overrides: Dict[str, str] = field(default_factory=dict)
    # Agents the user has explicitly disabled.
    disabled_agents: list = field(default_factory=list)
    max_concurrency: int = 2
    # When True (default) agents run with auto-approve flags (edit unattended).
    # Safe mode (False) drops those flags so the agent can't act without asking.
    auto_approve: bool = True
    # Extra session stores to track, e.g. Hermes or any agent that logs JSONL:
    #   [{"name": "hermes", "label": "Hermes", "root": "~/.hermes/sessions",
    #     "pattern": "*.jsonl", "recursive": true, "fmt": "claude"}]
    discovery_sources: list = field(default_factory=list)
    # Provider API keys, keyed by AGENT name (claude, codex, glm, kimi, gemini…).
    # Injected into each agent's api_key_env when its subprocess runs. Keyed by
    # agent (not env var) so claude-compatible providers that share
    # ANTHROPIC_API_KEY (z.ai, Kimi) can each hold their own key without clashing.
    provider_keys: Dict[str, str] = field(default_factory=dict)
    # Shell command run when an agent needs you (state → blocked) or a TUI
    # finishes. Gets KANBOT_TITLE, KANBOT_BODY, KANBOT_STATE, KANBOT_PANE_ID,
    # KANBOT_AGENT in its env. "" = platform default (macOS notification banner).
    # Point it at anything: a Telegram curl, ntfy.sh, `say`, a Slack webhook.
    notify_command: str = ""

    def key_env_for(self, agent_name: str) -> Dict[str, str]:
        """Env overrides carrying the configured API key for an agent (if any)."""
        from .agents import BUILTIN_BY_NAME
        spec = BUILTIN_BY_NAME.get(agent_name)
        key = self.provider_keys.get(agent_name)
        if spec and spec.api_key_env and key:
            return {spec.api_key_env: key}
        return {}

    @classmethod
    def load(cls) -> "Config":
        p = config_path()
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known}
        cfg = cls(**clean)
        return cfg

    def save(self) -> None:
        config_path().write_text(json.dumps(asdict(self), indent=2))

    @property
    def ws_url(self) -> str:
        url = self.server_url.rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url
