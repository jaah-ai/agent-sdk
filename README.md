# Jaah Agent SDK v2

Write an agent that runs on [Jaah](https://jaah.ai). An SDK v2 agent is an
[Agent Client Protocol](https://agentclientprotocol.com) (ACP) agent: you implement one prompt handler and
emit updates; the library speaks the protocol and the `jaah.ai` extension — settling the task, asking a
human, fetching credentials.

| Directory | What |
|---|---|
| [`spec/`](spec/README.md) | The `jaah.ai` ACP extension: the [spec page](spec/README.md) and its machine-readable [`jaah-extension.json`](spec/jaah-extension.json) |
| `python/` | `jaah-agent-sdk`, imported as `jaah_agent` (Python ≥ 3.11), built on [`agent-client-protocol`](https://pypi.org/project/agent-client-protocol/) |
| `typescript/` | `@jaah/agent-sdk` (Node ≥ 22), built on [`@agentclientprotocol/sdk`](https://www.npmjs.com/package/@agentclientprotocol/sdk) |
| `conformance/` | `jaah-conformance`: checks any ACP agent against the spec, profiles `acp-core` and `jaah-extension` |
| `examples/` | One reference agent per language |

## Write an agent

You write `prompt`. The library owns `initialize`, `session/new`, `session/cancel`, `authenticate`, the
extension negotiation, and every extension call.

**Python** ([`examples/python/agent.py`](examples/python/agent.py)):

```python
import jaah_agent

class Echo(jaah_agent.Agent):
    name = "jaah-echo"

    async def prompt(self, turn: jaah_agent.Turn) -> None:
        await turn.say(f"You said: {turn.text}")
        await turn.complete("Echoed the prompt.", terminal_type="no_pr", no_pr_reason="no_code_needed",
                            no_pr_evidence="echoed the prompt back; writes no code")

jaah_agent.run(Echo())
```

**TypeScript** ([`examples/typescript/agent.ts`](examples/typescript/agent.ts)):

```ts
import { run, type Turn } from "@jaah/agent-sdk";

run({
  info: { name: "jaah-echo", version: "1.0.0" },
  async prompt(turn: Turn) {
    await turn.say(`You said: ${turn.text}`);
    await turn.complete("Echoed the prompt.", "no_pr", {
      no_pr_reason: "no_code_needed", no_pr_evidence: "echoed the prompt back; writes no code" });
  },
});
```

The agent reads ACP on stdin and writes it on stdout; anything it prints for humans goes to stderr.
The endings a settle may declare are the task's persona's: a `coder` task in a repository takes `merged_pr` or
`no_pr`, one in a folder slot only `outcome`. A refused settle raises `ExtensionError` (its `data.details` names
the allowed endings) and leaves the session unsettled, so the agent may settle again; the reference agents fall
back from `no_pr` to `outcome` that way.

### The turn

| Python | TypeScript | Does |
|---|---|---|
| `turn.text`, `turn.prompt` | same | The prompt's text, and its raw ACP content blocks |
| `turn.session_id`, `turn.cwd`, `turn.meta` | `sessionId`, `cwd`, `meta` | The session; `meta` is `session/new`'s `_meta["jaah.ai"]` run context |
| `await turn.say(text)` / `think(text)` | same | Emit an `agent_message_chunk` / `agent_thought_chunk` |
| `await turn.update(update)` | same | Emit any `session/update` (tool calls, plans, …) |
| `await turn.complete(summary, terminal_type, **refs)` | `complete(summary, terminalType, refs)` | Settle the task — once, last |
| `await turn.fail(reason)` | same | Give up on the task — instead of `complete` |
| return a stop reason | resolve one | `end_turn` when you return nothing |
| cancellation: the handler's task is cancelled | `turn.signal` aborts | On `session/cancel`; the turn ends `cancelled` |

### The extension: `turn.jaah`

| Python | TypeScript | Method |
|---|---|---|
| `jaah.available`, `jaah.supports(verb)` | same | Whether the client advertised the extension / one verb |
| `await jaah.ask(question, options=…, header=…)` | `ask(question, {options, header, multiSelect})` | `_jaah.ai/input/request` |
| `await jaah.credentials()` | same | `_jaah.ai/credential/list` (no secrets) |
| `await jaah.credential(name)` | same | `_jaah.ai/credential/get` (secrets included — never log it) |
| `await jaah.credential_file(name)` | `credentialFile(name)` | `credential/get`, written `0600` in a private directory; returns the path |
| `await jaah.call(verb, params)` | `call(verb, params)` | Any advertised verb, e.g. `artifact.upload` |

Rules the library enforces for you: a method the client did not advertise raises `ExtensionUnavailable`
and is never sent; a settle after an accepted one (or while one is in flight) raises `AlreadySettled`,
while a refused one may be sent again; a refusal raises `ExtensionError` with the JSON-RPC code. Settling
first removes every credential file the session wrote; after a refused settle, call `credential_file`
again for a key you still need. Under a plain ACP client (no extension) `complete` and `fail` do nothing and return
nothing, so the same agent runs anywhere. Hand a key to a tool as the file `credential_file` writes —
never put it in an environment variable.

## Run it on Jaah

An org registers the agent's repo (and sub-path) in the console; the control plane snapshots it at a pinned
commit. Its `jaah-agent.toml` is manifest 2 — `manifest = 2` and the `command` to run from the agent's
folder as the engine of the node's ACP host; each reference agent carries one
([Python](examples/python/jaah-agent.toml), [TypeScript](examples/typescript/jaah-agent.toml)). No third-party
file travels in the folder: it carries the SDK and lists its dependencies, hash-pinned, and before the host starts
`command` the node installs them into the folder (`pip install --require-hashes --only-binary=:all: --target` from
`requirements.txt`, `npm ci --omit=dev --ignore-scripts` from `package-lock.json`; a failed install fails the
attempt as `[agent_install_failed]`). The node needs reach to PyPI and the npm registry. A Python agent carries
`jaah_agent` and `requirements.txt` (the SDK's runtime dependencies, from `constraints.txt`) beside its script — or
a `requirements.txt` naming `jaah-agent-sdk`, which installs the SDK itself; a TypeScript agent runs `agent.mjs`,
`agent.ts` bundled with the SDK inlined and its dependencies left as imports, beside a `package.json` listing them
under `dependencies` and its `package-lock.json`. Registration refuses a folder whose script imports the SDK
without them. `examples/vendor.py` generates both reference agents' folders from this directory (given agent
folders, it generates each one's the same way, by its manifest's `language` and `command`; `--install` installs a
folder's dependencies into it as a node does); its output is not committed (`.gitignore`): `check.sh` generates it
before the suites run, and each release generates it into the published tree, so the published folders are
registrable.
The task is the first prompt.

## Check an agent: conformance

The suite runs any ACP agent — written with this SDK or not — and needs only this directory:

```sh
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r constraints.txt
.venv/bin/pip install --no-index --no-deps --no-build-isolation -e conformance
.venv/bin/jaah-conformance --profile acp-core -- path/to/any-acp-agent --its-acp-flags
.venv/bin/python examples/vendor.py   # the reference agent's folder is generated, not committed
.venv/bin/python examples/vendor.py --install examples/python   # its dependencies, as a node installs them
.venv/bin/jaah-conformance --profile jaah-extension -- python3 examples/python/agent.py
```

`--profile acp-core` checks a plain ACP agent runs; `jaah-extension` adds the extension checks. Each check
is listed in the [spec page](spec/README.md#conformance). `--json` prints a machine-readable report;
`--spec FILE` points at a spec elsewhere; exit status 0 means every check passed.

## Build and test

```sh
./check.sh
```

It installs the pinned dependencies (`constraints.txt`, hash-checked; the npm lockfiles) into `.venv` and
`node_modules`, runs the spec drift check, the Python and TypeScript suites, generates both reference agents'
folders (`examples/vendor.py`), runs the conformance suite against them (each from a copy of its folder alone,
its dependencies installed as a node installs them),
and builds the wheels, sdists and the npm tarball. Needs `python3` ≥ 3.11 and
Node ≥ 22.18. The spec page's method table is generated: `python3 spec/render.py` rewrites it from the
JSON.

## Licence and releases

Licensed under [Apache-2.0](LICENSE). Published as the repository `jaah-ai/agent-sdk`, npm `@jaah/agent-sdk`,
and PyPI `jaah-agent-sdk` and `jaah-acp-conformance`. Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md).

Each release is one commit on `main`, tagged with its version. The spec JSON is generated, never edited by hand.
