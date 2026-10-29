"""The ACP agent side: the library answers the protocol, the author writes `prompt()`.

The library owns `initialize` (and the `jaah.ai` negotiation), `session/new`, `session/cancel` and
`authenticate`. An author subclasses `Agent`, implements `prompt(turn)` and emits `session/update`
through the `Turn` it is handed. Built on the official ACP SDK (`agent-client-protocol`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid
from collections.abc import Mapping
from typing import Any

import acp
from acp.schema import AgentCapabilities, Implementation, InitializeResponse, NewSessionResponse, PromptResponse

from ._extension import NAMESPACE, Extension, ExtensionError, negotiate

STOP_REASONS = frozenset({"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"})


class Turn:
    """One `session/prompt`: the user's content in, `session/update`s out."""

    def __init__(self, conn: Any, session: _Session, prompt: list[Any]) -> None:
        self._conn = conn
        self.session_id = session.id
        self.cwd = session.cwd
        self.meta = session.meta
        self.prompt = prompt
        self.jaah = session.extension

    @property
    def text(self) -> str:
        """The prompt's text blocks, joined."""
        return "".join(getattr(block, "text", "") for block in self.prompt if getattr(block, "type", "") == "text")

    async def update(self, update: Any) -> None:
        """Emit any `session/update` (build one with the `acp.helpers` / `acp.schema` models)."""
        await self._conn.session_update(session_id=self.session_id, update=update)

    async def say(self, text: str) -> None:
        await self.update(acp.update_agent_message_text(text))

    async def think(self, text: str) -> None:
        await self.update(acp.update_agent_thought_text(text))

    async def complete(self, summary: str, terminal_type: str, **refs: Any) -> dict[str, Any] | None:
        """Settle the task (`task.complete`). Make it the last thing the turn does."""
        return await self.jaah.complete(summary, terminal_type, **refs)

    async def fail(self, reason: str) -> dict[str, Any] | None:
        return await self.jaah.fail(reason)


class Agent:
    """Subclass and implement `prompt`. Everything else in the protocol is the library's."""

    name = "jaah-agent"
    title: str | None = None
    version = "0.0.0"

    async def prompt(self, turn: Turn) -> str | None:
        """Handle one prompt turn. Return a stop reason, or None for `end_turn`."""
        raise NotImplementedError


class _Session:
    def __init__(self, cwd: str, meta: Mapping[str, Any], extension: Extension) -> None:
        self.id = uuid.uuid4().hex
        self.cwd = cwd
        self.meta = dict(meta)
        self.extension = extension
        self.running: asyncio.Task[str | None] | None = None
        self.cancel_requested = False


class _Runtime:
    """The `acp.Agent` the official SDK drives; delegates turns to the author's `Agent`."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._conn: Any = None
        self._methods: frozenset[str] = frozenset()
        self._sessions: dict[str, _Session] = {}

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(
        self, protocol_version: int, client_capabilities: Any = None, client_info: Any = None, **kwargs: Any
    ) -> InitializeResponse:
        self._methods = negotiate(getattr(client_capabilities, "field_meta", None))
        return InitializeResponse(
            protocol_version=min(protocol_version, acp.PROTOCOL_VERSION),
            agent_capabilities=AgentCapabilities(load_session=False),
            agent_info=Implementation(name=self._agent.name, title=self._agent.title, version=self._agent.version),
            auth_methods=[],
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None

    async def new_session(self, cwd: str, mcp_servers: Any = None, **kwargs: Any) -> NewSessionResponse:
        session = _Session(cwd, kwargs.get(NAMESPACE) or {}, Extension(self._send, self._methods))
        self._sessions[session.id] = session
        return NewSessionResponse(session_id=session.id)

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        session = self._sessions.get(session_id)
        if session is None:
            raise acp.RequestError.invalid_params({"sessionId": session_id, "reason": "unknown session"})
        if session.running is not None and not session.running.done():
            raise acp.RequestError.invalid_request({"reason": "a prompt is already running in this session"})
        session.cancel_requested = False
        session.running = asyncio.ensure_future(self._agent.prompt(Turn(self._conn, session, prompt)))
        try:
            reason = await session.running
        except asyncio.CancelledError:
            if not session.cancel_requested:  # the connection itself is going away
                raise
            reason = "cancelled"
        finally:
            session.running = None
        # ACP: after `session/cancel` the turn ends `cancelled`, whatever the handler returned.
        reason = "cancelled" if session.cancel_requested else (reason or "end_turn")
        if reason not in STOP_REASONS:
            raise ValueError(f"prompt() returned {reason!r}; expected one of {sorted(STOP_REASONS)}")
        return PromptResponse(stop_reason=reason)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.get(session_id)
        if session is not None and session.running is not None:
            session.cancel_requested = True
            session.running.cancel()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise acp.RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            # The ACP SDK prefixes the leading underscore itself.
            result = await self._conn.ext_method(method[1:], params)
        except acp.RequestError as err:
            raise ExtensionError(method, err.code, str(err), err.data) from None
        if not isinstance(result, dict):
            raise ExtensionError(method, None, f"the client answered a non-object: {type(result).__name__}")
        return result

    def close(self) -> None:
        for session in self._sessions.values():
            session.extension.close()


async def serve(agent: Agent, input_stream: Any = None, output_stream: Any = None) -> None:
    """Serve `agent` over ACP on the given streams (stdin/stdout by default) until the client hangs up."""
    runtime = _Runtime(agent)
    with contextlib.suppress(NotImplementedError, RuntimeError):  # no signal handlers off the main thread
        # The host ends a settled run with SIGTERM: remove credential files before going.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _terminate, runtime)
    try:
        await acp.run_agent(runtime, input_stream, output_stream)
    finally:
        runtime.close()


def _terminate(runtime: _Runtime) -> None:
    runtime.close()
    os._exit(128 + signal.SIGTERM)


def run(agent: Agent) -> None:
    """Entry point for an agent process: serve ACP on stdio."""
    asyncio.run(serve(agent))
