# Jaah Agent SDK

Plug **any** agent — a CLI coding tool, your own model loop, a non-coding bot — into Jaah's governance
pipeline. Jaah runs it on a node against a task and handles the logging, notifications, human approvals,
credentials and usage metrics; your code does the work.

Two packages, one API:

| | Python | TypeScript |
|---|---|---|
| Package | `jaah_agent` (`python/jaah_agent`) | `@jaah/agent-sdk` (`ts/`) |
| Runtime | Python ≥ 3.12, **stdlib only** | Node ≥ 22.18, **no dependencies**, no build step |
| Names | `snake_case` | `camelCase`, every call `async` except `log`/`reportUsage` |

The version is in [`VERSION`](VERSION). Private for now: not published to PyPI or npm.

## Quick start

An agent is a git repo with a `jaah-agent.toml` manifest and an entrypoint.

### 1. The manifest — `jaah-agent.toml` at the agent root

```toml
[agent]
name         = "hello-agent"          # slug: ^[a-z][a-z0-9-]{1,39}$, unique in your org
display_name = "Hello agent"          # 1..60 chars, shown everywhere in Jaah
description  = "Says hello, writes a report, settles the task."   # ≤ 280 chars
type         = "non_coding"           # coding | non_coding | cli_wrapper
language     = "python"               # python | typescript
entrypoint   = "main.py"              # relative to the agent root, no '..'
version      = "0.1.0"                # semver
maintainer   = "@your-github-handle"  # or "Name <email>"
personas     = ["coder"]              # which task kinds it may run: coder, business_analysis,
                                      # architecture, plan, finish, researcher
sdk          = ">=0.1,<0.2"           # the SDK versions it works with

[runtime]                              # optional
install      = true                   # python: requirements.txt / pyproject.toml; ts: package-lock.json → npm ci
```

The agent root is the repo root, or a sub-path you name when you register it.

### 2. A hello agent — Python (`main.py`)

```python
import html, os
import jaah_agent

agent = jaah_agent.connect()                 # raises NotARunner when not launched by a Jaah node
ctx = agent.task()
task = ctx["task"]
agent.log("starting", task_id=task["id"], persona=task["persona"])

if not task["autonomous"]:                    # Full Auto: decide yourself, never ask
    answers = agent.ask([{                    # blocks until the operator answers or dismisses
        "question": f"Say hello for task {task['id']}?",
        "header": "Confirm",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }])
    if answers[0]["status"] == "dismissed" or answers[0]["selections"] != ["Yes"]:
        agent.settle("The operator declined the hello.", "no_pr",
                     no_pr_reason="no_code_needed", no_pr_evidence="Operator answered No / dismissed.")
        raise SystemExit(0)

agent.notify(f"Hello from {ctx['agent']['display_name']}!")
agent.report_usage("my-model-1", input_tokens=120, output_tokens=40)

report = os.path.join(os.environ["JAAH_REPORT_DIR"], "report.html")
with open(report, "w", encoding="utf-8") as fh:
    fh.write(f"<!doctype html><title>Hello</title><p>Hello, {html.escape(task['title'])}.</p>")
agent.artifact("report", report)

agent.settle("Said hello and uploaded a report.", "no_pr",
             no_pr_reason="no_code_needed", no_pr_evidence="Hello agent: no code change requested.")
```

Under **Full Auto** (`task["autonomous"]` true) the agent makes every decision itself. Jaah then accepts
a question only about a blocker a human must clear (a missing credential or access), with `header` one of
`Credential`, `Access`, `Human act`, `External`; any other header is refused (`JaahRefused`).

### 2b. The same agent — TypeScript (`main.js`, `language = "typescript"`)

