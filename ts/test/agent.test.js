// Every §3.8 call against a fake daemon — the twin of python/tests/test_agent.py. Run: node --test 'test/*.test.js'
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";

import { Agent, JaahRefused, JaahUnavailable, NotARunner, connect } from "../index.js";
import { ATTEMPTS, Wire } from "../lib/wire.js";
import { FakeDaemon, err, ok } from "./fake_daemon.js";

const SECRET = "ghp_SuperSecretValue123";
const CREDENTIAL = {
  name: "acme/github", type: "github",
  config: { token: SECRET, base_url: "https://api.github.com", refresh_token: "rt_another_secret_99" },
  access_token: null, access_token_expires_at: null,
};

function withEnv(vars, fn) {
  const saved = {};
  for (const k of Object.keys(vars)) {
    saved[k] = process.env[k];
    if (vars[k] === undefined) delete process.env[k];
    else process.env[k] = vars[k];
  }
  const restore = () => {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  };
  return Promise.resolve().then(fn).finally(restore);
}

// One fake daemon + connected agent per test. `ctx.replies[verb]` is a reply object or (args) => reply.
function harness() {
  const ctx = { sleeps: [] };
  beforeEach(async () => {
    ctx.replies = { "task.get": ok({ id: 7 }) };
    ctx.daemon = await new FakeDaemon((verb, args) => {
      const r = ctx.replies[verb] ?? ok({});
      return typeof r === "function" ? r(args) : r;
    }).start();
    ctx.tmp = mkdtempSync(join(tmpdir(), "jaah-sdk-"));
    ctx.eventsPath = join(ctx.tmp, "events.jsonl");
    ctx.sleeps = [];
    ctx.agent = await connect({ socketPath: ctx.daemon.path, eventsPath: ctx.eventsPath });
    ctx.agent._wire._sleep = async (s) => { ctx.sleeps.push(s); };
    ctx.agent._sleep = async (s) => { ctx.sleeps.push(s); };
    ctx.daemon.calls.length = 0;
  });
  afterEach(() => ctx.daemon.close());
  ctx.events = () => existsSync(ctx.eventsPath)
    ? readFileSync(ctx.eventsPath, "utf8").trim().split("\n").map((l) => JSON.parse(l))
    : [];
  return ctx;
}

describe("connect", () => {
  test("no socket in env is NotARunner", async () => {
    await withEnv({ JAAH_AGENT_SOCKET: undefined }, async () => {
      await assert.rejects(connect(), NotARunner);
    });
  });

  test("daemon not_a_runner", async () => {
    const d = await new FakeDaemon(() => err("not_a_runner")).start();
    try {
      await assert.rejects(connect({ socketPath: d.path }), NotARunner);
      assert.deepEqual(d.verbs(), ["task.get"]);
    } finally {
      await d.close();
    }
  });

  test("socket from env", async () => {
    const d = await new FakeDaemon(() => ok({ id: 1 })).start();
    try {
      await withEnv({ JAAH_AGENT_SOCKET: d.path }, async () => {
        assert.ok((await connect()) instanceof Agent);
      });
    } finally {
      await d.close();
    }
  });
});

