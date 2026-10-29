"""Make an agent's folder carry the SDK it imports and name the dependencies the node installs (manifest 2).

    python examples/vendor.py                    generate both reference agents' folders
    python examples/vendor.py AGENT_DIR ...      generate each agent's folder, by its manifest `language`
    python examples/vendor.py --install DIR ...  install each folder's dependencies into it, as a node does

The output is not committed (`.gitignore`): `check.sh` generates the reference agents' before the conformance suite
runs, each release generates it into the published tree, so a published agent folder is registrable while the source
carries only the files an author writes. No third-party file is ever copied into a folder: the node installs the
dependencies a folder names into it before it starts `command` (the node daemon's `acp_registry_install`), and
`--install` does the same for a local run or a test.

The SDK goes where the agent's `command` (its `jaah-agent.toml`) runs it from:

Python: beside the first `.py` word of `command`, `jaah_agent` copied from `python/src` and `requirements.txt`: the
SDK's runtime dependencies at the versions and hashes `constraints.txt` pins (the closure over every interpreter and
machine in `TARGETS`). The node runs `pip install --require-hashes --only-binary=:all: --target` on it, which puts
them beside the script; Python puts the script's folder first on `sys.path`.

TypeScript: the first `.mjs` / `.js` word of `command` is written by esbuild from the `.ts` source of the same name
beside it, with the built `typescript/` package inlined and its dependencies left as imports. The folder's own
`package.json` (with its `package-lock.json`) must list each of them under `dependencies`; the node runs
`npm ci --omit=dev` on it. Needs `npm ci` in `typescript/` (built) and in `examples/typescript/` (esbuild).

Run it with a Python that has pip (check.sh's `.venv/bin/python`); it downloads through pip's cache.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
#: The reference agents, generated when no folder is named.
REFERENCE_AGENTS = [SDK / "examples" / "python", SDK / "examples" / "typescript"]
#: esbuild, from the TypeScript reference agent's own install.
ESBUILD = SDK / "examples" / "typescript" / "node_modules" / ".bin" / "esbuild"
#: The interpreters (the node's floor, `python3 >= 3.12`, up to the latest) and Linux machines a node runs.
TARGETS = [(py, plat) for py in ("3.12", "3.13", "3.14")
           for plat in ("manylinux2014_x86_64", "manylinux2014_aarch64")]
ROOT_REQUIREMENT = "agent-client-protocol"
_SKIP = ("__pycache__", "node_modules")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, str]:
    """`constraints.txt` as {normalised name: its whole block, `name==version` plus every `--hash` line}."""
    blocks: dict[str, str] = {}
    current = ""
    for line in (SDK / "constraints.txt").read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if not line.startswith(" "):
            current = _norm(line.split("==", 1)[0])
            blocks[current] = ""
        blocks[current] += line + "\n"
    return blocks


def _download(names: list[str], pins: dict[str, str], python: str, platform: str, into: Path) -> list[Path]:
    reqs = into / "requirements.txt"
    reqs.write_text("".join(pins[n] for n in names))
    abi = "cp" + python.replace(".", "")
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check", "--no-deps",
         "--require-hashes", "--only-binary=:all:", "--implementation", "cp", "--python-version", python,
         "--abi", abi, "--platform", platform, "--dest", str(into / "wheels"), "-r", str(reqs)],
        check=True,
    )
    return sorted((into / "wheels").glob("*.whl"))


def _requires(wheel: zipfile.ZipFile) -> set[str]:
    """The names a wheel's METADATA requires outside any extra (a version marker is kept: over-including is safe)."""
    meta = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
    names = set()
    for line in wheel.read(meta).decode().splitlines():
        if line.startswith("Requires-Dist:") and not re.search(r"\bextra\s*==", line):
            names.add(_norm(re.split(r"[\s;\[<>=!~(]", line.split(":", 1)[1].strip(), maxsplit=1)[0]))
    return names


def requirements() -> str:
    """`requirements.txt` for a Python agent: every runtime dependency of the SDK, each with all its pinned hashes."""
    pins = _pins()
    names = [_norm(ROOT_REQUIREMENT)]
    with tempfile.TemporaryDirectory() as tmp:
        for i, (python, platform) in enumerate(TARGETS):
            target = Path(tmp) / f"t{i}"
            target.mkdir()
            while True:  # grow `names` to the runtime closure on this target
                needed = set(names)
                for path in _download(names, pins, python, platform, target):
                    with zipfile.ZipFile(path) as wheel:
                        needed |= _requires(wheel)
                missing = needed - pins.keys()
                if missing:
                    sys.exit(f"constraints.txt pins no {', '.join(sorted(missing))}, which the SDK needs at runtime")
                if needed <= set(names):
                    break
                names = sorted(needed)
                shutil.rmtree(target / "wheels")
    header = ("# Generated by the Agent SDK's examples/vendor.py from its constraints.txt: the SDK's runtime dependencies,\n"
              "# hash-pinned. The node installs them beside the agent before it starts `command`. Do not edit.\n")
    return header + "".join(pins[n] for n in sorted(names))


