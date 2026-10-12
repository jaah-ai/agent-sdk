"""`connect()` and the `Agent` every SDK call hangs off."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Literal, TypedDict

from ._errors import JaahRefused, JaahUnavailable, NotARunner
from ._events import EventsFile, usage_line
from ._mask import Masker
from ._wire import Wire

LOG_LEVELS = ("debug", "info", "warning", "error")
MAX_QUESTIONS = 4
MAX_NOTIFY_CHARS = 2000


class Answer(TypedDict):
    status: Literal["answered", "dismissed"]
    selections: list[str]
    text: str


class TaskContext(TypedDict, total=False):
    task: dict        # {id, title, body, persona, autonomous, out_of_scope}
    project: dict     # {name, repo, integration_branch, branch_prefix, gate_quick_cmd, gate_full_cmd, ...}
    agent: dict       # {id, display_name, version, commit}
    security_gate: Literal["human", "agent"]
    worktree: str | None   # env JAAH_WORKTREE: the task's checkout, also the process's cwd
    inputs: list[dict]     # [{kind, name, path}] from the file at env JAAH_AGENT_INPUTS


class CredentialInfo(TypedDict):
    name: str
    type: str
    description: str
    fields: list[str]


def connect(*, socket_path: str | None = None, events_path: str | None = None) -> "Agent":
    """Attach to the node daemon this process was launched under, and prove it knows our task.

    Raises NotARunner when there is no daemon socket in the environment or the daemon cannot identify
    this process (it left the runner's process tree)."""
    path = socket_path or os.environ.get("JAAH_AGENT_SOCKET")
    if not path:
        raise NotARunner("JAAH_AGENT_SOCKET is not set: this process was not launched by a Jaah node")
    agent = Agent(Wire(path), EventsFile(events_path or os.environ.get("JAAH_AGENT_EVENTS")))
    agent._wire.call("task.get")  # NotARunner surfaces here, before the agent does any work
    return agent


class Agent:
    def __init__(self, wire: Wire, events: EventsFile, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._wire = wire
        self._events = events
        self._mask = Masker()
        self._events.masker = self._mask  # backstop: every events line is masked at the sink too
        self._sleep = sleep
        self._clock = clock

    # ── context ──────────────────────────────────────────────────────────────────────────────────
    def task(self) -> TaskContext:
        """The task this agent is working: `agent.task` plus the node-local `worktree` and `inputs`."""
        ctx: dict = dict(self._wire.call("agent.task") or {})
        ctx["worktree"] = os.environ.get("JAAH_WORKTREE")
        ctx["inputs"] = _read_inputs(os.environ.get("JAAH_AGENT_INPUTS"))
        return ctx  # type: ignore[return-value]

    # ── observability (local events file, never the network) ─────────────────────────────────────
    def log(self, message: str, level: str = "info", **data: Any) -> None:
        if level not in LOG_LEVELS:
            raise ValueError(f"level must be one of {', '.join(LOG_LEVELS)}, got {level!r}")
        self._events.write({"type": "log", "level": level, "message": self._mask.mask(str(message)),
                            "data": self._mask.mask(data)})

    def report_usage(self, model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
                     cache_write: int = 0) -> None:
        """Record one model call's tokens; Jaah prices them on the session's Efficiency view."""
        self._events.write(usage_line(model, input_tokens, output_tokens, cache_read, cache_write))

    # ── humans ───────────────────────────────────────────────────────────────────────────────────
    def notify(self, message: str) -> Any:
        """Post an informational message to the task's channel (console + Slack)."""
        text = self._mask.mask(str(message))
        if len(text) > MAX_NOTIFY_CHARS:
            raise ValueError(f"notify message is {len(text)} characters; the limit is {MAX_NOTIFY_CHARS}")
        result = self._wire.call("agent.notify", {"message": text})
        self._sdk("notify", {"length": len(text)})
        return result

    def ask(self, questions: list[dict], poll_interval: float = 5, timeout: float | None = None) -> list[Answer]:
        """Ask the operator 1..4 questions and BLOCK until every round is answered or dismissed.

        Returns one Answer per question, in order. A dismissed round is `status: "dismissed"` with no
        selections — never an empty "answered". Raises TimeoutError if `timeout` seconds pass first."""
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval}")
        if not 1 <= len(questions) <= MAX_QUESTIONS:
            raise ValueError(f"ask takes 1..{MAX_QUESTIONS} questions, got {len(questions)}")
        wire_questions = [self._question(q) for q in questions]
        ids = list((self._wire.call("agent.ask", {"questions": wire_questions}) or {}).get("ids") or [])
        if len(ids) != len(wire_questions):
            raise JaahRefused("bad_reply", f"asked {len(wire_questions)} questions, got {len(ids)} ids")
        self._sdk("ask", {"questions": len(ids), "ids": ids})
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            try:
                reply = self._wire.call("agent.answers", {"ids": ids}) or {}
            except JaahUnavailable:
                reply = {}  # a daemon restart mid-wait is not an answer; keep waiting
            if reply.get("all_resolved"):
                answers = _answers(ids, reply.get("rounds") or [])
                self._sdk("answers", {"ids": ids, "statuses": [a["status"] for a in answers]})
                return answers
            if deadline is not None and self._clock() >= deadline:
                raise TimeoutError(f"questions {ids} were not all resolved within {timeout} seconds")
            self._sleep(poll_interval if deadline is None else
                        max(0.0, min(poll_interval, deadline - self._clock())))

    def _question(self, q: dict) -> dict:
        if not isinstance(q, dict) or not q.get("question"):
            raise ValueError("each question needs a non-empty 'question'")
        options = []
        for opt in q.get("options") or []:
            o = {"label": opt} if isinstance(opt, str) else dict(opt)
            if not o.get("label"):
                raise ValueError("each option needs a non-empty 'label'")
            options.append(self._mask.mask({k: o[k] for k in ("label", "description") if o.get(k) is not None}))
        out: dict[str, Any] = {"question": self._mask.mask(str(q["question"])), "options": options,
                               "multi_select": bool(q.get("multi_select", False))}
        if q.get("header"):
            out["header"] = self._mask.mask(str(q["header"]))
        if q.get("recommended_index") is not None:
            out["recommended_index"] = int(q["recommended_index"])
        return out

    # ── credentials ──────────────────────────────────────────────────────────────────────────────
    def credentials(self) -> list[CredentialInfo]:
        """The roster: every credential this task may use — names, types and descriptions, no secrets."""
        return list((self._wire.call("task.credential_list") or {}).get("credentials") or [])

    def credential(self, name: str) -> dict:
        """One credential's config, secrets INCLUDED. Every secret value is masked from then on in all
        text this SDK writes or sends. Never write it anywhere yourself."""
        result = self._wire.call("task.credential_get", {"name": name})
        self._mask.register(name, result)
        self._sdk("credential_get", {"name": name})
        return result

    # ── documents and other tasks ────────────────────────────────────────────────────────────────
    def artifact(self, kind: str, path: str) -> Any:
        """Upload a deliverable (`report` .html, `prd`/`architecture` .md, or any `file`)."""
        result = self._wire.call("task.artifact", {"kind": kind, "path": os.path.abspath(path)})
        self._sdk("artifact", {"kind": kind})
        return result

    def read_task(self, task_id: int) -> Any:
        return self._wire.call("task.read", {"peer_task_id": int(task_id)})

    def search_tasks(self, q: str, **filters: Any) -> Any:
        """Filters: state, persona, has_artifacts, artifact_kind, limit."""
        return self._wire.call("task.search", self._mask.mask(
            {"q": q, **{k: v for k, v in filters.items() if v is not None}}))

    # ── ending ───────────────────────────────────────────────────────────────────────────────────
    def settle(self, summary: str, terminal_type: str, *, pr_url: str | None = None,
               no_pr_reason: str | None = None, no_pr_evidence: str | None = None, **gate: Any) -> Any:
        """The ONE terminal call. Make it last: settling frees the slot and the process is ended soon after."""
        args = {**gate, "summary": summary, "terminal_type": terminal_type, "pr_url": pr_url,
                "no_pr_reason": no_pr_reason, "no_pr_evidence": no_pr_evidence}
        # Every field is masked, the free-form gate fields (recon_map, ...) included.
        result = self._wire.call("task.settle", self._mask.mask({k: v for k, v in args.items() if v is not None}))
        self._sdk("settle", {"terminal_type": terminal_type,
                             "settled": bool(isinstance(result, dict) and result.get("settled"))})
        return result

    def fail(self, reason: str) -> Any:
        """Give up on the task: only for what no human could have cleared. Also terminal."""
        args = {"reason": self._mask.mask(reason)}
        if os.environ.get("CONTROL_RUNNER_SESSION_ID"):
            args["session_id"] = os.environ["CONTROL_RUNNER_SESSION_ID"]
        result = self._wire.call("task.fail", args)
        self._sdk("fail", {})
        return result

    def _sdk(self, event: str, detail: dict) -> None:
        self._events.write({"type": "sdk", "event": event, "detail": detail})


def _read_inputs(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return list(data) if isinstance(data, list) else []


def _answers(ids: list[int], rounds: list[dict]) -> list[Answer]:
    by_id = {r.get("id"): r for r in rounds}
    out: list[Answer] = []
    for i in ids:
        r = by_id.get(i)
        if r is None or r.get("status") not in ("answered", "dismissed"):
            raise JaahRefused("bad_reply", f"round {i} is missing or unresolved in a resolved reply")
        if r["status"] == "dismissed":  # a dismissed round carries no answer, whatever the reply says
            out.append({"status": "dismissed", "selections": [], "text": ""})
        else:
            out.append({"status": "answered", "selections": [str(s) for s in r.get("selections") or []],
                        "text": str(r.get("text") or "")})
    return out