```js
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { connect } from "@jaah/agent-sdk";

const agent = await connect();
const ctx = await agent.task();
const { task } = ctx;
agent.log("starting", "info", { taskId: task.id });

if (!task.autonomous) {                       // Full Auto: decide yourself, never ask
  const [answer] = await agent.ask([{         // waits until the operator answers or dismisses
    question: `Say hello for task ${task.id}?`,
    header: "Confirm",
    options: [{ label: "Yes" }, { label: "No" }],
  }]);
  if (answer.status === "dismissed" || answer.selections[0] !== "Yes") {
    await agent.settle("The operator declined the hello.", "no_pr", {
      noPrReason: "no_code_needed", noPrEvidence: "Operator answered No / dismissed.",
    });
    process.exit(0);
  }
}

await agent.notify(`Hello from ${ctx.agent.display_name}!`);
agent.reportUsage("my-model-1", 120, 40);

const report = join(process.env.JAAH_REPORT_DIR, "report.html");
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
writeFileSync(report, `<!doctype html><title>Hello</title><p>Hello, ${esc(task.title)}.</p>`);
await agent.artifact("report", report);

await agent.settle("Said hello and uploaded a report.", "no_pr", {
  noPrReason: "no_code_needed", noPrEvidence: "Hello agent: no code change requested.",
});
```

The entrypoint may be `.js`, `.mjs`, or an erasable-syntax `.ts` file (Node ≥ 22.18 strips types itself).

### 3. Run it

1. Push the repo to GitHub where your Jaah GitHub connection can read it.
2. In the Jaah console, register it under **Agents** (repo URL, ref, optional sub-path). Jaah validates
   the manifest and pins the commit; refresh it after you push changes.