describe("calls", () => {
  const h = harness();

  test("task adds worktree and inputs", async () => {
    const inputs = join(h.tmp, "inputs.json");
    writeFileSync(inputs, JSON.stringify([{ kind: "prd", name: "prd.md", path: "/x/prd.md" }]));
    h.replies["agent.task"] = ok({
      task: { id: 7, title: "t", body: "b", persona: "coder", autonomous: true, out_of_scope: "" },
      project: { name: "p" }, agent: { id: "a" }, security_gate: "human",
    });
    const ctx = await withEnv({ JAAH_WORKTREE: "/w", JAAH_AGENT_INPUTS: inputs }, () => h.agent.task());
    assert.equal(ctx.worktree, "/w");
    assert.equal(ctx.inputs[0].kind, "prd");
    assert.equal(ctx.security_gate, "human");
    assert.deepEqual(h.daemon.calls, [["agent.task", {}]]);
  });

  test("log line", () => {
    h.agent.log("hello", "warning", { step: 2 });
    const [line] = h.events();
    assert.deepEqual(Object.keys(line), ["type", "timestamp", "level", "message", "data"]);
    assert.deepEqual([line.type, line.level, line.message, line.data], ["log", "warning", "hello", { step: 2 }]);
    assert.match(line.timestamp, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
    assert.throws(() => h.agent.log("x", "loud"), RangeError);
  });

  test("usage line shape is the contract", () => {
    h.agent.reportUsage("gpt-5-codex", 1, 2, 3, 4);
    const [line] = h.events();
    assert.deepEqual(Object.keys(line), ["type", "timestamp", "message"]);
    assert.deepEqual(Object.keys(line.message), ["id", "model", "usage"]);
    assert.deepEqual(Object.keys(line.message.usage),
      ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]);
    assert.match(line.message.id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.deepEqual(line.message.usage, {
      input_tokens: 1, output_tokens: 2, cache_read_input_tokens: 3, cache_creation_input_tokens: 4,
    });
  });

  test("notify", async () => {
    h.replies["agent.notify"] = ok({ ok: true });
    await h.agent.notify("halfway");
    assert.deepEqual(h.daemon.calls, [["agent.notify", { message: "halfway" }]]);
    assert.deepEqual(h.events().at(-1).detail, { length: 7 });
    await assert.rejects(h.agent.notify("x".repeat(2001)), RangeError);
  });

  test("credentials and credential", async () => {
    h.replies["task.credential_list"] = ok({ credentials: [{ name: "a", type: "github" }] });
    h.replies["task.credential_get"] = ok(CREDENTIAL);
    assert.equal((await h.agent.credentials())[0].name, "a");
    assert.equal((await h.agent.credential("acme/github")).config.token, SECRET);
    assert.deepEqual(h.daemon.calls.at(-1), ["task.credential_get", { name: "acme/github" }]);
    const sdk = h.events().at(-1);
    assert.deepEqual([sdk.type, sdk.event, sdk.detail], ["sdk", "credential_get", { name: "acme/github" }]);
  });

  test("credential refusals pass through", async () => {
    h.replies["task.credential_get"] = err("rejected", "HTTP 409: two credentials share that name");
    await assert.rejects(h.agent.credential("dup"), (e) => {
      assert.ok(e instanceof JaahRefused);
      assert.equal(e.reason, "rejected");
      assert.match(e.detail, /409/);
      return true;
    });
  });

  test("artifact sends an absolute path", async () => {
    await h.agent.artifact("report", "report.html");
    const [verb, args] = h.daemon.calls[0];
    assert.deepEqual([verb, args.kind], ["task.artifact", "report"]);
    assert.ok(isAbsolute(args.path));
  });

  test("readTask and searchTasks", async () => {
    await h.agent.readTask(42);
    await h.agent.searchTasks("sdk", { state: "done", hasArtifacts: true, limit: null });
    assert.deepEqual(h.daemon.calls, [
      ["task.read", { peer_task_id: 42 }],
      ["task.search", { q: "sdk", state: "done", has_artifacts: true }],
    ]);
  });

  test("settle", async () => {
    h.replies["task.settle"] = ok({ stored: true, settled: true, state: "done" });
    const res = await h.agent.settle("done", "merged_pr", { prUrl: "https://x/pull/1", reconMap: { files: [] } });
    assert.equal(res.settled, true);
    assert.deepEqual(h.daemon.calls, [["task.settle", {
      summary: "done", terminal_type: "merged_pr", pr_url: "https://x/pull/1", recon_map: { files: [] },
    }]]);
    assert.deepEqual(h.events().at(-1).detail, { terminal_type: "merged_pr", settled: true });
  });

  test("fail carries the session id", async () => {
    await withEnv({ CONTROL_RUNNER_SESSION_ID: "sess-1" }, () => h.agent.fail("boom"));
    assert.deepEqual(h.daemon.calls, [["task.fail", { reason: "boom", session_id: "sess-1" }]]);
  });
});

describe("ask", () => {
  const h = harness();
  const poll = (...replies) => {
    const seq = [...replies];
    h.replies["agent.answers"] = () => (seq.length > 1 ? seq.shift() : seq[0]);
  };

  test("blocks until all resolved, and dismissed is distinct", async () => {
    h.replies["agent.ask"] = ok({ ids: [11, 12] });
    const open = { id: 12, status: "open", selections: [], text: "" };
    const answered = { id: 11, status: "answered", selections: ["A", "C"], text: "A, C" };
    const dismissed = { id: 12, status: "dismissed", selections: [], text: "" };
    poll(ok({ rounds: [answered, open], all_resolved: false }),
      ok({ rounds: [answered, open], all_resolved: false }),
      ok({ rounds: [answered, dismissed], all_resolved: true }));
    const answers = await h.agent.ask([
      { question: "Which?", header: "Pick", options: ["A", "B", { label: "C", description: "c" }],
        multiSelect: true, recommendedIndex: 0 },
      { question: "Proceed?", options: ["yes", "no"] },
    ], { pollInterval: 2 });
    assert.deepEqual(answers, [
      { status: "answered", selections: ["A", "C"], text: "A, C" },
      { status: "dismissed", selections: [], text: "" },
    ]);
    assert.deepEqual(h.daemon.verbs(), ["agent.ask", "agent.answers", "agent.answers", "agent.answers"]);
    assert.deepEqual(h.sleeps, [2, 2]);
    assert.deepEqual(h.daemon.calls[0][1].questions[0], {
      question: "Which?", options: [{ label: "A" }, { label: "B" }, { label: "C", description: "c" }],
      multi_select: true, header: "Pick", recommended_index: 0,
    });
    assert.deepEqual(h.daemon.calls[1][1], { ids: [11, 12] });
  });

  test("timeout", async () => {
    h.replies["agent.ask"] = ok({ ids: [1] });
    poll(ok({ rounds: [{ id: 1, status: "open" }], all_resolved: false }));
    let t = 0;
    h.agent._clock = () => t;
    h.agent._sleep = async (s) => { t += s; };
    await assert.rejects(h.agent.ask([{ question: "q", options: ["a"] }], { pollInterval: 5, timeout: 12 }),
      (e) => e.name === "TimeoutError");
  });

  test("unavailable while polling keeps waiting", async () => {
    h.replies["agent.ask"] = ok({ ids: [1] });
    poll(err("unavailable"), err("unavailable"), err("unavailable"),
      ok({ rounds: [{ id: 1, status: "answered", selections: ["a"], text: "a" }], all_resolved: true }));
    const [answer] = await h.agent.ask([{ question: "q", options: ["a"] }]);
    assert.deepEqual(answer.selections, ["a"]);
  });

  test("question count and ask refusal", async () => {
    await assert.rejects(h.agent.ask([]), RangeError);
    await assert.rejects(h.agent.ask(Array(5).fill({ question: "q", options: ["a"] })), RangeError);
    h.replies["agent.ask"] = err("rejected", "HTTP 422: Full Auto may only ask about a blocker");
    await assert.rejects(h.agent.ask([{ question: "q", header: "Decision", options: ["a"] }]), JaahRefused);
    assert.deepEqual(h.daemon.verbs(), ["agent.ask"]);
  });
});

describe("retry", () => {
  const h = harness();

  test("a refusal is never retried", async () => {
    h.replies["agent.notify"] = err("rejected", "HTTP 429: rate cap");
    await assert.rejects(h.agent.notify("x"), JaahRefused);
    h.replies["task.credential_list"] = err("bad_request"); // a read-only verb: still not retried
    await assert.rejects(h.agent.credentials(), JaahRefused);
    assert.deepEqual(h.daemon.verbs(), ["agent.notify", "task.credential_list"]);
    assert.deepEqual(h.sleeps, []);
  });

  test("a read-only verb is retried on unavailable", async () => {
    const seq = [err("unavailable"), err("unavailable"), ok({ credentials: [] })];
    h.replies["task.credential_list"] = () => seq.shift();
    assert.deepEqual(await h.agent.credentials(), []);
    assert.equal(h.daemon.verbs().filter((v) => v === "task.credential_list").length, 3);
  });

  test("a write verb is not retried on unavailable", async () => {
    h.replies["task.settle"] = err("unavailable");
    await assert.rejects(h.agent.settle("s", "no_pr"), JaahUnavailable);
    assert.deepEqual(h.daemon.verbs(), ["task.settle"]);
  });

  test("a connect failure is retried, then unavailable", async () => {
    const sleeps = [];
    const wire = new Wire(join(h.tmp, "absent.sock"), { sleep: async (s) => { sleeps.push(s); } });
    await assert.rejects(wire.call("task.settle", {}), JaahUnavailable);
    assert.equal(sleeps.length, ATTEMPTS - 1);
  });

  test("closed without a reply is unavailable", async () => {
    h.daemon.handler = () => null;
    await assert.rejects(h.agent.fail("x"), JaahUnavailable);
  });
});

describe("masking", () => {
  const h = harness();

  test("a credential value never leaves in clear", async () => {
    h.replies["task.credential_get"] = ok(CREDENTIAL);
    h.replies["agent.ask"] = ok({ ids: [1] });
    h.replies["agent.answers"] = ok({ rounds: [{ id: 1, status: "answered", selections: ["ok"], text: "ok" }], all_resolved: true });
    await h.agent.credential("acme/github");
    h.agent.log(`using ${SECRET}`, "info", { token: SECRET, nested: { t: [SECRET] } });
    await h.agent.notify(`token is ${SECRET}`);
    await h.agent.ask([{ question: `use ${SECRET}?`, options: [SECRET] }]);
    await h.agent.settle(`done with ${SECRET}`, "no_pr", {
      noPrReason: "no_code_needed", noPrEvidence: "ran with rt_another_secret_99",
    });
    await h.agent.fail(`failed ${SECRET}`);
    const eventsText = readFileSync(h.eventsPath, "utf8");
    const sent = JSON.stringify(h.daemon.calls.slice(1)); // everything after the credential_get itself
    for (const blob of [eventsText, sent]) {
      assert.ok(!blob.includes(SECRET));
      assert.ok(!blob.includes("rt_another_secret_99"));
      assert.ok(blob.includes("<redacted:acme/github>"));
    }
    h.agent.log("see https://api.github.com"); // base_url is not a secret: not masked
    assert.equal(h.events().at(-1).message, "see https://api.github.com");
  });
});
