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
// Shorter strings are too likely to occur by accident ("true", "us", "1") to mask safely.
export const MIN_LENGTH = 6;

export class Masker {
  constructor() {
    this._values = new Map();
    this._ordered = [];
  }

  /**
   * `payload` is `task.credential_get`'s result: the secrets are its `config` and any minted
   * `access_token`; its `name`/`type` describe the row and are not secret.
   */
  register(name, payload) {
    if (payload && typeof payload === "object" && !Array.isArray(payload) && "config" in payload) {
      this._collect(name, payload.config, null);
      this._collect(name, payload.access_token, "access_token");
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

  mask(value) {
    if (this._ordered.length === 0) return value;
    if (typeof value === "string") {
      for (const [secret, name] of this._ordered) {
        if (value.includes(secret)) value = value.split(secret).join(`<redacted:${name}>`);
      }
      return value;
    }
    if (Array.isArray(value)) return value.map((v) => this.mask(v));
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, this.mask(v)]));
    }
    return value;
  }
}