def _script(agent: Path, suffixes: tuple[str, ...]) -> tuple[Path, list[str]]:
    """The file `command` runs (its first word ending in one of `suffixes`), and the manifest's `command`."""
    try:
        with open(agent / "jaah-agent.toml", "rb") as fh:
            command = tomllib.load(fh)["agent"]["command"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as err:
        sys.exit(f"{agent}: no manifest 2 `command` in jaah-agent.toml ({err})")
    for word in command[1:] if isinstance(command, list) else ():
        if isinstance(word, str) and word.endswith(suffixes):
            path = (agent / word).resolve()
            if not path.is_relative_to(agent.resolve()):
                sys.exit(f"{agent}: `command` runs {word}, outside the agent's folder")
            return path, command
    sys.exit(f"{agent}: `command` {shlex.join(map(str, command))} runs no {' or '.join(suffixes)} file")


def _language(agent: Path) -> str:
    try:
        with open(agent / "jaah-agent.toml", "rb") as fh:
            return str(tomllib.load(fh)["agent"]["language"])
    except (OSError, tomllib.TOMLDecodeError, KeyError) as err:
        sys.exit(f"{agent}: no `language` in jaah-agent.toml ({err})")


def place_python(requirements_txt: str, agent: Path) -> None:
    """Replace the `jaah_agent` and `requirements.txt` an earlier run wrote beside the agent's script."""
    script, _ = _script(agent, (".py",))
    target = script.parent
    shutil.rmtree(target / "jaah_agent", ignore_errors=True)
    src = SDK / "python" / "src"
    shutil.copytree(src / "jaah_agent", target / "jaah_agent", ignore=shutil.ignore_patterns(*_SKIP))
    (target / "requirements.txt").write_text(requirements_txt)


def build_typescript(agent: Path) -> None:
    script, _ = _script(agent, (".mjs", ".js"))
    source = script.with_suffix(".ts")
    if not source.is_file():
        sys.exit(f"{agent}: `command` runs {script.name}, generated from {source.name}, which is missing")
    if not ESBUILD.exists():
        sys.exit(f"{ESBUILD} is missing: run `npm ci` in typescript/ (and build it) and in examples/typescript/")
    sdk_deps = sorted(json.loads((SDK / "typescript" / "package.json").read_text()).get("dependencies") or {})
    try:
        declared = json.loads((source.parent / "package.json").read_text()).get("dependencies") or {}
    except (OSError, ValueError):
        declared = {}
    undeclared = [dep for dep in sdk_deps if dep not in declared]
    if undeclared or not (source.parent / "package-lock.json").is_file():
        sys.exit(f"{agent}: its package.json (with a package-lock.json) must list the SDK's dependencies under "
                 f"`dependencies` — the node installs them with `npm ci --omit=dev`; missing: "
                 f"{', '.join(undeclared) or 'package-lock.json'}")
    banner = (f"// Generated by the Agent SDK's examples/vendor.py from {source.name} and the SDK it imports, "
              "inlined (its dependencies stay imports, installed from package.json). Do not edit.")
    subprocess.run(
        [str(ESBUILD), source.name, "--bundle", "--platform=node", "--format=esm", "--target=node22",
         f"--alias:@jaah/agent-sdk={SDK / 'typescript' / 'dist' / 'index.js'}", "--log-level=warning",
         *[f"--external:{dep}" for dep in sdk_deps], f"--banner:js={banner}", f"--outfile={script}"],
        cwd=source.parent, check=True,
    )


def install(agent: Path) -> None:
    """Install what the folder names beside its script, as the node does before it starts `command`."""
    script, _ = _script(agent, (".py", ".mjs", ".js"))
    here = script.parent
    if (here / "requirements.txt").is_file():
        # This interpreter, which has pip: run the agent with the same Python version, as its native wheels need.
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-input",
                        "--require-hashes", "--only-binary=:all:", "--target", str(here),
                        "-r", str(here / "requirements.txt")], check=True)
    if (here / "package-lock.json").is_file():
        subprocess.run(["npm", "ci", "--prefix", str(here), "--omit=dev", "--ignore-scripts", "--no-audit",
                        "--no-fund", "--loglevel=error"], check=True)


def main(argv: list[str]) -> int:
    installing = argv[:1] == ["--install"]
    args = argv[1:] if installing else argv
    if any(arg.startswith("-") for arg in args) or (installing and not args):
        sys.exit(f"usage: {sys.argv[0]} [AGENT_DIR ...] (none: both reference agents' folders) | "
                 "--install AGENT_DIR ...")
    agents = [Path(arg).resolve() for arg in args] or REFERENCE_AGENTS
    if installing:
        for agent in agents:
            install(agent)
        return 0
    languages = {agent: _language(agent) for agent in agents}
    unknown = sorted(f"{agent} ({language})" for agent, language in languages.items()
                     if language not in ("python", "typescript"))
    if unknown:
        sys.exit(f"nothing to generate for {', '.join(unknown)}: only python and typescript agents carry the SDK")
    requirements_txt = requirements() if "python" in languages.values() else ""
    for agent, language in languages.items():
        place_python(requirements_txt, agent) if language == "python" else build_typescript(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
