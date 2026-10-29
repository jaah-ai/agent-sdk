// @jaah/agent-sdk: the extension's negotiation, settle-once, credentials as files, and turn cancellation.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync, statSync, existsSync } from "node:fs";
import { dirname } from "node:path";
import { Readable, Writable } from "node:stream";
import { test } from "node:test";
import * as acp from "@agentclientprotocol/sdk";

import { AlreadySettled, Extension, ExtensionError, ExtensionUnavailable, methodFor, negotiate, verbFor } from "../dist/index.js";

const ALL = ["_jaah.ai/task/complete", "_jaah.ai/task/fail", "_jaah.ai/credential/get", "_jaah.ai/input/request"];

function extension(methods = ALL, answers = {}) {
  const sent = [];
  const ext = new Extension(async (method, params) => {
    sent.push([method, params]);
    return answers[method] ?? { ok: true };
  }, new Set(methods));
  return { ext, sent };
}

test("verbs map to ACP extension methods", () => {
  assert.equal(methodFor("pull_request.enqueue"), "_jaah.ai/pull_request/enqueue");
  assert.equal(verbFor("_jaah.ai/credential/get"), "credential.get");
  assert.throws(() => methodFor("task/complete"));
});

test("negotiation takes only a v1 offer", () => {
  assert.equal(negotiate(undefined).size, 0);
  assert.equal(negotiate({ "jaah.ai": { version: 2, methods: ALL } }).size, 0);
  assert.equal(negotiate({ "jaah.ai": { task_id: 7 } }).size, 0);
  assert.deepEqual([...negotiate({ "jaah.ai": { version: 1, methods: [...ALL, "_other/x", 3] } })].sort(), [...ALL].sort());
});

test("settles once and never sends an unadvertised method", async () => {
  const { ext, sent } = extension();
  await ext.complete("done", "outcome", { pr_url: "https://example.invalid/1" });
  await assert.rejects(ext.fail("again"), AlreadySettled);
  await assert.rejects(ext.call("memory.search", { query: "x" }), ExtensionUnavailable);
  assert.deepEqual(sent, [
    ["_jaah.ai/task/complete", { summary: "done", terminal_type: "outcome", pr_url: "https://example.invalid/1" }],
  ]);
});

test("a refused or unmoved settle may be sent again", async () => {
  let attempts = 0;
  const ext = new Extension(async (method) => {
    attempts += 1;
    if (attempts === 1) throw new ExtensionError(method, -32000, "refused: upload the report first");
    return { settled: attempts === 3 };
  }, new Set(ALL));
  await assert.rejects(ext.complete("done", "outcome"), ExtensionError);
  assert.deepEqual(await ext.complete("done", "outcome"), { settled: false });
  assert.equal(ext.settled, false);
  await ext.complete("done", "outcome");
  assert.equal(ext.settled, true);
  await assert.rejects(ext.complete("done", "outcome"), AlreadySettled);
  assert.equal(attempts, 3);
});

test("settle is a no-op under a plain ACP client", async () => {
  const { ext, sent } = extension([]);
  assert.equal(ext.available, false);
  assert.equal(await ext.complete("done", "outcome"), undefined);
  assert.deepEqual(sent, []);
});

test("ask sends input.request", async () => {
  const { ext, sent } = extension(ALL, { "_jaah.ai/input/request": { answer: "B" } });
  assert.deepEqual(await ext.ask("Which?", { options: ["A", "B"], multiSelect: false }), { answer: "B" });
  assert.deepEqual(sent, [["_jaah.ai/input/request", { question: "Which?", options: ["A", "B"], multi_select: false }]]);
});

test("a credential arrives as a private file, never the environment", async () => {
  const config = { name: "x/y", config: { api_key: "placeholder" } };
  const { ext } = extension(ALL, { "_jaah.ai/credential/get": config });
  const before = { ...process.env };
  const path = await ext.credentialFile("x/y");
  assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), config);
  assert.equal(statSync(path).mode & 0o777, 0o600);
  assert.equal(statSync(dirname(path)).mode & 0o777, 0o700);
  assert.deepEqual({ ...process.env }, before);
  assert.notEqual(await ext.credentialFile("x_y"), path);
  await ext.complete("done", "outcome");
  assert.equal(existsSync(dirname(path)), false);
});

const SLOW_AGENT = `
import { run } from "${new URL("../dist/index.js", import.meta.url).href}";
run({ async prompt(turn) {
  await turn.say("working");
  await new Promise((resolve) => turn.signal.addEventListener("abort", resolve));
} });
`;

test("session/cancel ends the turn cancelled", async () => {
  const child = spawn(process.execPath, ["--input-type=module", "-e", SLOW_AGENT], { stdio: ["pipe", "pipe", "inherit"] });
  let sawUpdate;
  const updated = new Promise((resolve) => (sawUpdate = resolve));
  const conn = new acp.ClientSideConnection(
    () => ({ async sessionUpdate() { sawUpdate(); }, async requestPermission() { throw acp.RequestError.methodNotFound(""); } }),
    acp.ndJsonStream(Writable.toWeb(child.stdin), Readable.toWeb(child.stdout)),
  );
  try {
    await conn.initialize({ protocolVersion: 1, clientCapabilities: {} });
    const { sessionId } = await conn.newSession({ cwd: ".", mcpServers: [] });
    const turn = conn.prompt({ sessionId, prompt: [{ type: "text", text: "go" }] });
    await updated;
    await conn.cancel({ sessionId });
    assert.equal((await turn).stopReason, "cancelled");
  } finally {
    child.kill();
  }
});
