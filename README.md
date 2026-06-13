# Deckhand

**A Kanban board where every card is a task run by your local CLI coding agents.**

Trello, but the cards *do the work*. Drop a task on the board, pick an agent
(Claude Code, Codex, Gemini, GLM/Z.ai, Aider, OpenCode, Cursor — or any CLI you
define), and a background runner on your machine executes it and streams the logs
straight back to the card. Cards carry tags that pull live insight from other
spots (git status, changed files, test output).

```
┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐
│ Backlog │ → │ Queued  │ → │ Running │ → │ Review  │ → │  Done   │
└─────────┘   └─────────┘   └────┬────┘   └─────────┘   └─────────┘
                                 │ runner picks it up, runs `claude -p …`,
                                 │ streams stdout/stderr to the card live
                                 ▼
                        ╔════════════════════╗
                        ║  deckhand runner    ║  (pip-installed, background)
                        ║  detects: claude,   ║
                        ║  codex, gemini, …   ║
                        ╚════════════════════╝
```

## Quickstart

```bash
pip install -e .          # from this repo (or: pip install deckhand)
deckhand up               # starts the server + a local runner, opens the board
```

Then in the browser: hit **+ add task**, write a prompt, choose an agent, and
**Queue now**. Watch it run.

Prefer the pieces separate (e.g. runner on a different machine):

```bash
deckhand server                                   # on the host
deckhand runner --server http://HOST:8787 --name gpu-box   # on each worker
```

## How it works

- **Server** (`deckhand server`) — FastAPI + SQLite. Serves the board UI, a REST
  API, and two WebSocket channels: one for the live board, one for runners.
- **Runner** (`deckhand runner`) — connects over WebSocket, detects which agent
  CLIs are on your `PATH`, advertises them as capabilities, and executes assigned
  tasks, streaming output back line-by-line.
- **Board** — five default columns (Backlog → Queued → Running → Review → Done).
  Dropping a card into **Queued** dispatches it to an available runner. The card
  auto-advances to **Running**, then **Review** (on success) or shows **failed**.

## Agents

`deckhand agents` shows what's detected locally. Built-in catalog:

| agent | invocation |
|-------|------------|
| `claude` | `claude -p "<prompt>" --dangerously-skip-permissions` |
| `codex` | `codex exec --full-auto "<prompt>"` |
| `gemini` | `gemini -y -p "<prompt>"` |
| `glm` | Claude Code pointed at `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic` |
| `opencode` | `opencode run "<prompt>"` |
| `aider` | `aider --yes --message "<prompt>"` |
| `cursor-agent` | `cursor-agent -p "<prompt>"` |
| `shell` | `bash -lc "<prompt>"` (always available) |

**"…or whatever else is available in the CLI"** — override or add any agent:

```bash
deckhand config            # show current config
# edit ~/.deckhand/config.json -> "agent_overrides": { "claude": "claude -p {prompt} --model opus" }
```

A card set to `auto` runs on whatever the matched runner has (preferring a real
coding agent over the raw `shell` fallback).

## Tags & insights

Tags are colored labels. A tag can also be an **insight provider** (marked ◆)
that pulls live context onto any card wearing it:

- **Git status & diff** — branch, changed files, diffstat for the card's repo.
- **Recent files** — most recently modified files in the working directory.
- **Custom command** — runs a read-only command (e.g. `pytest -q`) and shows the tail.

## CLI reference

```
deckhand up         start server + local runner (best first run)
deckhand server     web server / API only
deckhand runner     background runner only  (--server, --name, --concurrency)
deckhand agents     show detected CLI agents
deckhand config     view/set server URL, token, runner name, enable/disable agents
deckhand open       open the board in a browser
```

Config lives in `~/.deckhand/config.json`; data in `~/.deckhand/deckhand.db`.
Set `DECKHAND_TOKEN` on the server to require a matching `--token` from runners.

## License

MIT
