"""Exact-value masking of every secret `credential()` handed out (DD9).

Every string the SDK writes or sends on the agent's behalf — log lines and their data, notify, ask,
settle and fail text — passes through `Masker.mask`, which replaces each known value with
`<redacted:<credential name>>`. Longest value first, so a value containing another is masked whole."""
from __future__ import annotations

from typing import Any

# Config keys whose values describe a credential rather than unlock it. Masking them would shred
# ordinary log text ("calling https://api.github.com") for no protection.
NON_SECRET_KEYS = frozenset({
    "base_url", "region", "type", "scope", "token_type", "header_name", "value_prefix", "project_key",
    "authorize_url", "token_url", "expiry_date", "token_expires_at", "grant_dead_at",
    "access_token_expires_at",
})
# Top-level `task.credential_get` fields that describe the row (`nodevault` holds sealed, not clear, values).
DESCRIPTIVE_FIELDS = frozenset({"name", "type", "type_label", "description", "nodevault"})
# Shorter strings are too likely to occur by accident ("true", "us", "1") to mask safely.
MIN_LENGTH = 6


class Masker:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._ordered: list[tuple[str, str]] = []

    def register(self, name: str, payload: Any) -> None:
        """`payload` is `task.credential_get`'s result: every field but the descriptive ones (`name`,
        `type`, ...) may hold a secret — `config`, a minted `access_token`, any field added later."""
        if isinstance(payload, dict) and "config" in payload:
            for key, value in payload.items():
                if key not in DESCRIPTIVE_FIELDS:
                    self._collect(name, value, key)
        else:
            self._collect(name, payload, None)
        self._ordered = sorted(self._values.items(), key=lambda kv: -len(kv[0]))

    def _collect(self, name: str, node: Any, key: str | None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                self._collect(name, v, k)
        elif isinstance(node, (list, tuple)):
            for v in node:
                self._collect(name, v, key)
        elif isinstance(node, str) and len(node) >= MIN_LENGTH and key not in NON_SECRET_KEYS:
            self._values.setdefault(node, name)

    def mask(self, value: Any) -> Any:
        """Mask every known value in `value`. Anything that is not JSON-native (an exception, bytes, a
        path, ...) is turned into its `str()` first, because the events writer would do that AFTER
        masking and so carry a secret through in the clear."""
        if not self._ordered:
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, dict):
            return {self.mask(str(k)): self.mask(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.mask(v) for v in value]
        if isinstance(value, bytes):
            value = value.decode("utf-8", "backslashreplace")
        value = str(value)
        for secret, name in self._ordered:
            if secret in value:
                value = value.replace(secret, f"<redacted:{name}>")
        return value
