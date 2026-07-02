"""The review engine's LLM runtime, implemented over KanBot's CLI agents.

The engine phases (harnesses.py, merge_gate.py, polish.py) were written against a
tiny two-method interface:

    await router.app.ai(prompt, system=, schema=, response_format=)   # fast, structured
    await router.app.harness(prompt, schema=, cwd=)                    # agent w/ file tools

KanBot already drives headless coding-agent CLIs (`claude -p`, `codex exec`, …)
in a working directory — which is exactly what `.harness()` needs (the agent can
read the repo). So this module is the whole bridge: it spawns the configured CLI
agent, asks it to emit JSON matching a pydantic schema, and validates the reply.

`.ai()` and `.harness()` are the same mechanism here — a subprocess agent call.
The original split (cheap classifier vs. tool-using agent) maps onto one CLI
agent; the only difference is whether a `cwd` (repo) is handed over for file
access. # ponytail: one agent backs both; separate models per role only if cost matters.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from pydantic import BaseModel

from ..agents import BUILTIN_BY_NAME

# Bound how many agent subprocesses run at once. The engine fans out parallel
# reviewers/adversaries with asyncio.gather; without a cap that becomes a fork
# bomb of `claude` processes. # ponytail: global cap, per-phase caps if it matters.
_MAX_CONCURRENT = int(os.getenv("KANBOT_REVIEW_CONCURRENCY", "4"))
_sem = asyncio.Semaphore(_MAX_CONCURRENT)

# Agent timeout per call. Repo-reading harness calls can be slow.
_TIMEOUT = int(os.getenv("KANBOT_REVIEW_TIMEOUT", "600"))


def _provider_key_env(agent_name: str) -> dict[str, str]:
    """Configured API key for the review agent, from KanBot's config (best-effort)."""
    try:
        from ..config import Config
        return Config.load().key_env_for(agent_name)
    except Exception:  # noqa: BLE001
        return {}


def _agent_argv(prompt: str, fast: bool = False) -> tuple[list[str], dict[str, str]]:
    """Resolve the review agent's argv + env (with {prompt} substituted).

    Agent and models both come from KanBot's catalog (agents.py) — NOT hardcoded,
    NOT claude-only. KANBOT_REVIEW_AGENT picks any agent (claude, codex, gemini,
    glm/z.ai, kimi, …); the model is that agent's catalog default, with the cheap
    `fast_model` used for the classification gates (.ai) and the premium
    `default_model` for deep reviewers (.harness). Each is passed via the agent's
    own `model_flag` (`--model`, `-m`, …). Env overrides win:
        KANBOT_REVIEW_AGENT / KANBOT_REVIEW_MODEL / KANBOT_REVIEW_FAST_MODEL
    """
    name = os.getenv("KANBOT_REVIEW_AGENT", "claude")
    spec = BUILTIN_BY_NAME.get(name) or BUILTIN_BY_NAME["claude"]
    argv = [tok.replace("{prompt}", prompt) for tok in spec.argv]
    if fast:
        model = os.getenv("KANBOT_REVIEW_FAST_MODEL") or spec.fast_model or spec.default_model
    else:
        model = os.getenv("KANBOT_REVIEW_MODEL") or spec.default_model
    flag = spec.model_flag or "--model"
    if model and flag and flag not in argv:
        argv += [flag, model]
    env = dict(spec.env)
    env.update(_provider_key_env(spec.name))  # inject the configured provider API key
    return argv, env


def _schema_instruction(schema: type[BaseModel]) -> str:
    return (
        "\n\n---\nReturn your answer as a SINGLE JSON object that validates against "
        "this JSON Schema. Output ONLY the JSON (a ```json fence is fine); no prose "
        "before or after:\n"
        + json.dumps(schema.model_json_schema())
    )