3. Pick the agent on a task (or make it the project's default agent) and start the task. A node runs the
   entrypoint in the task's worktree and the task's activity, questions and usage show up like any runner's.

## Two rules your agent must follow

- **Don't daemonize.** Jaah identifies your task by the process tree it launched: stay a (grand)child of
  the entrypoint. A process that double-forks, calls `setsid` to escape, or is handed to `nohup`/systemd
  gets `NotARunner` on every call. Child processes you spawn and wait for are fine.
- **Never add the SDK to your dependencies.** The node provides it (Python on `PYTHONPATH`, TS as
  `node_modules/@jaah/agent-sdk`), always the version it was built for. A `jaah_agent` / `jaah-agent` /
  `@jaah/agent-sdk` entry in `requirements.txt`, `pyproject.toml` or `package.json` is refused at launch.

## API

Every call talks to the node daemon over the unix socket at `$JAAH_AGENT_SOCKET` — no token, no task id.

| Python | TypeScript | What it does |
|---|---|---|
| `connect()` | `await connect()` | Attach to the node; raises `NotARunner` if it cannot identify this process. |
| `agent.task()` | `await agent.task()` | `{task:{id,title,body,persona,autonomous,out_of_scope}, project:{…}, agent:{id,display_name,version,commit}, security_gate, worktree, inputs}`. `inputs` = `[{kind,name,path}]` input documents (e.g. a `prd`). |
| `agent.log(message, level="info", **data)` | `agent.log(message, level?, data?)` | A line in the session transcript. Levels: `debug`, `info`, `warning`, `error`. |
| `agent.report_usage(model, input_tokens, output_tokens, cache_read=0, cache_write=0)` | `agent.reportUsage(model, inputTokens, outputTokens, cacheRead?, cacheWrite?)` | Record one model call's tokens; priced on the session's Efficiency view. |
| `agent.notify(message)` | `await agent.notify(message)` | Post an info message (≤ 2000 chars) to the task's console + Slack channel. Capped at 30/hour. |
| `agent.ask(questions, poll_interval=5, timeout=None)` | `await agent.ask(questions, {pollInterval?, timeout?})` | Ask 1..4 questions and **block** until all are resolved. Returns one `Answer` per question. |
| `agent.credentials()` | `await agent.credentials()` | The task's credential roster: names, types, descriptions — no secrets. |
| `agent.credential(name)` | `await agent.credential(name)` | One credential's config, secrets included (`404` unknown, `409` ambiguous name → `JaahRefused`). |
| `agent.artifact(kind, path)` | `await agent.artifact(kind, path)` | Upload a deliverable: `report` (.html), `prd`/`architecture` (.md), `file` (anything). Write it under `$JAAH_REPORT_DIR`. |
| `agent.read_task(task_id)` / `agent.search_tasks(q, **filters)` | `readTask(taskId)` / `searchTasks(q, filters?)` | Read or search other tasks in the project. |
| `agent.settle(summary, terminal_type, *, pr_url=None, no_pr_reason=None, no_pr_evidence=None, **gate)` | `settle(summary, terminalType, {prUrl?, noPrReason?, noPrEvidence?, ...gate})` | **The** terminal call: `merged_pr` (with `pr_url`) or `no_pr` (with a reason + evidence). Make it last — the process is ended soon after. |
| `agent.fail(reason)` | `await agent.fail(reason)` | Give up: only for what no human could have cleared. Also terminal. |

A question is `{question, header?, options: [{label, description?}] (or plain strings), multi_select?,
recommended_index?}` (TS: `multiSelect`, `recommendedIndex`). An `Answer` is
`{status: "answered" | "dismissed", selections: [labels], text}`. A **dismissed** round has
`status: "dismissed"` and no selections — it is never an empty "answered". `ask` raises `TimeoutError`
when `timeout` seconds pass first.

### Errors

| Error | Meaning | Retry? |
|---|---|---|
| `JaahUnavailable` | No answer from the daemon, or the daemon could not reach Jaah. | Yes, later. The SDK already retried what is safe: a connect failure for any call, and any outage for a read-only call. A write (`settle`, `fail`, `notify`, `ask`, `artifact`) is not retried after a lost answer, because it may have landed — check with `task()` before repeating. |
| `JaahRefused(reason, detail)` | Jaah answered no: `reason` is the coarse code, `detail` Jaah's explanation (e.g. `HTTP 422 …`). | **Never** — the same request earns the same answer. Fix it. |
| `NotARunner` | The daemon cannot tell which task this process is (see "Don't daemonize"). | No. |

All three subclass `JaahError`.

### The security gate

`task()["security_gate"]` is `"human"` for every SDK agent today: no automated reviewer stands between
your agent and a merge. When your change touches sign-in/auth, credentials/secrets, data migrations or
personal data, **never merge it and never enable auto-merge**. Instead:

- `task.autonomous` false → `ask` ONE question, header `Security`, options approve / do-not-merge. Merge
  only on approve; otherwise settle leaving the PR open.
- `task.autonomous` true → open the PR as a **draft** and `fail` with a reason that names the security
  surface first and carries the PR URL.

## What Jaah records

- **Transcript.** `log`, `report_usage` and the SDK's own call events are appended to the session's events
  file (`$JAAH_AGENT_EVENTS`), one JSON line each, every line with `timestamp` and `type`:
  `log` `{level, message, data}`, `usage` `{message: {id, model, usage: {input_tokens, output_tokens,
  cache_read_input_tokens, cache_creation_input_tokens}}}`, `sdk` `{event, detail}`. The node adds your
  stdout/stderr. Without `$JAAH_AGENT_EVENTS` (a local dry run) lines go to stderr.
- **Secrets are masked.** Every secret value `credential()` returned is replaced with
  `<redacted:<credential name>>` in everything the SDK writes or sends: log lines and data, `notify`,
  `ask`, `settle` (every field) and `fail` text, and every events-file line. `sdk` events carry names and
  counts, never values. Masking matches exact values of 6+ characters only: never print an encoded or
  partial secret yourself, and never put one in a file you upload with `artifact` — file contents are not
  masked.

## Developing the SDK

```sh
python3 -m unittest discover -s python/tests     # Python suite (fake daemon)
(cd ts && node --test 'test/*.test.js')                     # TypeScript suite (fake daemon)
python3 -m unittest discover -s tests            # parity: every Python call has a TS twin
```

The wire is one JSON line each way: `{"call": "<verb>", "args": {...}}` →
`{"ok": true, "result": ...}` or `{"ok": false, "error": "<reason>", "detail": "..."}`. Verbs:
`agent.task`, `agent.ask`, `agent.answers`, `agent.notify`, `task.settle`, `task.fail`, `task.artifact`,
`task.credential_list`, `task.credential_get`, `task.get`, `task.read`, `task.search`.

Release: bump `VERSION` and `ts/package.json` together (the parity suite checks they match), merge to
`develop`, tag `vX.Y.Z`. Jaah vendors the SDK into its node package from a tag.
