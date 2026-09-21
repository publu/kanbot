# KanBot

**Swarm connection:** `kanbot swarm connect` connects this runner directly to
Truffle, with named Claude/Codex/Kimi agents, peer delegation, parallel execution
and durable replies. It is an optional Truffle add-on: the plugin connects existing
agents, Kanbot manages local sessions, and the hosted platform holds conversations,
wiki and tasks. See [swarm setup and commands](docs/swarm.md) to install **Kanbot 0.9.1 or newer from PyPI**.

**One screen for every coding agent you run — live terminals you can type into from anywhere, exact working / blocked / idle state, and a task queue that drives them.**

**Run instantly:** `pipx install kanbot && kanbot` (or `uvx kanbot`) · **Site:** https://kanbot-gamma.vercel.app

You run a lot of terminal coding agents (Claude Code, Codex, Gemini, …). KanBot's
runner *owns a real terminal for each one*, so the agents keep running when you
close the tab or switch viewing devices, while the runner’s computer stays awake — and you pick them back up
exactly where they were, from the web board, your phone, or `kanbot attach`.

- **See state, not spinners.** Every agent is `working`, `blocked` (waiting on
  you), `idle`, or `done`. Claude Code reports it through lifecycle hooks —
  exact, no guessing; other CLIs are read off the screen. Blocked agents jump to
  the top of the board, flash the tab title, and ping you (macOS banner, or any
  command you configure — Telegram, ntfy, Slack).
- **Type into any agent from the board.** A real xterm in the browser plus
  one-tap keys (`⏎` `y` `n` `esc` `^C`) for answering permission prompts from a
  phone. Or `kanbot attach <id>` from any terminal — `Ctrl-]` detaches, the
  agent keeps going.
- **Agent-native API.** The CLI and the local socket API are the same surface
  your own scripts (or other agents) drive: `kanbot agent start claude "…"`,
  `kanbot agent wait <id> --until idle`, `kanbot agent read <id>`,
  `kanbot agent prompt <id> "now add tests"`.
- **Still a queue.** Cards, workflows, gates, and the 10-hour goal spree all run
  *inside* panes now — so unattended runs are watchable and interruptible live.
- **Multi-machine.** Runners on any box connect to one board; every pane shows
  which machine it lives on.

```
 ┌─ board (web) ─────────────────────────────────────────────────┐
 │ NEEDS YOU ◆ codex  "Allow command?"      ┌─ live terminal ─┐  │
 │ AGENTS    ● claude  fixing tests  gpu-box │ $ claude …      │  │
 │           ○ gemini  idle                  │ Do you want to… │  │
 │ QUEUE     · ship-feature (workflow)       │ ❯ 1. Yes        │  │
 │ PICK UP   ↻ yesterday's claude session    └── ⏎ y n esc ^C ─┘  │
 └────────────────────────────────────────────────────────────────┘
        ▲ ws                                      ▲ ws
 ╔════════════════════╗                 ╔════════════════════╗
 ║ runner @ laptop    ║                 ║ runner @ gpu-box   ║
 ║ pane ● claude (pty)║                 ║ pane ● claude (pty)║
 ║ pane ◆ codex  (pty)║  ~/.kanbot/     ║ hooks → state      ║
 ║ socket API + CLI   ║  runner.sock    ║ socket API + CLI   ║
 ╚════════════════════╝                 ╚════════════════════╝
```

## Quickstart

Easiest (isolated, sidesteps Homebrew's PEP 668 `externally-managed` error):

```bash
pipx install kanbot && kanbot up     # or zero-install:  uvx kanbot up
```

From source:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
kanbot up                             # server + local runner, board at :8787
```

> Don't use bare `pip install` on macOS Homebrew Python — it errors with
> `externally-managed-environment` (PEP 668). `pipx`/`uv` handle the env for you.

Bare `kanbot` opens **the terminal app** (below) and starts the server + runner
in the background if they aren't up. `kanbot up` runs them in the foreground
and opens the **web board** at http://127.0.0.1:8787 instead — same agents,
same panes, pick whichever screen you're in front of.

## The terminal app — a tmux for agents

```
 KANBOT  3 live          │ ❯ Reply with exactly the single word: pong.
                         │
 NEEDS YOU               │ ⏺ pong
 ◆ migrate to app router │
   codex · web · 3m      │ ❯ █
 AGENTS                  │
 ● fix the flaky test    │
   claude · api · 14m    │
 ○ write the runbook     │
   gemini · infra · 41m  │
 j/k move · Enter focus · n new · x kill · ? help · q quit              Ctrl-] sidebar
