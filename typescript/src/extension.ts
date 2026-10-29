// The agent's side of the `jaah.ai` ACP extension (spec/jaah-extension.json).
//
// A verb `a.b_c` is the ACP method `_jaah.ai/a/b_c`. The client advertises which methods it serves in
// `initialize.clientCapabilities._meta["jaah.ai"] = {"version": 1, "methods": [...]}`; a method it did not
// advertise is never sent. A session settles (`task.complete` / `task.fail`) once: a settle the client
// refused or could not deliver may be sent again, one the client accepted may not.

import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const NAMESPACE = "jaah.ai";
export const VERSION = 1;
export const METHOD_PREFIX = `_${NAMESPACE}/`;
const SETTLE_VERBS = new Set(["task.complete", "task.fail"]);

export type Json = Record<string, unknown>;
export type Send = (method: string, params: Json) => Promise<Json>;

/** The client did not advertise this `jaah.ai` method (a plain ACP client advertises none). */
export class ExtensionUnavailable extends Error {
  override name = "ExtensionUnavailable";
}

/** The client answered a `jaah.ai` request with a JSON-RPC error. */
export class ExtensionError extends Error {
  override name = "ExtensionError";
  constructor(
    readonly method: string,
    readonly code: number | undefined,
    message: string,
    readonly data?: unknown,
  ) {
    super(`${method} refused (${code}): ${message}`);
  }
}

/** This session already settled, or a settle is in flight. */
export class AlreadySettled extends Error {
  override name = "AlreadySettled";
}

/** `credential.get` -> `_jaah.ai/credential/get`. */
export function methodFor(verb: string): string {
  const [noun, name, ...rest] = verb.split(".");
  if (!noun || !name || rest.length > 0 || verb.includes("/")) {
    throw new Error(`not a jaah.ai verb: ${JSON.stringify(verb)} (expected \`noun.verb\`)`);
  }
  return `${METHOD_PREFIX}${noun}/${name}`;
}

/** `_jaah.ai/credential/get` -> `credential.get`. */
export function verbFor(method: string): string {
  if (!method.startsWith(METHOD_PREFIX)) throw new Error(`not a jaah.ai method: ${method}`);
  return method.slice(METHOD_PREFIX.length).replace("/", ".");
}

/** The methods a client advertised, or none when it offers no compatible `jaah.ai` capability. */
export function negotiate(clientMeta: unknown): ReadonlySet<string> {
  const offer = (clientMeta as Json | null | undefined)?.[NAMESPACE] as Json | undefined;
  if (!offer || offer.version !== VERSION || !Array.isArray(offer.methods)) return new Set();
  return new Set(
    offer.methods.filter((m): m is string => typeof m === "string" && m.startsWith(METHOD_PREFIX)),
  );
}

export interface AskOptions {
  options?: unknown[];
  header?: string;
  multiSelect?: boolean;
}

/** Typed calls over the methods this session's client serves. */
export class Extension {
  #settled = false;
  #settling = false;
  #credentialDir: string | undefined;

  constructor(
    private readonly send: Send,
    private readonly methods: ReadonlySet<string>,
  ) {}

  /** True when the client serves the `jaah.ai` extension at all. */
  get available(): boolean {
    return this.methods.size > 0;
  }

  get settled(): boolean {
    return this.#settled;
  }

  supports(verb: string): boolean {
    return this.methods.has(methodFor(verb));
  }

  /** Send any advertised verb with its params (spec/jaah-extension.json has each schema). */
  async call(verb: string, params: Json = {}): Promise<Json> {
    const method = methodFor(verb);
    if (!this.methods.has(method)) throw new ExtensionUnavailable(`the client does not serve ${method}`);
    if (!SETTLE_VERBS.has(verb)) return this.send(method, { ...params });
    if (this.#settled || this.#settling) throw new AlreadySettled("this session already settled; settle once, last");
    this.#settling = true;
    let result: Json;
    try {
      this.close(); // the run may end the moment the settle lands: no credential file outlives it
      result = await this.send(method, { ...params });
    } finally {
      this.#settling = false;
    }
    // A refusal threw above and left the session unsettled; so does an answer that moved nothing.
    this.#settled = result.settled !== false;
    return result;
  }

  /** `task.complete`. Resolves undefined under a plain ACP client, which has no task to settle. */
  async complete(summary: string, terminalType: string, refs: Json = {}): Promise<Json | undefined> {
    if (!this.supports("task.complete")) return undefined;
    return this.call("task.complete", { ...refs, summary, terminal_type: terminalType });
  }

  /** `task.fail`. Resolves undefined under a plain ACP client. */
  async fail(reason: string): Promise<Json | undefined> {
    if (!this.supports("task.fail")) return undefined;
    return this.call("task.fail", { reason });
  }

  /** `input.request`: put a question to a human; the client answers when they do. */
  async ask(question: string, opts: AskOptions = {}): Promise<Json> {
    const params: Json = { question };
    if (opts.options !== undefined) params.options = opts.options;
    if (opts.header !== undefined) params.header = opts.header;
    if (opts.multiSelect !== undefined) params.multi_select = opts.multiSelect;
    return this.call("input.request", params);
  }

  /** `credential.list`: names, types and descriptions — never a secret. */
  async credentials(): Promise<Json> {
    return this.call("credential.list");
  }

  /** `credential.get`: the credential's configuration, secrets included. Never log it. */
  async credential(name: string): Promise<Json> {
    return this.call("credential.get", { name });
  }

  /**
   * `credential.get`, written 0600 into a private 0700 directory; resolves the file's path.
   * This is how a key reaches a tool: as a config file it reads, never an environment variable.
   */
  async credentialFile(name: string): Promise<string> {
    const value = await this.credential(name);
    this.#credentialDir ??= mkdtempSync(join(tmpdir(), "jaah-credentials-"));
    const safe = name.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^\.+|\.+$/g, "") || "credential";
    const digest = createHash("sha256").update(name).digest("hex").slice(0, 8); // `a/b` and `a_b` stay two files
    const path = join(this.#credentialDir, `${safe}-${digest}.json`);
    writeFileSync(path, JSON.stringify(value), { mode: 0o600 });
    return path;
  }

  /** Remove every credential file this session wrote. */
  close(): void {
    if (this.#credentialDir) rmSync(this.#credentialDir, { recursive: true, force: true });
    this.#credentialDir = undefined;
  }
}
