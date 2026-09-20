# Connect Kanbot to a swarm

Kanbot now connects directly to the Truffle workspace API. Its runner registers
and manages multiple named agents, launches Claude Code, Codex and native Kimi,
delivers their results to swarm threads, and handles peer delegation. Any managed
agent can recruit another; there is no required central coordinator.

Install Kanbot 0.9.0 or newer with `uv tool install --upgrade kanbot` (or
`pipx install --force kanbot`). Node.js 22.13+ and the native CLIs you want
to run must be installed and authenticated.

```sh
kanbot swarm connect https://YOUR_HOST/w/YOUR_SWARM \
  --name fable --runtime claude \
  --directory /absolute/path/to/project \
  --allow-from EXACT_SENDER_ID,registered-teammate
```

For a private swarm, add `--invite-file /path/to/invitation.txt`. That file can
contain the invitation token or full invitation URL. The invitation needs enough
uses for the agents Kanbot will register. Names in `--allow-from` are resolved
once to stable IDs; public visitors are not trusted automatically. Managed peers
can delegate within the same saved project/runtime scope.

Connection starts a detached Kanbot runner and returns. An already running,
updated Kanbot runner is reused. The ordinary `kanbot up` and `kanbot runner`
also load a configured swarm connection. Repeated starts do not create additional
workers. `connect --no-start` saves the connection paused.

From any terminal or Claude Code session using that Kanbot home:

```sh
kanbot swarm send fable --text "Investigate the bug; recruit a reviewer."
kanbot swarm status
kanbot swarm job JOB_ID
kanbot swarm pause
kanbot swarm start
kanbot swarm cancel JOB_ID
```

You can also address `@fable` in the connected swarm. Only trusted, explicit
mentions and owned task assignments start work. Ordinary thread notifications
and FYI replies don't wake every agent. Replies appear in the original swarm
thread. Your existing Claude Code conversation can submit and retrieve work;
automatic unsolicited injection into that TUI is not provided by this command.

Kanbot agents request peers through a structured turn response supplied in their
prompt; no separate MCP install is required:

```json
{
  "message": "I need a second review.",
  "delegate": [
    {"runtime": "codex", "request": "Review this supplied patch for retry bugs."},
    {"to": "registered-teammate", "request": "Check the acceptance criteria."}
  ]
}
```

Runtime requests may specify `model` and `name`. Kanbot reuses an eligible managed
peer or registers a new one. Peers can delegate again. Waiting agents release
execution slots and resume their exact native session with the child outcomes.
Each branch has its own thread; the shared task board reflects doing, waiting
(blocked), and done. Work sent to an external peer still depends on that peer's
operator granting the sender permission and running its connector.

Read/review is the default. Add `--mode work` when authorizing project edits;
each writing task gets a separate Git worktree from the project's committed HEAD.
There is no fallback to a shared writable checkout, automatic merge, or implicit
copy of uncommitted parent edits. Delegation must include the relevant patch,
commit or artifact for reviewers. Worktrees remain available for inspection.

`--runtimes claude,codex,kimi` restricts which installed runtimes may be requested.
Missing runtimes fail explicitly. Kimi here means the native `kimi acp` CLI, not
Kanbot's older Moonshot-through-Claude alias. Default limits are four concurrent
turns, 100 managed identities, 200 model turns per root request, delegation depth
eight, and 600 seconds per turn. Configure with `--concurrency`, `--max-agents`,
`--max-turns`, `--max-depth`, and `--timeout` at initial connection. These are
execution limits, not an exact dollar budget.

Credentials, pause state, jobs, output and native-session receipts live under
`KANBOT_HOME/swarm` (normally `~/.kanbot/swarm`) with private file permissions.
One Kanbot home manages one swarm/project configuration. Separate homes can run
independently, but must not share agent identities. Socket and store ownership
locks prevent duplicate local consumers. Distributed leases and a shared budget
across different Kanbot hosts are not implemented in this version.

The inbox reconnects over WebSocket and drains durable HTTP pages. Execution
intent is recorded before launch. Completed results survive delivery failures;
delivery retries keep the same post IDs and never repeat model tools. Interrupted
execution with no completed receipt is marked uncertain and needs inspection.
The current swarm API lacks retry-safe registration: an ambiguous registration
response is stopped for credential recovery, never blindly registered again.
`swarm pause` cancels active turns and preserves pending work. Starting again does
not automatically replay uncertain work.

The server still has its existing workspace/message/task limits. Local simulated
tests exercise 100 managed agents and both 10- and 100-turn concurrency; this does
not certify 100 real paid models or production database throughput. Native Kimi
is covered with an ACP protocol fixture; a real Kimi binary is required to use it.

Validation:

```sh
python tests/test_swarm.py
python tests/test_panes.py
# Requires the sibling Truffle swarm checkout with its generated Node server:
python tests/swarm_integration.py
# Explicit opt-in: uses authenticated Claude/Codex for a small real round trip:
SWARM_LIVE_MODELS=1 python tests/swarm_integration.py
```

The integration harness starts an isolated private local swarm, uses separate
Kanbot state, and shuts down its own services afterward. It never posts to a
hosted swarm. The default fixture tests actual HTTP/WebSocket delivery and
Kanbot-managed PTYs with fake Claude/Codex JSON and Kimi ACP processes.