```

Left: every agent on this machine, blocked ones first. Right: the selected
agent's real terminal, live, with colours. Press **Enter** and you are typing
into the agent; **Ctrl-]** brings you back to the sidebar. **n** starts a new
agent (agent · prompt · cwd), **x** kills one, **q** quits — the agents keep
running, because the runner owns them, not the app. Open it again from any
terminal, or on another machine with `ssh box kanbot`.

Everything in the app is also a command, so scripts and other agents can do
what you do:

## Live terminals

The runner owns a PTY per agent. Panes outlive every client: close the browser,
`kanbot attach` from another terminal, or open the board from your phone — the
scrollback replays and you are back where it left off.

```bash
kanbot                                        # the terminal app
kanbot ps                                     # every agent on this machine, with state
kanbot agent start claude "fix the flaky test" --cwd ~/repo   # open a live Claude Code TUI
kanbot agent start codex --headless "run the suite" --cwd ~/repo
kanbot attach 3f2a                            # your terminal becomes that pane; Ctrl-] detaches
kanbot agent wait 3f2a --until idle           # block until it's ready for you (or blocked/done)
kanbot agent read 3f2a --lines 40             # the screen as plain text
kanbot agent prompt 3f2a "now add tests"      # type + Enter
kanbot agent keys 3f2a y Enter                # answer a permission prompt
kanbot agent keys 3f2a C-c                    # interrupt
kanbot agent kill 3f2a
```

**State** is one of `working` · `blocked` (needs a human) · `idle` · `done`.
Claude Code panes (and GLM/Kimi via Claude Code) report it through lifecycle
hooks injected with `--settings`, so it is exact: `UserPromptSubmit`/`PreToolUse`
→ working, `Notification` → blocked, `Stop` → idle. Every other CLI is classified
from the last lines of its screen (a `(y/n)`, `Allow command?`, or a `❯ 1. Yes`
menu means blocked; a bare prompt means idle). `kanbot agent get <id>` shows
which source is speaking (`state_source`).

**Notifications.** When an agent turns blocked (or a live TUI finishes) the runner
runs `notify_command` from `~/.kanbot/config.json` with `KANBOT_TITLE`,
`KANBOT_BODY`, `KANBOT_STATE`, `KANBOT_PANE_ID`, `KANBOT_AGENT` in its env. Unset,
macOS gets a banner. Point it anywhere:

```json
{ "notify_command": "curl -s -d \"$KANBOT_TITLE — $KANBOT_BODY\" ntfy.sh/my-agents" }
```

The board links straight to a pane: `http://host:8787/#pane=<id>` — put that in
your notification and a tap opens the agent that needs you.

**Socket API.** `~/.kanbot/runner.sock`, newline-delimited JSON, the same methods
the CLI uses: `ping` · `agent.list` · `agent.get` · `agent.read` · `agent.start` ·
`agent.prompt` · `agent.send_keys` · `agent.wait` · `agent.kill` ·
`events.subscribe` (a stream of state changes) · `pane.attach` (raw bytes).
Over HTTP the board exposes the same for every runner: `GET /api/panes`,
`POST /api/panes/start`, `GET /api/panes/{id}/read`, `POST /api/panes/{id}/input`
(`{"text":"…"}` or `{"keys":["y","Enter"]}`), `POST /api/panes/{id}/kill`.

**Headless vs live.** Cards default to a live TUI. Untick **Live terminal** for
the agent's print/exec mode (`claude -p`, `codex exec`): the run still happens in
a pane you can watch, and its stdout lines feed the card, workflows, and gates
as before. Plan-before-execute is headless only.

> Panes die with the runner, like tmux panes die with the tmux server. Keep the
> runner alive as a service (launchd/systemd), or run `kanbot up` in a tmux.

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

- **Suggest** — open **⛓ Automations → ✨ Suggest from my sessions** and Deckhand
  reads every Claude/Codex session you've run and proposes automations (per
  project + cross-cutting patterns), each with a rationale and its source
  sessions.
- **Refine (distillation)** — the suggestions start as raw drafts (your actual
  transcript turns). Hit **✨ Refine** and a connected agent (claude, codex, glm,
  gemini — whichever your runner advertises) rewrites them into clean,
  *generalized*, short guided step-prompts that work in fresh context — adding
  test/verify loops where you were iterating. This is the difference between
  parroting your chat and a real reusable automation. (Needs a reasoning agent
  on a connected runner; the same **✨ Distill** button lives in the builder.)
- **Templates** — a built-in starter library (*Ship a feature*, *Harden until
  green*, *Deep refactor*); pick one and tweak.
- **Extract** — turn a single Claude/Codex session into a draft workflow: open
  **⟳ sessions → ⛓ workflow**.
- **Export / import** — every workflow exports to portable JSON you can share,
  version, or paste into another board.
- **Clone & edit** — duplicate and adjust in the builder.

API: `GET /api/workflow-templates`, `…/workflows` (CRUD), `…/workflows/import`,
`…/workflows/extract`, `…/workflows/{id}/export`, `…/workflows/{id}/run`. The full
spec is under **`</> API`** in the app.

