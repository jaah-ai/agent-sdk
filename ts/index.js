// Jaah Agent SDK for TypeScript / JavaScript — the same API as the Python `jaah_agent`, in camelCase.
//
//   import { connect } from "@jaah/agent-sdk";
//   const agent = await connect();
//   const ctx = await agent.task();
//   ...
//   await agent.settle("Did the thing", "no_pr", { noPrReason: "no_code_needed", noPrEvidence: "..." });
//
// Plain ESM, no dependencies, no build step (Node >= 22.18). The node links this package into the
// agent's node_modules; never add it to your own dependencies.
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

import { JaahError, JaahRefused, JaahUnavailable, NotARunner } from "./lib/errors.js";
import { EventsFile, usageLine } from "./lib/events.js";
import { Masker } from "./lib/mask.js";
import { Wire } from "./lib/wire.js";

export { JaahError, JaahRefused, JaahUnavailable, NotARunner };

export const LOG_LEVELS = Object.freeze(["debug", "info", "warning", "error"]);
const MAX_QUESTIONS = 4;
const MAX_NOTIFY_CHARS = 2000;

/**
 * Attach to the node daemon this process was launched under, and prove it knows our task.
 * Rejects with NotARunner when there is no daemon socket in the environment or the daemon cannot
 * identify this process (it left the runner's process tree).
 */
export async function connect({ socketPath, eventsPath } = {}) {
  const path = socketPath || process.env.JAAH_AGENT_SOCKET;
  if (!path) {
    throw new NotARunner("JAAH_AGENT_SOCKET is not set: this process was not launched by a Jaah node");
  }
  const agent = new Agent(new Wire(path), new EventsFile(eventsPath || process.env.JAAH_AGENT_EVENTS));
  await agent._wire.call("task.get"); // NotARunner surfaces here, before the agent does any work
  return agent;
}

export class Agent {
  constructor(wire, events, { sleep = defaultSleep, clock = () => performance.now() / 1000 } = {}) {
    this._wire = wire;
    this._events = events;
    this._mask = new Masker();
    this._events.masker = this._mask; // backstop: every events line is masked at the sink too
    this._sleep = sleep;
    this._clock = clock;
  }

  // ── context ───────────────────────────────────────────────────────────────────────────────────
  /** The task this agent is working: `agent.task` plus the node-local `worktree` and `inputs`. */
  async task() {
    const ctx = { ...((await this._wire.call("agent.task")) || {}) };
    ctx.worktree = process.env.JAAH_WORKTREE ?? null;
    ctx.inputs = readInputs(process.env.JAAH_AGENT_INPUTS);
    return ctx;
  }

  // ── observability (local events file, never the network) ──────────────────────────────────────
  log(message, level = "info", data = {}) {
    if (!LOG_LEVELS.includes(level)) {
      throw new RangeError(`level must be one of ${LOG_LEVELS.join(", ")}, got ${JSON.stringify(level)}`);
    }
    this._events.write({ type: "log", level, message: this._mask.mask(String(message)), data: this._mask.mask(data ?? {}) });
  }

  /** Record one model call's tokens; Jaah prices them on the session's Efficiency view. */
  reportUsage(model, inputTokens, outputTokens, cacheRead = 0, cacheWrite = 0) {
    this._events.write(usageLine(model, inputTokens, outputTokens, cacheRead, cacheWrite));
  }

  // ── humans ────────────────────────────────────────────────────────────────────────────────────
  /** Post an informational message to the task's channel (console + Slack). */
  async notify(message) {
    const text = this._mask.mask(String(message));
    if (text.length > MAX_NOTIFY_CHARS) {
      throw new RangeError(`notify message is ${text.length} characters; the limit is ${MAX_NOTIFY_CHARS}`);
    }
    const result = await this._wire.call("agent.notify", { message: text });
    this._sdk("notify", { length: text.length });
    return result;
  }

  /**
   * Ask the operator 1..4 questions and wait until EVERY round is answered or dismissed.
   * Resolves to one Answer per question, in order. A dismissed round is `status: "dismissed"` with no
   * selections — never an empty "answered". Rejects with a TimeoutError if `timeout` seconds pass first.
   */
  async ask(questions, { pollInterval = 5, timeout = null } = {}) {
    if (!(pollInterval > 0)) throw new RangeError(`pollInterval must be positive, got ${pollInterval}`);
    if (!Array.isArray(questions) || questions.length < 1 || questions.length > MAX_QUESTIONS) {
      throw new RangeError(`ask takes 1..${MAX_QUESTIONS} questions, got ${questions?.length ?? 0}`);
    }
    const wireQuestions = questions.map((q) => this._question(q));
    const ids = [...(((await this._wire.call("agent.ask", { questions: wireQuestions })) || {}).ids || [])];
    if (ids.length !== wireQuestions.length) {
      throw new JaahRefused("bad_reply", `asked ${wireQuestions.length} questions, got ${ids.length} ids`);
    }
    this._sdk("ask", { questions: ids.length, ids });
    const deadline = timeout == null ? null : this._clock() + timeout;
    for (;;) {
      let reply;
      try {
        reply = (await this._wire.call("agent.answers", { ids })) || {};
      } catch (err) {
        if (!(err instanceof JaahUnavailable)) throw err;
        reply = {}; // a daemon restart mid-wait is not an answer; keep waiting
      }
      if (reply.all_resolved) {
        const answers = toAnswers(ids, reply.rounds || []);
        this._sdk("answers", { ids, statuses: answers.map((a) => a.status) });
        return answers;
      }
      if (deadline !== null && this._clock() >= deadline) {
        const err = new Error(`questions ${JSON.stringify(ids)} were not all resolved within ${timeout} seconds`);
        err.name = "TimeoutError";
        throw err;
      }
      await this._sleep(deadline === null ? pollInterval : Math.max(0, Math.min(pollInterval, deadline - this._clock())));
    }
  }

