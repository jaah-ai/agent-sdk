"""The agent's side of the `jaah.ai` ACP extension (spec/jaah-extension.json).

A verb `a.b_c` is the ACP method `_jaah.ai/a/b_c`. The client advertises which methods it serves in
`initialize.clientCapabilities._meta["jaah.ai"] = {"version": 1, "methods": [...]}`; a method it did not
advertise is never sent. A session settles (`task.complete` / `task.fail`) once: a settle the client
refused or could not deliver may be sent again, one the client accepted may not.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

NAMESPACE = "jaah.ai"
VERSION = 1
METHOD_PREFIX = f"_{NAMESPACE}/"
SETTLE_VERBS = frozenset({"task.complete", "task.fail"})


class ExtensionUnavailable(RuntimeError):
    """The client did not advertise this `jaah.ai` method (a plain ACP client advertises none)."""


class ExtensionError(RuntimeError):
    """The client answered a `jaah.ai` request with a JSON-RPC error."""

    def __init__(self, method: str, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(f"{method} refused ({code}): {message}")
        self.method = method
        self.code = code
        self.data = data


class AlreadySettled(RuntimeError):
    """This session already settled, or a settle is in flight."""


def method_for(verb: str) -> str:
    """`credential.get` -> `_jaah.ai/credential/get`."""
    noun, _, name = verb.partition(".")
    if not noun or not name or "/" in verb or "." in name:
        raise ValueError(f"not a jaah.ai verb: {verb!r} (expected `noun.verb`)")
    return f"{METHOD_PREFIX}{noun}/{name}"


def verb_for(method: str) -> str:
    """`_jaah.ai/credential/get` -> `credential.get`."""
    if not method.startswith(METHOD_PREFIX):
        raise ValueError(f"not a jaah.ai method: {method!r}")
    return method[len(METHOD_PREFIX):].replace("/", ".", 1)


def negotiate(client_meta: Mapping[str, Any] | None) -> frozenset[str]:
    """The methods a client advertised, or none when it offers no compatible `jaah.ai` capability."""
    offer = (client_meta or {}).get(NAMESPACE)
    version = offer.get("version") if isinstance(offer, Mapping) else None
    if type(version) is not int or version != VERSION:
        return frozenset()
    methods = offer.get("methods")
    if not isinstance(methods, list):
        return frozenset()
    return frozenset(m for m in methods if isinstance(m, str) and m.startswith(METHOD_PREFIX))


Send = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class Extension:
    """Typed calls over the methods this session's client serves."""

    def __init__(self, send: Send, methods: frozenset[str]) -> None:
        self._send = send
        self._methods = methods
        self._settled = False
        self._settling = False
        self._credential_dir: Path | None = None

    @property
    def available(self) -> bool:
        """True when the client serves the `jaah.ai` extension at all."""
        return bool(self._methods)

    @property
    def settled(self) -> bool:
        return self._settled

    def supports(self, verb: str) -> bool:
        return method_for(verb) in self._methods

    async def call(self, verb: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Send any advertised verb with its params (spec/jaah-extension.json has each schema)."""
        method = method_for(verb)
        if method not in self._methods:
            raise ExtensionUnavailable(f"the client does not serve {method}")
        if verb not in SETTLE_VERBS:
            return await self._send(method, dict(params or {}))
        if self._settled or self._settling:
            raise AlreadySettled("this session already settled; settle once, last")
        self._settling = True
        try:
            self.close()  # the run may end the moment the settle lands: no credential file outlives it
            result = await self._send(method, dict(params or {}))
        finally:
            self._settling = False
        # A refusal raised above and left the session unsettled; so does an answer that moved nothing.
        self._settled = result.get("settled", True) is not False
        return result

    # --- settle: at most one per session, and a no-op where nothing is there to settle with ---------

    async def complete(self, summary: str, terminal_type: str, **refs: Any) -> dict[str, Any] | None:
        """`task.complete`. Returns None under a plain ACP client, which has no task to settle."""
        if not self.supports("task.complete"):
            return None
        return await self.call("task.complete", {"summary": summary, "terminal_type": terminal_type, **refs})

    async def fail(self, reason: str) -> dict[str, Any] | None:
        """`task.fail`. Returns None under a plain ACP client."""
        if not self.supports("task.fail"):
            return None
        return await self.call("task.fail", {"reason": reason})

    # --- questions and credentials ----------------------------------------------------------------

    async def ask(
        self,
        question: str,
        *,
        options: list[Any] | None = None,
        header: str | None = None,
        multi_select: bool | None = None,
    ) -> dict[str, Any]:
        """`input.request`: put a question to a human; the client answers when they do."""
        params: dict[str, Any] = {"question": question}
        if options is not None:
            params["options"] = options
        if header is not None:
            params["header"] = header
        if multi_select is not None:
            params["multi_select"] = multi_select
        return await self.call("input.request", params)

    async def credentials(self) -> dict[str, Any]:
        """`credential.list`: names, types and descriptions — never a secret."""
        return await self.call("credential.list")

    async def credential(self, name: str) -> dict[str, Any]:
        """`credential.get`: the credential's configuration, secrets included. Never log it."""
        return await self.call("credential.get", {"name": name})

    async def credential_file(self, name: str) -> Path:
        """`credential.get`, written 0600 into a private 0700 directory; returns the file's path.

        This is how a key reaches a tool: as a config file it reads, never an environment variable.
        """
        value = await self.credential(name)
        if self._credential_dir is None:
            self._credential_dir = Path(tempfile.mkdtemp(prefix="jaah-credentials-"))
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".") or "credential"
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]  # `a/b` and `a_b` stay two files
        path = self._credential_dir / f"{safe}-{digest}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
        return path

    def close(self) -> None:
        """Remove every credential file this session wrote."""
        if self._credential_dir is not None:
            shutil.rmtree(self._credential_dir, ignore_errors=True)
            self._credential_dir = None
