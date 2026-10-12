"""Every §3.8 call against a fake daemon. Run: python -m unittest discover -s python/tests"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jaah_agent  # noqa: E402
from jaah_agent import JaahRefused, JaahUnavailable, NotARunner  # noqa: E402
from jaah_agent import _wire  # noqa: E402
from fake_daemon import FakeDaemon, err, ok  # noqa: E402

SECRET = "ghp_SuperSecretValue123"
CREDENTIAL = {"name": "acme/github", "type": "github", "config": {
    "token": SECRET, "base_url": "https://api.github.com", "refresh_token": "rt_another_secret_99"},
    "access_token": None, "access_token_expires_at": None}


class Base(unittest.TestCase):
    routes: dict = {}

    def setUp(self):
        self._sleeps = []
        _wire_sleep = self._sleeps.append
        self.daemon = FakeDaemon(self.handle)
        self.addCleanup(self.daemon.close)
        self.tmp = tempfile.mkdtemp()
        self.events_path = os.path.join(self.tmp, "events.jsonl")
        self.replies: dict = {"task.get": ok({"id": 7})}
        self.agent = jaah_agent.connect(socket_path=self.daemon.path, events_path=self.events_path)
        self.agent._wire._sleep = _wire_sleep
        self.agent._sleep = _wire_sleep
        self.daemon.calls.clear()

    def handle(self, verb, args):
        reply = self.replies.get(verb, ok({}))
        return reply(args) if callable(reply) else reply

    def events(self) -> list[dict]:
        if not os.path.exists(self.events_path):
            return []
        with open(self.events_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]


class ConnectTest(unittest.TestCase):
    def test_no_socket_in_env_is_not_a_runner(self):
        env = os.environ.pop("JAAH_AGENT_SOCKET", None)
        try:
            with self.assertRaises(NotARunner):
                jaah_agent.connect()
        finally:
            if env is not None:
                os.environ["JAAH_AGENT_SOCKET"] = env

    def test_daemon_not_a_runner(self):
        d = FakeDaemon(lambda v, a: err("not_a_runner"))
        self.addCleanup(d.close)
        with self.assertRaises(NotARunner):
            jaah_agent.connect(socket_path=d.path)
        self.assertEqual(d.verbs(), ["task.get"])

    def test_env_socket(self):
        d = FakeDaemon(lambda v, a: ok({"id": 1}))
        self.addCleanup(d.close)
        os.environ["JAAH_AGENT_SOCKET"] = d.path
        try:
            self.assertIsInstance(jaah_agent.connect(), jaah_agent.Agent)
        finally:
            del os.environ["JAAH_AGENT_SOCKET"]


class CallsTest(Base):
    def test_task_adds_worktree_and_inputs(self):
        inputs = os.path.join(self.tmp, "inputs.json")
        with open(inputs, "w") as fh:
            json.dump([{"kind": "prd", "name": "prd.md", "path": "/x/prd.md"}], fh)
        ctx_wire = {"task": {"id": 7, "title": "t", "body": "b", "persona": "coder", "autonomous": True,
                             "out_of_scope": ""}, "project": {"name": "p"}, "agent": {"id": "a"},
                    "security_gate": "human"}
        self.replies["agent.task"] = ok(ctx_wire)
        os.environ.update(JAAH_WORKTREE="/w", JAAH_AGENT_INPUTS=inputs)
        try:
            ctx = self.agent.task()
        finally:
            del os.environ["JAAH_WORKTREE"], os.environ["JAAH_AGENT_INPUTS"]
        self.assertEqual(ctx["worktree"], "/w")
        self.assertEqual(ctx["inputs"][0]["kind"], "prd")
        self.assertEqual(ctx["security_gate"], "human")
        self.assertEqual(self.daemon.calls, [("agent.task", {})])

    def test_log_line(self):
        self.agent.log("hello", level="warning", step=2)
        (line,) = self.events()
        self.assertEqual(list(line), ["type", "timestamp", "level", "message", "data"])
        self.assertEqual((line["type"], line["level"], line["message"], line["data"]),
                         ("log", "warning", "hello", {"step": 2}))
        self.assertTrue(line["timestamp"].endswith("Z"))
        with self.assertRaises(ValueError):
            self.agent.log("x", level="loud")

    def test_usage_line_shape_is_the_contract(self):
        self.agent.report_usage("gpt-5-codex", 1, 2, cache_read=3, cache_write=4)
        (line,) = self.events()
        self.assertEqual(list(line), ["type", "timestamp", "message"])
        self.assertEqual(list(line["message"]), ["id", "model", "usage"])
        self.assertEqual(list(line["message"]["usage"]), [
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
        self.assertEqual(uuid.UUID(line["message"]["id"]).version, 4)
        self.assertEqual(line["message"]["usage"], {"input_tokens": 1, "output_tokens": 2,
                                                    "cache_read_input_tokens": 3,
                                                    "cache_creation_input_tokens": 4})

    def test_notify(self):
        self.replies["agent.notify"] = ok({"ok": True})
        self.agent.notify("halfway")
        self.assertEqual(self.daemon.calls, [("agent.notify", {"message": "halfway"})])
        self.assertEqual(self.events()[-1]["detail"], {"length": 7})
        with self.assertRaises(ValueError):
            self.agent.notify("x" * 2001)

    def test_credentials_and_credential(self):
        self.replies["task.credential_list"] = ok({"credentials": [{"name": "a", "type": "github"}]})
        self.replies["task.credential_get"] = ok(CREDENTIAL)
        self.assertEqual(self.agent.credentials()[0]["name"], "a")
        self.assertEqual(self.agent.credential("acme/github")["config"]["token"], SECRET)
        self.assertEqual(self.daemon.calls[-1], ("task.credential_get", {"name": "acme/github"}))
        sdk = self.events()[-1]
        self.assertEqual((sdk["type"], sdk["event"], sdk["detail"]), ("sdk", "credential_get", {"name": "acme/github"}))

    def test_credential_refusals_pass_through(self):
        self.replies["task.credential_get"] = err("rejected", "HTTP 409: two credentials share that name")
        with self.assertRaises(JaahRefused) as cm:
            self.agent.credential("dup")
        self.assertEqual(cm.exception.reason, "rejected")
        self.assertIn("409", cm.exception.detail)

    def test_artifact_absolute_path(self):
        self.agent.artifact("report", "report.html")
        verb, args = self.daemon.calls[0]
        self.assertEqual((verb, args["kind"]), ("task.artifact", "report"))
        self.assertTrue(os.path.isabs(args["path"]))

    def test_read_and_search(self):
        self.agent.read_task(42)
        self.agent.search_tasks("sdk", state="done", limit=None)
        self.assertEqual(self.daemon.calls, [("task.read", {"peer_task_id": 42}),
                                             ("task.search", {"q": "sdk", "state": "done"})])

    def test_settle(self):
        self.replies["task.settle"] = ok({"stored": True, "settled": True, "state": "done"})
        res = self.agent.settle("done", "merged_pr", pr_url="https://x/pull/1", recon_map={"files": []})
        self.assertTrue(res["settled"])
        self.assertEqual(self.daemon.calls, [("task.settle", {
            "summary": "done", "terminal_type": "merged_pr", "pr_url": "https://x/pull/1",
            "recon_map": {"files": []}})])
        self.assertEqual(self.events()[-1]["detail"], {"terminal_type": "merged_pr", "settled": True})

    def test_fail_carries_session_id(self):
        os.environ["CONTROL_RUNNER_SESSION_ID"] = "sess-1"
        try:
            self.agent.fail("boom")
        finally:
            del os.environ["CONTROL_RUNNER_SESSION_ID"]
        self.assertEqual(self.daemon.calls, [("task.fail", {"reason": "boom", "session_id": "sess-1"})])


class AskTest(Base):
    def _poll(self, *replies):
        seq = list(replies)
        self.replies["agent.answers"] = lambda args: seq.pop(0) if len(seq) > 1 else seq[0]

    def test_blocks_until_all_resolved_and_dismissed_is_distinct(self):
        self.replies["agent.ask"] = ok({"ids": [11, 12]})
        open_ = {"id": 12, "status": "open", "selections": [], "text": ""}
        answered = {"id": 11, "status": "answered", "selections": ["A", "C"], "text": "A, C"}
        dismissed = {"id": 12, "status": "dismissed", "selections": [], "text": ""}
        self._poll(ok({"rounds": [answered, open_], "all_resolved": False}),
                   ok({"rounds": [answered, open_], "all_resolved": False}),
                   ok({"rounds": [answered, dismissed], "all_resolved": True}))
        answers = self.agent.ask([
            {"question": "Which?", "header": "Pick", "options": ["A", "B", {"label": "C", "description": "c"}],
             "multi_select": True, "recommended_index": 0},
            {"question": "Proceed?", "options": ["yes", "no"]},
        ], poll_interval=2)
        self.assertEqual(answers, [{"status": "answered", "selections": ["A", "C"], "text": "A, C"},
                                   {"status": "dismissed", "selections": [], "text": ""}])
        self.assertEqual(self.daemon.verbs(), ["agent.ask", "agent.answers", "agent.answers", "agent.answers"])
        self.assertEqual(self._sleeps, [2, 2])
        self.assertEqual(self.daemon.calls[0][1]["questions"][0], {
            "question": "Which?", "options": [{"label": "A"}, {"label": "B"}, {"label": "C", "description": "c"}],
            "multi_select": True, "header": "Pick", "recommended_index": 0})
        self.assertEqual(self.daemon.calls[1][1], {"ids": [11, 12]})

    def test_timeout(self):
        self.replies["agent.ask"] = ok({"ids": [1]})
        self._poll(ok({"rounds": [{"id": 1, "status": "open"}], "all_resolved": False}))
        t = [0.0]
        self.agent._clock = lambda: t[0]
        self.agent._sleep = lambda s: t.__setitem__(0, t[0] + s)
        with self.assertRaises(TimeoutError):
            self.agent.ask([{"question": "q", "options": ["a"]}], poll_interval=5, timeout=12)

    def test_unavailable_while_polling_keeps_waiting(self):
        self.replies["agent.ask"] = ok({"ids": [1]})
        self._poll(err("unavailable"), err("unavailable"), err("unavailable"),
                   ok({"rounds": [{"id": 1, "status": "answered", "selections": ["a"], "text": "a"}],
                       "all_resolved": True}))
        self.assertEqual(self.agent.ask([{"question": "q", "options": ["a"]}])[0]["selections"], ["a"])

    def test_question_count_and_ask_refusal(self):
        with self.assertRaises(ValueError):
            self.agent.ask([])
        with self.assertRaises(ValueError):
            self.agent.ask([{"question": "q", "options": ["a"]}] * 5)
        self.replies["agent.ask"] = err("rejected", "HTTP 422: Full Auto may only ask about a blocker")
        with self.assertRaises(JaahRefused):
            self.agent.ask([{"question": "q", "header": "Decision", "options": ["a"]}])
        self.assertEqual(self.daemon.verbs(), ["agent.ask"])


class RetryTest(Base):
    def test_refusal_is_never_retried(self):
        self.replies["agent.notify"] = err("rejected", "HTTP 429: rate cap")
        with self.assertRaises(JaahRefused):
            self.agent.notify("x")
        self.replies["task.credential_list"] = err("bad_request")  # a read-only verb: still not retried
        with self.assertRaises(JaahRefused):
            self.agent.credentials()
        self.assertEqual(self.daemon.verbs(), ["agent.notify", "task.credential_list"])
        self.assertEqual(self._sleeps, [])

    def test_read_only_verb_retried_on_unavailable(self):
        seq = [err("unavailable"), err("unavailable"), ok({"credentials": []})]
        self.replies["task.credential_list"] = lambda a: seq.pop(0)
        self.assertEqual(self.agent.credentials(), [])
        self.assertEqual(self.daemon.verbs().count("task.credential_list"), 3)

    def test_write_verb_not_retried_on_unavailable(self):
        self.replies["task.settle"] = err("unavailable")
        with self.assertRaises(JaahUnavailable):
            self.agent.settle("s", "no_pr")
        self.assertEqual(self.daemon.verbs(), ["task.settle"])

    def test_connect_failure_retried_then_unavailable(self):
        wire = _wire.Wire(os.path.join(self.tmp, "absent.sock"), sleep=self._sleeps.append)
        with self.assertRaises(JaahUnavailable):
            wire.call("task.settle", {})
        self.assertEqual(len(self._sleeps), _wire.ATTEMPTS - 1)

    def test_closed_without_reply_is_unavailable(self):
        self.daemon.handler = lambda v, a: None
        with self.assertRaises(JaahUnavailable):
            self.agent.fail("x")


class MaskingTest(Base):
    def test_credential_value_never_leaves_in_clear(self):
        self.replies["task.credential_get"] = ok(CREDENTIAL)
        self.replies["agent.ask"] = ok({"ids": [1]})
        self.replies["agent.answers"] = ok({"rounds": [{"id": 1, "status": "answered", "selections": ["ok"],
                                                        "text": "ok"}], "all_resolved": True})
        self.agent.credential("acme/github")
        self.agent.log(f"using {SECRET}", token=SECRET, nested={"t": [SECRET]})
        self.agent.notify(f"token is {SECRET}")
        self.agent.ask([{"question": f"use {SECRET}?", "options": [SECRET]}])
        self.agent.settle(f"done with {SECRET}", "no_pr", no_pr_reason="no_code_needed",
                          no_pr_evidence=f"ran with rt_another_secret_99")
        self.agent.fail(f"failed {SECRET}")
        with open(self.events_path, encoding="utf-8") as fh:
            events_text = fh.read()
        sent = json.dumps(self.daemon.calls[1:])  # everything after the credential_get itself
        for blob in (events_text, sent):
            self.assertNotIn(SECRET, blob)
            self.assertNotIn("rt_another_secret_99", blob)
            self.assertIn("<redacted:acme/github>", blob)
        self.assertIn("https://api.github.com", json.dumps(CREDENTIAL))  # base_url is not masked
        self.agent.log("see https://api.github.com")
        self.assertEqual(self.events()[-1]["message"], "see https://api.github.com")


if __name__ == "__main__":
    unittest.main()
