// One JSON line each way on the node daemon's socket — the twin of python/_wire.py.
//
//   -> {"call": "task.settle", "args": {...}}
//   <- {"ok": true, "result": ...}   or   {"ok": false, "error": "<reason>", "detail": "..."}
//
// The daemon identifies the calling task from the process ancestry, so a request carries no token and no
// task id. Retry rule: a request that certainly never reached the daemon (connect failed) is retried for
// every verb; any other unavailability is retried only for a read-only verb, because a write may have
// landed before the answer was lost. A refusal is never retried.
import { createConnection } from "node:net";

import { JaahRefused, JaahUnavailable, NotARunner } from "./errors.js";

// Verbs that change nothing, so repeating one after a lost answer cannot double anything.
export const READ_ONLY_VERBS = new Set([
  "agent.task", "agent.answers", "task.get", "task.read", "task.search",
  "task.credential_list", "task.credential_get",
]);

// Per-verb budgets: the daemon's own spend on the slow verbs (settle waits on PR stats, artifact uploads).
export const TIMEOUTS_S = { "task.artifact": 420, "task.settle": 60 };
export const DEFAULT_TIMEOUT_S = 30;
export const ATTEMPTS = 3;
export const BACKOFF_S = [0.5, 1.5];
const MAX_REPLY_BYTES = 4 * 1024 * 1024;

/** Connect failed: the daemon never saw the request. */
class NotDelivered extends JaahUnavailable {}

export class Wire {
  constructor(socketPath, { sleep = (s) => new Promise((done) => setTimeout(done, s * 1000)) } = {}) {
    this.socketPath = socketPath;
    this._sleep = sleep;
  }

  async call(verb, args = {}) {
    let last = null;
    for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
      try {
        return await this._once(verb, args || {});
      } catch (err) {
        if (!(err instanceof JaahUnavailable)) throw err;
        last = err;
        if (!(err instanceof NotDelivered) && !READ_ONLY_VERBS.has(verb)) break;
        if (attempt + 1 < ATTEMPTS) await this._sleep(BACKOFF_S[Math.min(attempt, BACKOFF_S.length - 1)]);
      }
    }
    throw new JaahUnavailable(last.message);
  }

  _once(verb, args) {
    const request = JSON.stringify({ call: verb, args }) + "\n";
    const timeoutMs = (TIMEOUTS_S[verb] ?? DEFAULT_TIMEOUT_S) * 1000;
    return new Promise((resolveCall, rejectCall) => {
      let connected = false;
      let settled = false;
      const chunks = [];
      let size = 0;
      const sock = createConnection(this.socketPath);
      const finish = (fn) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        sock.destroy();
        fn();
      };
      const timer = setTimeout(() => finish(() => rejectCall(connected
        ? new JaahUnavailable(`${verb}: no answer from the node daemon within ${timeoutMs / 1000}s`)
        : new NotDelivered(`${verb}: node daemon unreachable at ${this.socketPath}: connect timed out`))), timeoutMs);
      const onReply = () => {
        const buf = Buffer.concat(chunks);
        if (buf.length === 0) {
          throw new JaahUnavailable(`${verb}: the node daemon closed the connection without replying`);
        }
        const nl = buf.indexOf(10);
        return parseReply(verb, (nl === -1 ? buf : buf.subarray(0, nl)).toString("utf8"));
      };
      const deliver = () => finish(() => {
        try {
          resolveCall(onReply());
        } catch (err) {
          rejectCall(err);
        }
      });
      sock.on("connect", () => {
        connected = true;
        sock.end(request); // write, then half-close: the daemon reads to EOF or newline
      });
      sock.on("data", (chunk) => {
        chunks.push(chunk);
        size += chunk.length;
        if (chunk.includes(10) || size >= MAX_REPLY_BYTES) deliver();
      });
      sock.on("end", deliver);
      sock.on("error", (err) => finish(() => rejectCall(connected
        ? new JaahUnavailable(`${verb}: no answer from the node daemon: ${err.message}`)
        : new NotDelivered(`${verb}: node daemon unreachable at ${this.socketPath}: ${err.message}`))));
    });
  }
}

function parseReply(verb, text) {
  let reply;
  try {
    reply = JSON.parse(text);
  } catch {
    throw new JaahUnavailable(`${verb}: the node daemon returned malformed JSON`);
  }
  if (!reply || typeof reply !== "object" || Array.isArray(reply)) {
    throw new JaahUnavailable(`${verb}: the node daemon returned ${Array.isArray(reply) ? "list" : typeof reply}, not an object`);
  }
  if (reply.ok === true) return reply.result ?? null;
  const reason = String(reply.error || "unknown");
  const detail = typeof reply.detail === "string" ? reply.detail : "";
  if (reason === "not_a_runner") {
    throw new NotARunner(`${verb}: the node daemon cannot tell which task this process belongs to`);
  }
  if (reason === "unavailable" || reason === "busy") {
    throw new JaahUnavailable(`${verb}: ${reason}` + (detail ? `: ${detail}` : ""));
  }
  throw new JaahRefused(reason, detail);
}
