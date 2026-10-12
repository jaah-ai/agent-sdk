// Exact-value masking of every secret `credential()` handed out (DD9) — the twin of python/_mask.py.
//
// Every string the SDK writes or sends on the agent's behalf — log lines and their data, notify, ask,
// settle and fail text — passes through `Masker.mask`, which replaces each known value with
// `<redacted:<credential name>>`. Longest value first, so a value containing another is masked whole.

// Config keys whose values describe a credential rather than unlock it. Masking them would shred
// ordinary log text ("calling https://api.github.com") for no protection.
export const NON_SECRET_KEYS = new Set([
  "base_url", "region", "type", "scope", "token_type", "header_name", "value_prefix", "project_key",
  "authorize_url", "token_url", "expiry_date", "token_expires_at", "grant_dead_at",
  "access_token_expires_at",
]);
// Top-level `task.credential_get` fields that describe the row (`nodevault` holds sealed, not clear, values).
export const DESCRIPTIVE_FIELDS = new Set(["name", "type", "type_label", "description", "nodevault"]);
// Shorter strings are too likely to occur by accident ("true", "us", "1") to mask safely.
export const MIN_LENGTH = 6;

export class Masker {
  constructor() {
    this._values = new Map();
    this._ordered = [];
  }

  /**
   * `payload` is `task.credential_get`'s result: every field but the descriptive ones (`name`,
   * `type`, ...) may hold a secret — `config`, a minted `access_token`, any field added later.
   */
  register(name, payload) {
    if (payload && typeof payload === "object" && !Array.isArray(payload) && "config" in payload) {
      for (const [key, value] of Object.entries(payload)) {
        if (!DESCRIPTIVE_FIELDS.has(key)) this._collect(name, value, key);
      }
    } else {
      this._collect(name, payload, null);
    }
    this._ordered = [...this._values.entries()].sort((a, b) => b[0].length - a[0].length);
  }

  _collect(name, node, key) {
    if (Array.isArray(node)) {
      for (const v of node) this._collect(name, v, key);
    } else if (node && typeof node === "object") {
      for (const [k, v] of Object.entries(node)) this._collect(name, v, k);
    } else if (typeof node === "string" && node.length >= MIN_LENGTH && !NON_SECRET_KEYS.has(key)) {
      if (!this._values.has(node)) this._values.set(node, name);
    }
  }

  // Mask every known value in `value`. Anything that is not a plain object, array or JSON scalar (an
  // Error, a Date, a Buffer, ...) is turned into its string form first — walking its entries would
  // drop or leak it, and JSON.stringify would serialise it AFTER masking.
  mask(value) {
    if (this._ordered.length === 0) return value;
    if (value == null || typeof value === "boolean" || typeof value === "number") return value;
    if (Array.isArray(value) || value instanceof Set) return [...value].map((v) => this.mask(v));
    if (typeof value === "object") {
      const proto = Object.getPrototypeOf(value);
      if (proto === Object.prototype || proto === null) {
        return Object.fromEntries(Object.entries(value).map(([k, v]) => [this.mask(k), this.mask(v)]));
      }
    }
    let text = String(value);
    for (const [secret, name] of this._ordered) {
      if (text.includes(secret)) text = text.split(secret).join(`<redacted:${name}>`);
    }
    return text;
  }
}
