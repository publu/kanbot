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


# Non-interactive / headless invocations for each known coding CLI.
BUILTIN_AGENTS: List[AgentSpec] = [
    AgentSpec(
        name="claude",
        label="Claude Code",
        bin="claude",
        argv=["claude", "-p", "{prompt}", "--dangerously-skip-permissions"],
        resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}",
                     "--dangerously-skip-permissions"],
        description="Anthropic Claude Code in headless print mode.",
        color="#d97757",
    ),
    AgentSpec(
        name="codex",
        label="Codex",
        bin="codex",
        argv=["codex", "exec", "--sandbox", "workspace-write",
              "--skip-git-repo-check", "{prompt}"],
        resume_argv=["codex", "exec", "resume", "--skip-git-repo-check",
                     "{session_id}", "{prompt}"],
        description="OpenAI Codex CLI, non-interactive exec (workspace-write sandbox).",
        color="#10a37f",
    ),
    AgentSpec(
        name="gemini",
        label="Gemini CLI",
        bin="gemini",
        argv=["gemini", "-y", "-p", "{prompt}"],
        description="Google Gemini CLI in YOLO/auto mode.",
        color="#4285f4",
    ),
    AgentSpec(
        name="glm",
        label="GLM / Z.ai",
        bin="claude",
        argv=["claude", "-p", "{prompt}", "--dangerously-skip-permissions"],
        resume_argv=["claude", "--resume", "{session_id}", "-p", "{prompt}",
                     "--dangerously-skip-permissions"],
        description="Z.ai GLM coding plan via Claude Code (set ANTHROPIC_BASE_URL).",
        env={"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"},
        color="#2563eb",
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
    }


def catalog() -> List[dict]:
    return [spec_to_dict(a) for a in BUILTIN_AGENTS]
