import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

// Each invocation owns a dedicated session. Never use --last or attach to a TUI.
export function runtimeCommand(
  runtime,
  { session, mode = "read", model, stateDirectory, readable } = {},
) {
  if (runtime === "codex")
    return [
      "codex",
      [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-c",
        `sandbox_mode="${mode === "work" ? "workspace-write" : "read-only"}"`,
        ...(model ? ["-m", model] : []),
        ...(session ? ["resume", session] : []),
        "-",
      ],
    ];
  if (runtime === "claude")
    return [
      "claude",
      [
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        mode === "work" ? "acceptEdits" : "dontAsk",
        ...(mode === "read" ? ["--tools", "Read,Grep,Glob"] : []),
        ...(mode === "work"
          ? ["--allowedTools", "WebFetch", "WebSearch", ...(readable ? [`Read(/${readable}/**)`] : [])]
          : []),
        ...(model ? ["--model", model] : []),
        ...(session ? ["--resume", session] : []),
      ],
    ];
  if (runtime === "kimi") return ["kimi", ["acp"]];
  if (runtime === "hermes")
    return [process.execPath, [fileURLToPath(new URL("./hermes-runtime.mjs", import.meta.url)), "--mode", mode, ...(stateDirectory ? ["--state", stateDirectory] : []), ...(readable ? ["--readable", readable] : [])]];
  throw Error("Choose --runtime kimi, codex, claude, or hermes.");
}

// Set by a Claude Code session for its own children. Provider and login settings
// (CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_OAUTH_TOKEN, ...) are the operator's and stay.
const sessionOnly =
  /^CLAUDE_(PID|EFFORT|CODE_(CHILD_SESSION|SESSION_\w+|MESSAGING_\w+|ENTRYPOINT|EXECPATH|SSE_PORT))$/;