def _extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of an agent's stdout. Tolerant of
    ```json fences and surrounding prose."""
    if not text:
        raise ValueError("empty output")

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)

    for cand in candidates:
        # Scan from the first opening bracket (whichever of { or [ comes first),
        # matching balanced braces while respecting string literals.
        starts = [(cand.find(c), c, close) for c, close in (("{", "}"), ("[", "]")) if cand.find(c) >= 0]
        for start, open_ch, close_ch in sorted(starts):
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(cand)):
                c = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == open_ch:
                    depth += 1
                elif c == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cand[start : i + 1])
                        except json.JSONDecodeError:
                            break
    raise ValueError("no JSON object found in agent output")


def _schema_defaults(schema: type[BaseModel]) -> dict[str, Any]:
    """Best-effort placeholder values for a schema's required fields, so an
    unparseable `.ai()` reply still yields a constructible instance (with the
    'not confident' / empty defaults that make callers fall back safely)."""
    out: dict[str, Any] = {}
    for name, field in schema.model_fields.items():
        ann = field.annotation
        if ann is bool:
            out[name] = False
        elif ann is int:
            out[name] = 0
        elif ann is float:
            out[name] = 0.0
        elif ann in (list, list[str]) or str(ann).startswith("list"):
            out[name] = []
        else:
            out[name] = ""
    return out


async def _run_agent(prompt: str, cwd: str | None, fast: bool = False) -> str:
    argv, spec_env = _agent_argv(prompt, fast=fast)
    env = os.environ.copy()
    env.update(spec_env)
    workdir = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    async with _sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"review agent binary not found: {argv[0]}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise RuntimeError("review agent timed out") from exc
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"review agent exited {proc.returncode}: {tail}")
    return out.decode("utf-8", "replace")


class _HarnessResult:
    """Matches what the engine reads off a harness call: `.parsed`, `.error_message`."""

    def __init__(self, parsed: BaseModel | None, text: str = "", error_message: str | None = None):
        self.parsed = parsed
        self.text = text
        self.error_message = error_message


class _AiText:
    """Schemaless `.ai()` return: engine reads `.text` / `.content` / str()."""

    def __init__(self, text: str):
        self.text = text
        self.content = text

    def __str__(self) -> str:
        return self.text


class App:
    """The `router.app` the engine talks to."""

    async def harness(self, prompt: str, schema: type[BaseModel] | None = None,
                      cwd: str | None = None, **_: Any) -> _HarnessResult:
        full = prompt + (_schema_instruction(schema) if schema else "")
        try:
            text = await _run_agent(full, cwd)
        except Exception as exc:  # noqa: BLE001
            return _HarnessResult(None, error_message=str(exc))
        if schema is None:
            return _HarnessResult(None, text=text)
        try:
            parsed = schema.model_validate(_extract_json(text))
            return _HarnessResult(parsed, text=text)
        except Exception as exc:  # noqa: BLE001
            return _HarnessResult(None, text=text, error_message=str(exc))

    async def ai(self, prompt: str, system: str | None = None,
                 schema: type[BaseModel] | None = None, **_: Any) -> Any:
        parts = []
        if system:
            parts.append(system)
        parts.append(prompt)
        full = "\n\n".join(parts) + (_schema_instruction(schema) if schema else "")
        # No cwd: `.ai()` is a context-free classification, no repo access needed.
        # fast=True → uses the cheaper KANBOT_REVIEW_FAST_MODEL when set.
        try:
            text = await _run_agent(full, None, fast=True)
        except Exception as exc:  # noqa: BLE001
            if schema is None:
                raise
            # Fall back to a safe default instance (e.g. confident=False).
            return schema.model_validate(_schema_defaults(schema))
        if schema is None:
            return _AiText(text)
        try:
            return schema.model_validate(_extract_json(text))
        except Exception:  # noqa: BLE001
            return schema.model_validate(_schema_defaults(schema))

    def note(self, *a: Any, **k: Any) -> None:
        # Engine progress hook. KanBot surfaces logs via the runner, not here.
        if a:
            print("[review]", *a, flush=True)


class _Router:
    def __init__(self) -> None:
        self.app = App()

    def reasoner(self, *a: Any, **k: Any):
        """No-op stand-in for agentfield's @router.reasoner() registration."""
        def deco(fn):
            return fn
        return deco


router = _Router()
