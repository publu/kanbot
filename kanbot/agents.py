"""Built-in CLI agent catalog, shared by the server (display) and runner (execution).

Each agent is defined declaratively so adding "whatever else is available in the
CLI" is a one-liner here, or a config override on the runner side. The runner
detects which `bin` are on PATH and only advertises those it finds.

Command templates use Python str.format with:
    {prompt}  -> the task prompt (already shell-safe; passed as a single argv item)
The command is a list of argv tokens; the runner substitutes {prompt} per-token
so no shell quoting is needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AgentSpec:
    name: str            # stable id, e.g. "claude"
    label: str           # display name
    bin: str             # executable to look for on PATH
    argv: List[str]      # argv template; tokens may contain {prompt}
    description: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    color: str = "#8b5cf6"
    # argv template to resume/continue an existing agent session. Tokens may
    # contain {prompt} and {session_id}. Empty => resume not supported.
    resume_argv: List[str] = field(default_factory=list)
    # Safe-mode argv: same command without auto-approve/yolo flags (so the agent
    # can't act unattended). Empty => no safer variant; argv is used as-is.
    safe_argv: List[str] = field(default_factory=list)
    safe_resume_argv: List[str] = field(default_factory=list)
    # --- model catalog (surfaced to the UI/API; used by the review engine) ----
    models: List[str] = field(default_factory=list)   # selectable model ids for this provider
    default_model: str = ""    # premium/default model ("" = let the CLI decide)
    fast_model: str = ""       # cheap model for classification gates ("" = use default_model)
    model_flag: str = "--model"  # how this CLI takes a model on argv
    # --- provider auth --------------------------------------------------------
    # Env var this provider's API key belongs in. KanBot injects the key the user
    # configured (Config.provider_keys[name]) into this var for the subprocess.
    # claude-compatible providers (z.ai, Kimi) reuse ANTHROPIC_API_KEY but each
    # runs in its own subprocess env, so the keys never collide.
    api_key_env: str = ""


# Non-interactive / headless invocations for each known coding CLI.
BUILTIN_AGENTS: List[AgentSpec] = [
    AgentSpec(
        name="claude",
        label="Claude Code",
        bin="claude",
        argv=["claude", "-p", "{prompt}", "--dangerously-skip-permissions"],
        resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}",
                     "--dangerously-skip-permissions"],
        safe_argv=["claude", "-p", "{prompt}"],
        safe_resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}"],
        description="Anthropic Claude Code in headless print mode.",
        color="#d97757",
        models=["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        default_model="claude-opus-4-8",
        fast_model="claude-haiku-4-5-20251001",
        api_key_env="ANTHROPIC_API_KEY",
    ),
    AgentSpec(
        name="codex",
        label="Codex",
        bin="codex",
        argv=["codex", "exec", "--sandbox", "workspace-write",
              "--skip-git-repo-check", "{prompt}"],
        resume_argv=["codex", "exec", "resume", "--skip-git-repo-check",
                     "{session_id}", "{prompt}"],
        safe_argv=["codex", "exec", "--sandbox", "read-only",
                   "--skip-git-repo-check", "{prompt}"],
        safe_resume_argv=["codex", "exec", "resume", "--skip-git-repo-check",
                          "{session_id}", "{prompt}"],
        description="OpenAI Codex CLI, non-interactive exec (workspace-write sandbox).",
        color="#10a37f",
        models=["gpt-5-codex", "gpt-5", "o4-mini"],
        default_model="gpt-5-codex",
        fast_model="o4-mini",
        model_flag="--model",
        api_key_env="OPENAI_API_KEY",
    ),
    AgentSpec(
        name="gemini",
        label="Gemini CLI",
        bin="gemini",
        argv=["gemini", "-y", "-p", "{prompt}"],
        safe_argv=["gemini", "-p", "{prompt}"],
        description="Google Gemini CLI in YOLO/auto mode.",
        color="#4285f4",
        models=["gemini-2.5-pro", "gemini-2.5-flash"],
        default_model="gemini-2.5-pro",
        fast_model="gemini-2.5-flash",
        model_flag="-m",
        api_key_env="GEMINI_API_KEY",
    ),
    AgentSpec(
        name="glm",
        label="GLM / Z.ai",
        bin="claude",
        argv=["claude", "-p", "{prompt}", "--dangerously-skip-permissions"],
        resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}",
                     "--dangerously-skip-permissions"],
        safe_argv=["claude", "-p", "{prompt}"],
        safe_resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}"],
        description="Z.ai GLM coding plan via Claude Code (set ANTHROPIC_BASE_URL).",
        env={"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"},
        color="#2563eb",
        models=["glm-4.6", "glm-4.5", "glm-4.5-air"],
        default_model="glm-4.6",
        fast_model="glm-4.5-air",
        api_key_env="ANTHROPIC_API_KEY",  # claude-compatible endpoint; key is your Z.ai key
    ),
    AgentSpec(
        name="kimi",
        label="Kimi / Moonshot",
        bin="claude",
        argv=["claude", "-p", "{prompt}", "--dangerously-skip-permissions"],
        resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}",
                     "--dangerously-skip-permissions"],
        safe_argv=["claude", "-p", "{prompt}"],
        safe_resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}"],
        description="Moonshot Kimi via Claude Code's Anthropic-compatible endpoint.",
        env={"ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic"},
        color="#1f8fff",
        models=["kimi-k2-0905-preview", "kimi-k2-turbo-preview", "kimi-k2-0711-preview"],
        default_model="kimi-k2-0905-preview",
        fast_model="kimi-k2-turbo-preview",
        api_key_env="ANTHROPIC_API_KEY",  # claude-compatible endpoint; key is your Moonshot key
    ),
    AgentSpec(
        name="opencode",
        label="OpenCode",
        bin="opencode",
        argv=["opencode", "run", "{prompt}"],
        description="OpenCode terminal agent, non-interactive run.",
        color="#f59e0b",
    ),
    AgentSpec(
        name="hermes",
        label="Hermes",
        bin="hermes",
        argv=["hermes", "-p", "{prompt}"],
        description="Hermes coding agent (best-effort; override argv in config if it differs).",
        color="#e879f9",
    ),
    AgentSpec(
        name="aider",
        label="Aider",
        bin="aider",
        argv=["aider", "--yes", "--no-auto-commits", "--message", "{prompt}"],
        safe_argv=["aider", "--no-auto-commits", "--message", "{prompt}"],
        description="Aider pair-programmer, single message mode.",
        color="#22c55e",
    ),
    AgentSpec(
        name="cursor-agent",
        label="Cursor Agent",
        bin="cursor-agent",
        argv=["cursor-agent", "-p", "{prompt}"],
        description="Cursor CLI agent in print mode.",
        color="#000000",
    ),
    AgentSpec(
        name="shell",
        label="Shell command",
        bin="bash",
        argv=["bash", "-lc", "{prompt}"],
        description="Run the prompt as a raw shell command. Always available.",
        color="#64748b",
    ),
]

BUILTIN_BY_NAME: Dict[str, AgentSpec] = {a.name: a for a in BUILTIN_AGENTS}


def builtin_names() -> List[str]:
    return [a.name for a in BUILTIN_AGENTS]


def spec_to_dict(a: AgentSpec) -> dict:
    return {
        "name": a.name,
        "label": a.label,
        "bin": a.bin,
        "description": a.description,
        "color": a.color,
        "models": a.models,
        "default_model": a.default_model,
        "api_key_env": a.api_key_env,
    }


def catalog() -> List[dict]:
    return [spec_to_dict(a) for a in BUILTIN_AGENTS]
