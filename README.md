# KanBot

**A visual control room for your coding-agent TUIs — and a Kanban board where every card is a task run by them.**

**Live demo:** https://getkanbot.vercel.app · **Install:** `pipx install kanbot && kanbot up`

You run a lot of terminal coding agents (Claude Code, Codex, …). KanBot gives you
one screen to *see what every session is doing*, pick any of them back up, and
drop new tasks that agents run for you — with live logs streamed straight to the
card.

Two things in one board:

1. **Track your TUIs.** A background runner watches each agent's local session
   store and surfaces every session as a card: the project, the latest message,
   how many turns, how long it's been brewing, and whether it's **working right
   now**. Sessions flow by recency — working → **Running**, just-finished →
   **Done**, older → **Backlog**.
2. **Run new tasks.** Drop a card, pick an agent, and the runner executes it and
   streams stdout/stderr to the card. Or drag any tracked session into **Running**
   to resume it (`claude --resume`, `codex exec resume`).

```
 Backlog            Running            Review      Done
 (stale sessions    (sessions          (your       (recently
  + new tasks)       working now        finished    finished
                     + running tasks)   tasks)       sessions)
        │  drag → Running, or "Run", queues for a runner
        ▼
   ╔══════════════════════════════╗
   ║  kanbot runner (background)   ║  detects claude · codex · gemini · glm · shell
   ║  watches ~/.claude, ~/.codex  ║  executes & resumes, streams logs back
   ╚══════════════════════════════╝
```

## Quickstart

Easiest (isolated, sidesteps Homebrew's PEP 668 `externally-managed` error):

```bash
pipx install kanbot && kanbot up     # or zero-install:  uvx kanbot up
```

From source (until it's on PyPI):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
kanbot up                             # server + local runner, board at :8787
```

> Don't use bare `pip install` on macOS Homebrew Python — it errors with
> `externally-managed-environment` (PEP 668). `pipx`/`uv` handle the env for you.

The board immediately fills with your recent Claude/Codex sessions. Click any one
to see its recent transcript in a terminal view and **resume** it; or hit
**+ add task** to give an agent fresh work.

Run the pieces separately (e.g. runner on another machine):

```bash
kanbot server                                   # the board / API
kanbot runner --server http://HOST:8787 --name gpu-box
```

## Tracking other agents (Hermes, OpenCode, your own…)

Claude Code and Codex are tracked out of the box. Any agent that logs
newline-delimited JSON transcripts can be added with **no code change** — point
KanBot at its store in `~/.kanbot/config.json`:

```json
{
  "discovery_sources": [
    {
      "name": "hermes",
      "label": "Hermes",
      "root": "~/.hermes/sessions",
      "pattern": "*.jsonl",
      "recursive": true,
      "fmt": "claude"
    }
  ]
}
```

- `fmt`: `"claude"` for flat records (`{type, message, cwd, timestamp}`) or
  `"codex"` for payload-nested records (`{payload: {role, content, cwd}}`).
- `kanbot agents` shows which trackers are active and where they read from.

## Run agents

`kanbot agents` lists the CLIs detected on this machine. Built-in catalog:

| agent | run | resume |
|-------|-----|--------|
| `claude` | `claude -p "<prompt>"` | `claude --resume <id> -p "<prompt>"` |
| `codex` | `codex exec --sandbox workspace-write "<prompt>"` | `codex exec resume <id> "<prompt>"` |
| `gemini` | `gemini -y -p "<prompt>"` | — |
| `glm` | Claude Code w/ `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic` | ✓ |
| `opencode`, `aider`, `cursor-agent`, `hermes`, `shell` | see `kanbot/agents.py` | — |

Override or add any agent's command in `~/.kanbot/config.json` →
`agent_overrides`. A card set to `auto` runs on whatever the matched runner has.

**Custom command per card.** Need to run *literally any* CLI for one task? Open a
card → **⚡ custom command** and write it yourself, e.g.
`claude -p "{prompt}" --model opus --add-dir /data`. It runs instead of the
agent's default; `{prompt}` and `{session_id}` expand. Blank = use the agent.

**Images.** Paste or drop an image onto any prompt box (composer or card). KanBot
uploads it and hands the agent a local path it can read; thumbnails are shown and
removable, and the card gets a 📎 badge.

> **Safety:** by default agents run with auto-approve flags so tasks run
> unattended (Claude `--dangerously-skip-permissions`, Codex `workspace-write`).
> For **safe mode** — agents run without those flags (Codex read-only, Claude
> without skip-permissions) — start with `kanbot up --safe` or set it persistently
> with `kanbot config --safe` (`--unsafe` to revert). The runner shows a 🔒 safe
> badge on the board when safe mode is on.

## Workflows — long autonomous runs

A single prompt is a sprint; a **workflow** is the marathon. A workflow is an
ordered chain of agent **steps** that runs as one card — a session per step,
auto-advancing on success — built to drive **1–5 hour** autonomous runs from
Claude/Codex. Open **⛓ Workflows** (or press `w`).

Each step has its own prompt, agent override, **Ralph loop** (`loop_max` /
`loop_until`), and two switches: **carry context** (inject the previous step's
output into this prompt) and **continue on fail**. Steps run with fresh context,
so durable, file-based handoff (`PLAN.md`, `NOTES.md` in the repo) is the pattern —
e.g. *Plan → Build until tests pass → Review → Report*.

The point of 0.4.0 is making workflows **easy to get**, not just run:

- **Templates** — a built-in starter library (*Ship a feature*, *Harden until
  green*, *Deep refactor*); pick one and tweak.
- **Extract** — turn a Claude/Codex session you already ran into a draft
  workflow: open **⟳ sessions → ⛓ workflow**, each human turn becomes a step.
- **Export / import** — every workflow exports to portable JSON you can share,
  version, or paste into another board.
- **Clone & edit** — duplicate and adjust in the builder.

API: `GET /api/workflow-templates`, `…/workflows` (CRUD), `…/workflows/import`,
`…/workflows/extract`, `…/workflows/{id}/export`, `…/workflows/{id}/run`. The full
spec is under **`</> API`** in the app.

## Tags & insights

Tags are colored labels; a tag can also be an **insight provider** (◆) that pulls
live context onto any card: **git** (branch/diff), **files** (recent changes), or
a **custom command** (e.g. `pytest -q`).

## CLI

```
kanbot up         server + local runner (best first run)
kanbot server     board / API only
kanbot runner     background runner only  (--server, --name, --concurrency)
kanbot agents     detected agents + active session trackers
kanbot config     server URL, token, runner name, enable/disable agents
kanbot open       open the board
```

Config: `~/.kanbot/config.json` · data: `~/.kanbot/kanbot.db`.
Set `KANBOT_TOKEN` on the server to require a matching `--token` from runners.

## License

MIT
