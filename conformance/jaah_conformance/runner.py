"""Run one ACP agent through a conformance profile and report each check.

The runner is an ACP client (the official `agent-client-protocol` SDK) plus a mock of the `jaah.ai`
extension that validates every call against `spec/jaah-extension.json`. It needs no Jaah source: only
this package and the spec file.

Profiles:
  acp-core        a plain ACP client — the agent must run without the extension and never call it.
  jaah-extension  acp-core, then a run that advertises every method and one that advertises only
                  `task/complete`: every call is an advertised method with schema-valid params, and the
                  agent settles exactly once (one accepted settle), inside the turn.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
import jsonschema
from acp.schema import ClientCapabilities, Implementation, RequestPermissionResponse

PROFILES = ("acp-core", "jaah-extension")
NAMESPACE = "jaah.ai"
SETTLE_METHODS = frozenset({"_jaah.ai/task/complete", "_jaah.ai/task/fail"})
DEFAULT_PROMPT = "Jaah conformance check: reply with one short sentence."
PROBE_METHOD = "conformance.invalid/probe"  # sent as `_conformance.invalid/probe`


def find_spec(start: Path | None = None) -> Path:
    """`spec/jaah-extension.json` in this package's tree (the SDK in the monorepo or its mirror repo), else the
    copy an installed wheel carries (hatch_build.py puts it there)."""
    here = start or Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "spec" / "jaah-extension.json"
        if candidate.is_file():
            return candidate
    bundled = here.parent / "jaah-extension.json"
    if bundled.is_file():
        return bundled
    raise FileNotFoundError("spec/jaah-extension.json not found; pass --spec")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    profile: str
    command: list[str]
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, detail))

    def to_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "command": self.command,
            "passed": self.passed,
            "checks": [c.__dict__ for c in self.checks],
        }

    def render(self) -> str:
        lines = [f"profile {self.profile}: {' '.join(self.command)}"]
        for c in self.checks:
            lines.append(f"  {'PASS' if c.passed else 'FAIL'}  {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append(f"{'PASSED' if self.passed else 'FAILED'} ({sum(c.passed for c in self.checks)}/{len(self.checks)})")
        return "\n".join(lines)


class MockJaah:
    """The client half of the `jaah.ai` extension: canned answers, every call checked against the spec."""

    def __init__(self, spec: dict[str, Any], offered: Sequence[str] | None = None) -> None:
        self.spec = spec
        self.methods = list(spec["methods"] if offered is None else offered)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.violations: list[str] = []
        self.accepted_settles = 0

    def capability(self) -> dict[str, Any]:
        return {"version": self.spec["version"], "methods": self.methods}

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        entry = self.spec["methods"].get(method)
        if entry is None:
            self.violations.append(f"{method} is not a method of spec version {self.spec['version']}")
            raise acp.RequestError.method_not_found(method)
        if method not in self.methods:
            self.violations.append(f"{method} was called but not advertised")
            raise acp.RequestError.method_not_found(method)
        errors = sorted(jsonschema.Draft202012Validator(entry["params"]).iter_errors(params), key=str)
        if errors:
            self.violations.append(f"{method} params invalid: {errors[0].message}")
            raise acp.RequestError.invalid_params({"method": method, "error": errors[0].message})
        if method in SETTLE_METHODS:
            self.accepted_settles += 1
        return self._answer(entry["verb"], params)

    @staticmethod
    def _answer(verb: str, params: dict[str, Any]) -> dict[str, Any]:
        if verb == "task.complete":
            return {"stored": True, "settled": True, "state": "done"}
        if verb == "task.fail":
            return {"stored": True, "settled": True, "state": "system_failed"}
        if verb == "credential.list":
            return {"credentials": [{"name": "conformance/example", "type": "generic_api_key", "description": "",
                                     "fields": ["api_key"]}]}
        if verb == "credential.get":
            return {"name": params["name"], "type": "generic_api_key", "config": {"api_key": "conformance-placeholder"}}
        if verb == "input.request":
            options = params.get("options") or []
            first = options[0] if options else "yes"
            return {"answer": first.get("label", "yes") if isinstance(first, dict) else str(first)}
        return {"ok": True}


class ConformanceClient:
    """The ACP client the agent talks to: records updates, answers the extension when advertised."""

    def __init__(self, jaah: MockJaah | None) -> None:
        self.jaah = jaah
        self.updates: list[tuple[str, Any]] = []
        self.unadvertised: list[str] = []
        self.prompt_done = False
        self.late_settles: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any) -> Any:
        return RequestPermissionResponse(outcome={"outcome": "cancelled"})

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        wire = f"_{method}"
        if not wire.startswith(f"_{NAMESPACE}/"):  # another vendor's extension: allowed, just not served
            raise acp.RequestError.method_not_found(wire)
        if self.jaah is None:
            self.unadvertised.append(wire)
            raise acp.RequestError.method_not_found(wire)
        if self.prompt_done and wire in SETTLE_METHODS:
            self.late_settles.append(wire)
        return self.jaah.handle(wire, params)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        if method.startswith(f"{NAMESPACE}/"):
            self.unadvertised.append(f"_{method} (notification)")


async def _run_once(
    report: Report, client: ConformanceClient, command: Sequence[str], prompt: str, timeout: float, label: str
) -> None:
    jaah = client.jaah
    meta = {NAMESPACE: jaah.capability()} if jaah else None
    async with acp.spawn_agent_process(client, command[0], *command[1:]) as (conn, _process):
        init = await asyncio.wait_for(
            conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(field_meta=meta),
                client_info=Implementation(name="jaah-conformance", version="2.0.0"),
            ),
            timeout,
        )
        report.add(f"{label}: initialize negotiates protocol v{acp.PROTOCOL_VERSION}",
                   init.protocol_version == acp.PROTOCOL_VERSION, f"agent answered {init.protocol_version}")

        session = await asyncio.wait_for(conn.new_session(cwd=str(Path.cwd()), mcp_servers=[]), timeout)
        report.add(f"{label}: session/new returns a sessionId", bool(session.session_id))

        response = await asyncio.wait_for(
            conn.prompt(session_id=session.session_id, prompt=[acp.text_block(prompt)]), timeout
        )
        client.prompt_done = True
        report.add(f"{label}: session/prompt ends with a stop reason", bool(response.stop_reason),
                   str(response.stop_reason))
        mine = [u for sid, u in client.updates if sid == session.session_id]
        report.add(f"{label}: the turn emits session/update", bool(mine), f"{len(mine)} update(s)")
        stray = [sid for sid, _ in client.updates if sid != session.session_id]
        report.add(f"{label}: every session/update names the session", not stray,
                   f"unknown session ids: {sorted(set(stray))}" if stray else "")

        try:
            await asyncio.wait_for(conn.ext_method(PROBE_METHOD, {}), timeout)
            probe_ok, detail = False, "an unknown extension method was answered with a result"
        except acp.RequestError as err:
            probe_ok, detail = err.code == -32601, f"error {err.code}"
        report.add(f"{label}: an unknown extension method gets -32601", probe_ok, detail)


async def _guarded(
    report: Report, command: Sequence[str], jaah: MockJaah | None, prompt: str, timeout: float, label: str
) -> ConformanceClient:
    """One run; a crash, hang or protocol error is a failed check, never a traceback."""
    client = ConformanceClient(jaah)
    try:
        await _run_once(report, client, command, prompt, timeout, label)
    except TimeoutError:
        report.add(f"{label}: the agent answers within {timeout:g}s", False, "timed out")
    except Exception as err:  # noqa: BLE001 — any failure of the agent under test is a result
        report.add(f"{label}: the run completes", False, f"{type(err).__name__}: {err}")
    return client


async def run_profile(
    profile: str, command: Sequence[str], spec_path: Path, prompt: str = DEFAULT_PROMPT, timeout: float = 120.0
) -> Report:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    spec = json.loads(spec_path.read_text())
    report = Report(profile, list(command))

    plain = await _guarded(report, command, None, prompt, timeout, "plain ACP")
    report.add("plain ACP: no jaah.ai call without the capability", not plain.unadvertised,
               ", ".join(plain.unadvertised))
    if profile == "acp-core":
        return report

    for label, offered in (("jaah.ai", None), ("jaah.ai partial offer", ["_jaah.ai/task/complete"])):
        jaah = MockJaah(spec, offered)
        run = await _guarded(report, command, jaah, prompt, timeout, label)
        report.add(f"{label}: every call is an advertised method with schema-valid params",
                   not jaah.violations and not run.unadvertised, "; ".join(jaah.violations + run.unadvertised))
        report.add(f"{label}: settles exactly once", jaah.accepted_settles == 1,
                   f"{jaah.accepted_settles} accepted settle(s)")
        report.add(f"{label}: settles inside the prompt turn", not run.late_settles, ", ".join(run.late_settles))
    return report
