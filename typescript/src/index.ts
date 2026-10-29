// Jaah Agent SDK v2: write an ACP agent; the library speaks the protocol and the `jaah.ai` extension.
//
// The library owns `initialize` (and the `jaah.ai` negotiation), `session/new`, `session/cancel` and
// `authenticate`. An author implements `prompt(turn)` and emits `session/update` through the `Turn` it
// is handed. Built on the official ACP SDK (`@agentclientprotocol/sdk`). README.md is the guide.

import { randomUUID } from "node:crypto";
import { Readable, Writable } from "node:stream";
import * as acp from "@agentclientprotocol/sdk";

import { Extension, ExtensionError, NAMESPACE, negotiate, type Json } from "./extension.js";

export * from "./extension.js";

export type StopReason = acp.StopReason;
export type SessionUpdate = acp.SessionNotification["update"];

const STOP_REASONS = new Set<string>(["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"]);

interface Session {
  id: string;
  cwd: string;
  meta: Json;
  extension: Extension;
  running?: AbortController;
}

/** One `session/prompt`: the user's content in, `session/update`s out. */
export class Turn {
  readonly sessionId: string;
  readonly cwd: string;
  readonly meta: Json;
  readonly jaah: Extension;

  constructor(
    private readonly conn: acp.AgentSideConnection,
    session: Session,
    readonly prompt: acp.ContentBlock[],
    /** Aborts when the client sends `session/cancel` for this turn. */
    readonly signal: AbortSignal,
  ) {
    this.sessionId = session.id;
    this.cwd = session.cwd;
    this.meta = session.meta;
    this.jaah = session.extension;
  }

  /** The prompt's text blocks, joined. */
  get text(): string {
    return this.prompt.map((b) => (b.type === "text" ? b.text : "")).join("");
  }

  /** Emit any `session/update`. */
  async update(update: SessionUpdate): Promise<void> {
    await this.conn.sessionUpdate({ sessionId: this.sessionId, update });
  }

  async say(text: string): Promise<void> {
    await this.update({ sessionUpdate: "agent_message_chunk", content: { type: "text", text } });
  }

  async think(text: string): Promise<void> {
    await this.update({ sessionUpdate: "agent_thought_chunk", content: { type: "text", text } });
  }

  /** Settle the task (`task.complete`). Make it the last thing the turn does. */
  complete(summary: string, terminalType: string, refs: Json = {}): Promise<Json | undefined> {
    return this.jaah.complete(summary, terminalType, refs);
  }

  fail(reason: string): Promise<Json | undefined> {
    return this.jaah.fail(reason);
  }
}

export interface AgentInfo {
  name: string;
  title?: string;
  version: string;
}

/** What an author writes: one prompt handler. Everything else in the protocol is the library's. */
export interface JaahAgent {
  info?: AgentInfo;
  /** Handle one prompt turn. Resolve a stop reason, or nothing for `end_turn`. */
  prompt(turn: Turn): Promise<StopReason | void>;
}

class Runtime implements acp.Agent {
  #methods: ReadonlySet<string> = new Set();
  readonly #sessions = new Map<string, Session>();

  constructor(
    private readonly agent: JaahAgent,
    private readonly conn: () => acp.AgentSideConnection,
  ) {}

  async initialize(params: acp.InitializeRequest): Promise<acp.InitializeResponse> {
    this.#methods = negotiate(params.clientCapabilities?._meta);
    return {
      protocolVersion: Math.min(params.protocolVersion, acp.PROTOCOL_VERSION),
      agentCapabilities: { loadSession: false },
      agentInfo: this.agent.info ?? { name: "jaah-agent", version: "0.0.0" },
      authMethods: [],
    };
  }

  async authenticate(): Promise<void> {}

  async newSession(params: acp.NewSessionRequest): Promise<acp.NewSessionResponse> {
    const session: Session = {
      id: randomUUID().replaceAll("-", ""),
      cwd: params.cwd,
      meta: ((params._meta as Json | undefined)?.[NAMESPACE] as Json | undefined) ?? {},
      extension: new Extension((m, p) => this.#send(session, m, p), this.#methods),
    };
    this.#sessions.set(session.id, session);
    return { sessionId: session.id };
  }

  async prompt(params: acp.PromptRequest): Promise<acp.PromptResponse> {
    const session = this.#sessions.get(params.sessionId);
    if (!session) throw acp.RequestError.invalidParams({ sessionId: params.sessionId, reason: "unknown session" });
    if (session.running) throw acp.RequestError.invalidRequest({ reason: "a prompt is already running" });
    const running = new AbortController();
    session.running = running;
    try {
      const reason = await this.agent.prompt(new Turn(this.conn(), session, params.prompt, running.signal));
      if (running.signal.aborted) return { stopReason: "cancelled" };
      const stopReason = reason ?? "end_turn";
      if (!STOP_REASONS.has(stopReason)) throw new Error(`prompt() resolved ${stopReason}; expected a stop reason`);
      return { stopReason };
    } catch (err) {
      if (running.signal.aborted) return { stopReason: "cancelled" };
      throw err;
    } finally {
      session.running = undefined;
    }
  }

  async cancel(params: acp.CancelNotification): Promise<void> {
    this.#sessions.get(params.sessionId)?.running?.abort();
  }

  async extMethod(method: string): Promise<Json> {
    throw acp.RequestError.methodNotFound(method);
  }

  async extNotification(): Promise<void> {}

  // A call made during a turn rejects when that turn is cancelled, like Python's task cancellation.
  async #send(session: Session, method: string, params: Json): Promise<Json> {
    const signal = session.running?.signal;
    let onAbort = () => {};
    const aborted = new Promise<never>((_, reject) => {
      onAbort = () => reject(new ExtensionError(method, undefined, "the turn was cancelled"));
      if (signal?.aborted) onAbort();
      else signal?.addEventListener("abort", onAbort, { once: true });
    });
    try {
      const result = await Promise.race([this.conn().extMethod(method, params), aborted]);
      if (result === null || typeof result !== "object" || Array.isArray(result)) {
        throw new ExtensionError(method, undefined, "the client answered a non-object");
      }
      return result;
    } catch (err) {
      if (err instanceof acp.RequestError) throw new ExtensionError(method, err.code, err.message, err.data);
      throw err;
    } finally {
      signal?.removeEventListener("abort", onAbort);
    }
  }

  close(): void {
    for (const session of this.#sessions.values()) session.extension.close();
  }
}

/** Serve `agent` over ACP on the given streams (stdin/stdout by default) until the client hangs up. */
export async function serve(
  agent: JaahAgent,
  output: WritableStream<Uint8Array> = Writable.toWeb(process.stdout) as WritableStream<Uint8Array>,
  input: ReadableStream<Uint8Array> = Readable.toWeb(process.stdin) as ReadableStream<Uint8Array>,
): Promise<void> {
  let runtime: Runtime | undefined;
  const conn = new acp.AgentSideConnection((c) => {
    runtime = new Runtime(agent, () => c);
    return runtime;
  }, acp.ndJsonStream(output, input));
  // The host ends a settled run with SIGTERM: remove credential files before going.
  const onTerm = () => {
    runtime?.close();
    process.exit(143);
  };
  process.once("SIGTERM", onTerm);
  try {
    await conn.closed;
  } finally {
    process.removeListener("SIGTERM", onTerm);
    runtime?.close();
  }
}

/** Entry point for an agent process: serve ACP on stdio. */
export function run(agent: JaahAgent): void {
  serve(agent).catch((err: unknown) => {
    console.error(err);
    process.exitCode = 1;
  });
}