export async function runRuntime(options) {
  const {
    runtime,
    directory,
    prompt,
    signal,
    onSession = async () => {},
  } = options;
  if (signal?.aborted) throw Error("Runtime interrupted before starting.");
  const [command, args] = runtimeCommand(runtime, options);
  const env = { ...process.env, BOTSPACE_CONNECTOR: "1" };
  // Claude disallows accidental nesting; this is a separate connector-owned session.
  delete env.CLAUDECODE;
  // A listener started inside a Claude session must not pass that session's identity to its turns.
  for (const key of Object.keys(env)) if (sessionOnly.test(key)) delete env[key];
  const child = spawn(command, args, {
    cwd: directory,
    env,
    stdio: ["pipe", "pipe", "pipe"],
    detached: process.platform !== "win32",
  });
  const kill = () => {
    try {
      process.platform === "win32"
        ? child.kill("SIGTERM")
        : process.kill(-child.pid, "SIGTERM");
    } catch {}
    const timer = setTimeout(() => {
      try {
        process.platform === "win32"
          ? child.kill("SIGKILL")
          : process.kill(-child.pid, "SIGKILL");
      } catch {}
    }, 2000);
    timer.unref();
  };
  signal?.addEventListener("abort", kill, { once: true });
  if (signal?.aborted) kill();
  let runtimeFailed = false;
  let stderr = "",
    bytes = 0,
    output = "",
    session = options.session,
    chain = Promise.resolve();
  child.stderr.on("data", (chunk) => {
    options.onDiagnostic?.(chunk.toString());
    stderr = (stderr + chunk).slice(-4000);
  });
  child.stdin.on("error", () => {});
  const exited = new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code) => resolve(code));
  });
  // Attach now so a missing executable cannot create an unhandled rejection.
  exited.catch(() => {});
  const lines = createInterface({ input: child.stdout });
  try {
    if (runtime === "kimi" || runtime === "hermes") {
      let next = 1,
        collecting = false;
      const pending = new Map();
      const send = (value) => child.stdin.write(JSON.stringify(value) + "\n");
      const rpc = (method, params) =>
        new Promise((resolve, reject) => {
          const id = next++;
          pending.set(id, { resolve, reject });
          send({ jsonrpc: "2.0", id, method, params });
        });
      const fail = (error) => {
        for (const p of pending.values()) p.reject(error);
        pending.clear();
      };
      exited.then(
        () => fail(Error(runtime + " ACP stopped before completing the request.")),
        fail,
      );
      lines.on("line", (line) => {
        bytes += line.length;
        if (bytes > 8_000_000) {
          kill();
          return;
        }
        let event;
        try {
          event = JSON.parse(line);
        } catch {
          return;
        }
        if (event.method === "session/update")
          options.onActivity?.(event.params?.update?.sessionUpdate);
        if (event.method === "session/request_permission") {
          // Read mode never approves writes or shell execution; work mode is an explicit operator choice.
          const allowed =
            options.mode === "work" ||
            ["read", "search"].includes(event.params?.toolCall?.kind);
          const option = event.params?.options?.find(
            (o) => o.kind === (allowed ? "allow_once" : "reject_once"),
          );
          send({
            jsonrpc: "2.0",
            id: event.id,
            result: {
              outcome: option
                ? { outcome: "selected", optionId: option.optionId }
                : { outcome: "cancelled" },
            },
          });
        } else if (event.method && event.id !== undefined) {
          send({
            jsonrpc: "2.0",
            id: event.id,
            error: { code: -32601, message: "Client method unavailable" },
          });
        } else if (
          event.method === "session/update" &&
          collecting &&
          event.params?.update?.sessionUpdate === "agent_message_chunk"
        ) {
          const c = event.params.update.content;
          if (c?.type === "text") output += c.text;
        } else if (pending.has(event.id)) {
          const p = pending.get(event.id);
          pending.delete(event.id);
          event.error
            ? p.reject(Error(runtime + " ACP request failed. Check the runtime login and project scope."))
            : p.resolve(event.result);
        }
      });
      options.onActivity?.("initializing");
      await rpc("initialize", {
        protocolVersion: 1,
        clientCapabilities: {},
        clientInfo: { name: "botspace", version: "0.6.0" },
      });
      options.onActivity?.("opening-session");
      const opened = await rpc(session ? "session/load" : "session/new", {
        cwd: directory,
        mcpServers: [],
        ...(session ? { sessionId: session } : {}),
      });
      session ||= opened.sessionId;
      if (!session) throw Error(runtime + " did not return a session ID.");
      await onSession(session);
      if (options.model)
        await rpc("session/set_model", {
          sessionId: session,
          modelId: options.model,
        });
      // Use the approval-capable mode, not Kimi print mode's implicit auto approval.
      const modes = opened.modes?.availableModes || [];
      const manual =
        (options.mode === "read" ? modes.find((m) => m.id === "plan") : null) ||
        modes.find((m) => /^(default|manual|ask|normal)$/.test(m.id));
      if (manual)
        await rpc("session/set_mode", {
          sessionId: session,
          modeId: manual.id,
        });
      collecting = true;
      options.onActivity?.("prompting");
      const result = await rpc("session/prompt", {
        sessionId: session,
        prompt: [{ type: "text", text: prompt }],
      });
      if (result.stopReason !== "end_turn")
        throw Error(runtime + " stopped: " + result.stopReason);
      child.stdin.end();
      kill();
    } else {
      lines.on("line", (line) => {
        bytes += line.length;
        if (bytes > 8_000_000) {
          kill();
          return;
        }
        let event;
        try {
          event = JSON.parse(line);
        } catch {
          return;
        }
        if (runtime === "codex") {
          if (event.type === "thread.started") {
            session = event.thread_id;
            chain = chain.then(() => onSession(session));
          }
          if (
            event.type === "item.completed" &&
            event.item?.type === "agent_message"
          )
            output = event.item.text;
          if (event.type === "turn.failed" || event.type === "error") {
            stderr = "Runtime turn failed";
            runtimeFailed = true;
          }
        } else if (event.type === "result") {
          if (event.is_error) {
            runtimeFailed = true;
            stderr = "Claude did not complete the turn: " + event.subtype;
            output = "";
          } else output = event.result || "";
          session = event.session_id;
          if (session) chain = chain.then(() => onSession(session));
        }
      });
      child.stdin.end(prompt);
      const code = await exited;
      await chain;
      if (code !== 0)
        throw Error(
          `${runtime} exited (${code}). Check runtime login and local connector logs. ${stderr.slice(-300)}`.trim(),
        );
    }
    if (signal?.aborted) throw Error("Runtime interrupted or timed out.");
    if (bytes > 8_000_000) throw Error("Runtime output limit exceeded.");
    if (runtimeFailed) throw Error(`${runtime} reported a failed turn.`);
    if (!output.trim())
      throw Error(
        `${runtime} returned no completed answer. ${stderr.startsWith("Claude did") ? stderr : "Check its login and permissions."}`,
      );
    return { text: output.trim(), session };
  } finally {
    signal?.removeEventListener("abort", kill);
    lines.close();
    kill();
  }
}
