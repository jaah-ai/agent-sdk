"""A deliberately broken ACP agent on the bare ACP SDK, one defect per MODE (argv[1]), for the suite's own tests.

  ok              echoes and settles once, only when the capability is advertised
  silent          answers the turn without any session/update
  unadvertised    calls _jaah.ai/task/complete even when the client advertised nothing
  double-settle   settles twice
  bad-params      settles without the required `terminal_type`
  no-settle       never settles
  ignores-offer   calls credential/list whenever any jaah.ai offer is present, listed or not
  other-vendor    ok, plus a call to another vendor's extension method (allowed by ACP)
"""

import sys
import uuid

import acp
from acp.schema import InitializeResponse, NewSessionResponse, PromptResponse

MODE = sys.argv[1]


class Raw:
    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kwargs):
        meta = getattr(client_capabilities, "field_meta", None) or {}
        self.advertised = "jaah.ai" in meta
        return InitializeResponse(protocol_version=1)

    async def new_session(self, cwd, mcp_servers=None, **kwargs):
        return NewSessionResponse(session_id=uuid.uuid4().hex)

    async def prompt(self, session_id, prompt, **kwargs):
        if MODE != "silent":
            await self.conn.session_update(session_id=session_id, update=acp.update_agent_message_text("hi"))
        settle = {"summary": "done", "terminal_type": "outcome"}
        if MODE == "other-vendor":
            try:
                await self.conn.ext_method("example.dev/probe", {})
            except acp.RequestError:
                pass
        if self.advertised and MODE == "ignores-offer":
            await self.conn.ext_method("jaah.ai/credential/list", {})
        if MODE == "unadvertised" or (self.advertised and MODE in ("ok", "other-vendor", "ignores-offer")):
            await self.conn.ext_method("jaah.ai/task/complete", settle)
        elif self.advertised and MODE == "double-settle":
            await self.conn.ext_method("jaah.ai/task/complete", settle)
            await self.conn.ext_method("jaah.ai/task/complete", settle)
        elif self.advertised and MODE == "bad-params":
            try:
                await self.conn.ext_method("jaah.ai/task/complete", {"summary": "done"})
            except acp.RequestError:
                pass
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id, **kwargs):
        return None

    async def ext_method(self, method, params):
        raise acp.RequestError.method_not_found(f"_{method}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(acp.run_agent(Raw()))
