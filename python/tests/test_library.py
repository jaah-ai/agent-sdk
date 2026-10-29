"""jaah_agent: the extension's negotiation, settle-once, credentials as files, and turn cancellation."""

import asyncio
import json
import os
import stat
import sys
import textwrap

import acp
import pytest
from acp.schema import ClientCapabilities

import jaah_agent
from jaah_agent._extension import negotiate

ALL = ["_jaah.ai/task/complete", "_jaah.ai/task/fail", "_jaah.ai/credential/get", "_jaah.ai/input/request"]


def extension(methods=ALL, answers=None):
    sent = []

    async def send(method, params):
        sent.append((method, params))
        return (answers or {}).get(method, {"ok": True})

    return jaah_agent.Extension(send, frozenset(methods)), sent


def test_verbs_map_to_acp_extension_methods():
    assert jaah_agent.method_for("pull_request.enqueue") == "_jaah.ai/pull_request/enqueue"
    assert jaah_agent.verb_for("_jaah.ai/credential/get") == "credential.get"
    with pytest.raises(ValueError):
        jaah_agent.method_for("task/complete")


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (None, frozenset()),
        ({"jaah.ai": {"version": 2, "methods": ALL}}, frozenset()),
        ({"jaah.ai": {"task_id": 7}}, frozenset()),
        ({"jaah.ai": {"version": 1, "methods": ALL + ["_other/x", 3]}}, frozenset(ALL)),
    ],
)
def test_negotiation_takes_only_a_v1_offer(meta, expected):
    assert negotiate(meta) == expected


def test_settles_once_and_never_sends_an_unadvertised_method():
    ext, sent = extension()
    asyncio.run(ext.complete("done", "outcome", pr_url="https://example.invalid/1"))
    with pytest.raises(jaah_agent.AlreadySettled):
        asyncio.run(ext.fail("again"))
    with pytest.raises(jaah_agent.ExtensionUnavailable):
        asyncio.run(ext.call("memory.search", {"query": "x"}))
    assert sent == [("_jaah.ai/task/complete",
                     {"summary": "done", "terminal_type": "outcome", "pr_url": "https://example.invalid/1"})]


def test_a_refused_or_unmoved_settle_may_be_sent_again():
    attempts = []

    async def send(method, params):
        attempts.append(method)
        if len(attempts) == 1:
            raise jaah_agent.ExtensionError(method, -32000, "refused: upload the report first")
        if len(attempts) == 2:
            return {"stored": True, "settled": False}
        return {"stored": True, "settled": True}

    ext = jaah_agent.Extension(send, frozenset(ALL))
    with pytest.raises(jaah_agent.ExtensionError):
        asyncio.run(ext.complete("done", "outcome"))
    assert asyncio.run(ext.complete("done", "outcome")) == {"stored": True, "settled": False}
    assert not ext.settled
    asyncio.run(ext.complete("done", "outcome"))
    assert ext.settled
    with pytest.raises(jaah_agent.AlreadySettled):
        asyncio.run(ext.complete("done", "outcome"))
    assert len(attempts) == 3


def test_two_settles_in_flight_send_one():
    release = None

    async def send(method, params):
        await release.wait()
        return {"settled": True}

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        ext = jaah_agent.Extension(send, frozenset(ALL))
        first = asyncio.ensure_future(ext.complete("a", "outcome"))
        await asyncio.sleep(0)
        with pytest.raises(jaah_agent.AlreadySettled):
            await ext.fail("b")
        release.set()
        await first

    asyncio.run(scenario())


def test_settle_is_a_no_op_under_a_plain_acp_client():
    ext, sent = extension(methods=[])
    assert not ext.available
    assert asyncio.run(ext.complete("done", "outcome")) is None
    assert sent == []


def test_ask_sends_input_request():
    ext, sent = extension(answers={"_jaah.ai/input/request": {"answer": "B"}})
    assert asyncio.run(ext.ask("Which?", options=["A", "B"], header="Choice")) == {"answer": "B"}
    assert sent == [("_jaah.ai/input/request", {"question": "Which?", "options": ["A", "B"], "header": "Choice"})]


def test_a_credential_arrives_as_a_private_file_never_the_environment():
    config = {"name": "x/y", "config": {"api_key": "placeholder"}}
    ext, _ = extension(answers={"_jaah.ai/credential/get": config})
    before = dict(os.environ)
    path = asyncio.run(ext.credential_file("x/y"))
    assert json.loads(path.read_text()) == config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert dict(os.environ) == before
    other = asyncio.run(ext.credential_file("x_y"))
    assert other != path and path.exists()  # `x/y` and `x_y` are two files
    asyncio.run(ext.complete("done", "outcome"))
    assert not path.parent.exists()  # settling removes every credential file first


SLOW_AGENT = textwrap.dedent(
    """
    import asyncio, jaah_agent

    class Slow(jaah_agent.Agent):
        async def prompt(self, turn):
            await turn.say("working")
            await asyncio.sleep(60)

    jaah_agent.run(Slow())
    """
)


class Recorder:
    def __init__(self):
        self.updates = asyncio.Event()

    async def session_update(self, session_id, update, **kwargs):
        self.updates.set()

    async def request_permission(self, *args, **kwargs):
        raise acp.RequestError.method_not_found("session/request_permission")


def test_session_cancel_ends_the_turn_cancelled():
    async def scenario():
        client = Recorder()
        async with acp.spawn_agent_process(client, sys.executable, "-c", SLOW_AGENT) as (conn, _):
            await conn.initialize(protocol_version=1, client_capabilities=ClientCapabilities())
            session = await conn.new_session(cwd=".", mcp_servers=[])
            turn = asyncio.ensure_future(conn.prompt(session_id=session.session_id, prompt=[acp.text_block("go")]))
            await asyncio.wait_for(client.updates.wait(), 10)
            await conn.cancel(session_id=session.session_id)
            return (await asyncio.wait_for(turn, 10)).stop_reason

    assert asyncio.run(scenario()) == "cancelled"