  _question(q) {
    if (!q || typeof q !== "object" || !q.question) {
      throw new TypeError("each question needs a non-empty 'question'");
    }
    const options = (q.options || []).map((opt) => {
      const o = typeof opt === "string" ? { label: opt } : { ...opt };
      if (!o.label) throw new TypeError("each option needs a non-empty 'label'");
      const out = { label: o.label };
      if (o.description != null) out.description = o.description;
      return this._mask.mask(out);
    });
    const out = {
      question: this._mask.mask(String(q.question)),
      options,
      multi_select: Boolean(q.multiSelect ?? q.multi_select ?? false),
    };
    if (q.header) out.header = this._mask.mask(String(q.header));
    const recommended = q.recommendedIndex ?? q.recommended_index;
    if (recommended != null) out.recommended_index = Math.trunc(Number(recommended));
    return out;
  }

  // ── credentials ───────────────────────────────────────────────────────────────────────────────
  /** The roster: every credential this task may use — names, types and descriptions, no secrets. */
  async credentials() {
    return [...(((await this._wire.call("task.credential_list")) || {}).credentials || [])];
  }

  /**
   * One credential's config, secrets INCLUDED. Every secret value is masked from then on in all text
   * this SDK writes or sends. Never write it anywhere yourself.
   */
  async credential(name) {
    const result = await this._wire.call("task.credential_get", { name });
    this._mask.register(name, result);
    this._sdk("credential_get", { name });
    return result;
  }

  // ── documents and other tasks ─────────────────────────────────────────────────────────────────
  /** Upload a deliverable (`report` .html, `prd`/`architecture` .md, or any `file`). */
  async artifact(kind, path) {
    const result = await this._wire.call("task.artifact", { kind, path: resolve(path) });
    this._sdk("artifact", { kind });
    return result;
  }

  async readTask(taskId) {
    return this._wire.call("task.read", { peer_task_id: Math.trunc(Number(taskId)) });
  }

  /** Filters: state, persona, hasArtifacts, artifactKind, limit (snake_case accepted too). */
  async searchTasks(q, filters = {}) {
    return this._wire.call("task.search", this._mask.mask({ q, ...snakeKeys(filters) }));
  }

  // ── ending ────────────────────────────────────────────────────────────────────────────────────
  /** The ONE terminal call. Make it last: settling frees the slot and the process is ended soon after. */
  async settle(summary, terminalType, { prUrl, noPrReason, noPrEvidence, ...gate } = {}) {
    const args = {
      ...snakeKeys(gate),
      ...snakeKeys({ summary, terminal_type: terminalType, pr_url: prUrl, no_pr_reason: noPrReason, no_pr_evidence: noPrEvidence }),
    };
    // Every field is masked, the free-form gate fields (recon_map, ...) included.
    const result = await this._wire.call("task.settle", this._mask.mask(args));
    this._sdk("settle", { terminal_type: terminalType, settled: Boolean(result && result.settled) });
    return result;
  }

  /** Give up on the task: only for what no human could have cleared. Also terminal. */
  async fail(reason) {
    const args = { reason: this._mask.mask(reason) };
    if (process.env.CONTROL_RUNNER_SESSION_ID) args.session_id = process.env.CONTROL_RUNNER_SESSION_ID;
    const result = await this._wire.call("task.fail", args);
    this._sdk("fail", {});
    return result;
  }

  _sdk(event, detail) {
    this._events.write({ type: "sdk", event, detail });
  }
}

function defaultSleep(seconds) {
  return new Promise((done) => setTimeout(done, seconds * 1000));
}

function readInputs(path) {
  if (!path || !existsSync(path)) return [];
  const data = JSON.parse(readFileSync(path, "utf8"));
  return Array.isArray(data) ? data : [];
}

function toAnswers(ids, rounds) {
  const byId = new Map(rounds.map((r) => [r.id, r]));
  return ids.map((id) => {
    const r = byId.get(id);
    if (!r || (r.status !== "answered" && r.status !== "dismissed")) {
      throw new JaahRefused("bad_reply", `round ${id} is missing or unresolved in a resolved reply`);
    }
    if (r.status === "dismissed") return { status: "dismissed", selections: [], text: "" }; // carries no answer
    return { status: "answered", selections: (r.selections || []).map(String), text: String(r.text || "") };
  });
}

// camelCase → snake_case for top-level keys, dropping null/undefined (the wire speaks snake_case).
function snakeKeys(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj || {})) {
    if (v == null) continue;
    out[k.replace(/[A-Z]/g, (c) => "_" + c.toLowerCase())] = v;
  }
  return out;
}
