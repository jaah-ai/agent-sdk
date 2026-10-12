"""One JSON line each way on the node daemon's socket.

    -> {"call": "task.settle", "args": {...}}
    <- {"ok": true, "result": ...}   or   {"ok": false, "error": "<reason>", "detail": "..."}

The daemon identifies the calling task from the process ancestry, so a request carries no token and no
task id. Retry rule: a request that certainly never reached the daemon (connect failed) is retried for
every verb; any other unavailability is retried only for a read-only verb, because a write may have
landed before the answer was lost. A refusal is never retried."""
from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable

from ._errors import JaahRefused, JaahUnavailable, NotARunner

# Verbs that change nothing, so repeating one after a lost answer cannot double anything.
READ_ONLY_VERBS = frozenset({
    "agent.task", "agent.answers", "task.get", "task.read", "task.search",
    "task.credential_list", "task.credential_get",
})

# Per-verb budgets: the daemon's own spend on the slow verbs (settle waits on PR stats, artifact uploads).
TIMEOUTS_S = {"task.artifact": 420.0, "task.settle": 60.0}
DEFAULT_TIMEOUT_S = 30.0
ATTEMPTS = 3
BACKOFF_S = (0.5, 1.5)
_MAX_REPLY_BYTES = 4 * 1024 * 1024


class _NotDelivered(JaahUnavailable):
    """Connect failed: the daemon never saw the request."""


class Wire:
    def __init__(self, socket_path: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.socket_path = socket_path
        self._sleep = sleep

    def call(self, verb: str, args: dict | None = None) -> Any:
        last: JaahUnavailable | None = None
        for attempt in range(ATTEMPTS):
            try:
                return self._once(verb, args or {})
            except JaahUnavailable as exc:
                last = exc
                if not isinstance(exc, _NotDelivered) and verb not in READ_ONLY_VERBS:
                    break
                if attempt + 1 < ATTEMPTS:
                    self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        assert last is not None
        raise JaahUnavailable(str(last)) from last

    def _once(self, verb: str, args: dict) -> Any:
        request = (json.dumps({"call": verb, "args": args}, separators=(",", ":")) + "\n").encode()
        timeout = TIMEOUTS_S.get(verb, DEFAULT_TIMEOUT_S)
        deadline = time.monotonic() + timeout  # the budget is for the whole call, not per recv
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            try:
                sock.connect(self.socket_path)
            except OSError as exc:
                raise _NotDelivered(f"{verb}: node daemon unreachable at {self.socket_path}: {exc}") from exc
            try:
                sock.sendall(request)
                sock.shutdown(socket.SHUT_WR)
                buf = b""
                while b"\n" not in buf and len(buf) < _MAX_REPLY_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout(f"no complete reply within {timeout:g}s")
                    sock.settimeout(remaining)
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except OSError as exc:
                raise JaahUnavailable(f"{verb}: no answer from the node daemon: {exc}") from exc
        finally:
            sock.close()
        if not buf:
            raise JaahUnavailable(f"{verb}: the node daemon closed the connection without replying")
        try:
            reply = json.loads(buf.split(b"\n", 1)[0])
        except ValueError as exc:
            raise JaahUnavailable(f"{verb}: the node daemon returned malformed JSON") from exc
        if not isinstance(reply, dict):
            raise JaahUnavailable(f"{verb}: the node daemon returned {type(reply).__name__}, not an object")
        if reply.get("ok") is True:
            return reply.get("result")
        reason = str(reply.get("error") or "unknown")
        detail = reply.get("detail") if isinstance(reply.get("detail"), str) else ""
        if reason == "not_a_runner":
            raise NotARunner(f"{verb}: the node daemon cannot tell which task this process belongs to")
        if reason in ("unavailable", "busy"):
            raise JaahUnavailable(f"{verb}: {reason}" + (f": {detail}" if detail else ""))
        raise JaahRefused(reason, detail)