## Goal Spree — hand it one goal, walk away for 10+ hours

Playbooks are first-class now (the **▤ playbooks** button). The headline is the
**⚡ Set off a goal spree**: give it one big goal, a repo, a budget (hours) and an
optional verify command, and it runs unattended — engineered so a *skittish* agent
that wants to quit after five minutes can't end the run.

How it beats the early-stop problem:

- **It splits the goal.** A first pass writes `PROGRESS.md` — a checklist of small,
  independently-verifiable tasks plus a concrete `## DONE WHEN`. The agent never
  faces "do the whole thing", only "do the next box".
- **One task per fresh context.** Each iteration does exactly one unchecked item,
  verifies it, checks it off, and commits — then ends. The next iteration re-reads
  `PROGRESS.md`, so a 10-hour run survives context resets.
- **It can't talk its way out.** The loop's stop condition is a real shell
  predicate — *no unchecked boxes **and** your verify command passes*. When the
  agent says "good stopping point", the runner re-checks and relaunches with fresh
  context until it's actually done. The prompt also explicitly rejects "the rest is
  straightforward" / "I'll let you take it from here".
- **Bounded + safe to leave.** A wall-clock budget and an iteration cap bound the
  run; between iterations the runner **auto-commits** any work the agent left
  uncommitted (so nothing is ever lost), and **stops if there's no progress** for
  several passes instead of spinning.
- **Resumable.** If a run hits its budget or is interrupted, **⚡ Continue spree**
  picks up against the existing `PROGRESS.md` exactly where it left off.

You can seed a spree with a **saved playbook** (its method is folded into the plan)
and apply a **prompt mode** (e.g. *lean*) to every step.

### Creating playbooks (not just from sessions)

- **✎ Draft from an idea** — describe what the playbook should do (optionally point
  it at a repo to ground in) and an agent writes the 3–6 steps for you, live.
- **✨ Build from my sessions** — distill your real Claude/Codex transcripts into
  reusable playbooks (results are cached in the local DB, never re-generated).
- **＋ New (manual)** — author steps by hand, with a **✨ Distill** button to clean
  them up.
- **◇ Template / ⬇ Import** — starter library and portable JSON.

API: `POST /api/boards/{id}/spree`, `…/workflows/draft`, `…/workflows/build`,
`GET /api/spree/progress?cwd=…`.

## Self-improving (Training)

A workflow's job is to **distill a whole conversation into a procedure that
reproduces the outcome with far less prompting** — cards show the estimated
"↓ ~Nx less prompting." Deckhand improves at this on its own, locally, using your
agents:

- **Grounded distillation.** Extraction runs an agent *read-only inside the
  session's real repo*, so workflows are verified against actual code, not
  hallucinated from chat.
- **Evaluate.** After Analyze, **⚖ Evaluate** grades a workflow against the
  session's real outcome (git diff/commits) — fidelity, reusability, baked-in
  takeaways — and writes a grounded critique.
- **Exemplar library.** Anything scoring ≥75 is banked as a **proven exemplar**
  and injected as few-shot into future distillation — the system bootstraps off
  its own wins. See **⛓ Automations → 🧠 Training**.
- **Improvement pass.** "🧠 Run an improvement pass" distills + judges your
  richest sessions and banks the winners (cost-capped; real agent runs).
- **Sandbox replay (opt-in).** A deeper reward: replay a workflow in a throwaway
  `git worktree` at the session's pre-state and judge the diff it *produces*
  against the real one (`sandbox` flag on the eval/improve API).

API: `…/workflows/from-session` (deep extract), `…/workflows/eval`,
`…/workflows/improve`, `…/exemplars`.

## Tags & insights

Tags are colored labels; a tag can also be an **insight provider** (◆) that pulls
live context onto any card: **git** (branch/diff), **files** (recent changes), or
a **custom command** (e.g. `pytest -q`).

## CLI

```
kanbot            the terminal app (starts the stack in the background if needed)
kanbot up         server + local runner in the foreground, opens the web board
kanbot server     board / API only
kanbot runner     background runner only  (--server, --name, --concurrency)
kanbot ps         live agents on this machine (state · agent · cwd · title)
kanbot attach ID  your terminal becomes that agent's pane (Ctrl-] detaches)
kanbot agent …    start / prompt / keys / read / wait / kill / get / rm
kanbot agents     detected agents + active session trackers
kanbot config     server URL, token, runner name, enable/disable agents
kanbot open       open the board
kanbot review     multi-agent code review of local changes (--gate for chains)
```

Config: `~/.kanbot/config.json` · data: `~/.kanbot/kanbot.db` · socket: `~/.kanbot/runner.sock`.
Set `KANBOT_TOKEN` on the server to require a matching `--token` from runners.

## License

MIT
