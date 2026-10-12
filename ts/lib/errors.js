// The three ways an SDK call can fail. Every call rejects with one of these or resolves to a real
// result — never a partial or default value that could be mistaken for an empty answer.

/** Base class: this call did not produce a result. */
export class JaahError extends Error {
  get name() {
    return this.constructor.name;
  }
}

/**
 * No answer from the node daemon, or the daemon could not reach the control plane. RETRYABLE: the SDK
 * already retried what was safe to retry before raising this.
 */
export class JaahUnavailable extends JaahError {}

/**
 * The daemon or the control plane answered, and the answer was no. NEVER retried: the same request earns
 * the same refusal. `reason` is the daemon's coarse code (`rejected`, `bad_request`, ...); `detail` is the
 * control plane's own explanation when it sent one.
 */
export class JaahRefused extends JaahError {
  constructor(reason, detail = "") {
    super(detail ? `${reason}: ${detail}` : reason);
    this.reason = reason;
    this.detail = detail;
  }
}

/**
 * The daemon cannot tell which task this process belongs to: it is not running under a Jaah node, or it
 * left the runner's process tree (never daemonize, double-fork or `setsid` away from it).
 */
export class NotARunner extends JaahError {}
