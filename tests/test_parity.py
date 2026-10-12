"""AC-3 parity: every public Python callable and error class has a TypeScript export of the camelCase
name with the same required arity. Run from the repo root: python -m unittest discover -s tests

Arity = required positional parameters (Python: no default, not *args/**kwargs/keyword-only; TS:
`Function.length`, the parameters before the first default). Typed dicts (Answer, TaskContext,
CredentialInfo) are data shapes, not callables: their parity is the TS typings'."""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import typing
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

import jaah_agent  # noqa: E402


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def required_arity(fn) -> int:
    params = list(inspect.signature(fn).parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    return sum(1 for p in params
               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty)


def python_surface() -> dict:
    surface = {}
    for name in jaah_agent.__all__:
        obj = getattr(jaah_agent, name)
        if typing.is_typeddict(obj):
            continue
        if inspect.isclass(obj):
            init = obj.__init__ if "__init__" in vars(obj) else (lambda self: None)
            surface[name] = {
                "kind": "class", "length": required_arity(init),
                "base": obj.__mro__[1].__name__ if issubclass(obj, jaah_agent.JaahError) and obj is not jaah_agent.JaahError else None,
                "methods": {camel(m): required_arity(f) for m, f in vars(obj).items()
                            if inspect.isfunction(f) and not m.startswith("_")},
            }
        else:
            surface[camel(name)] = {"kind": "function", "length": required_arity(obj)}
    return surface


def ts_surface() -> dict:
    out = subprocess.run(["node", os.path.join(ROOT, "tests", "ts_exports.mjs")],
                         capture_output=True, text=True, check=True, cwd=ROOT)
    return json.loads(out.stdout)


class ParityTest(unittest.TestCase):
    def test_every_python_callable_has_a_ts_counterpart(self):
        py, ts = python_surface(), ts_surface()
        self.assertGreater(len(py), 5)
        problems = []
        for name, spec in py.items():
            got = ts.get(name)
            if got is None:
                problems.append(f"TS export {name} is missing")
                continue
            if got["kind"] != spec["kind"]:
                problems.append(f"{name}: Python {spec['kind']}, TS {got['kind']}")
            if got["length"] != spec["length"]:
                problems.append(f"{name}: arity Python {spec['length']} vs TS {got['length']}")
            if spec.get("base") and got.get("base") != spec["base"]:
                problems.append(f"{name}: extends {got.get('base')}, Python extends {spec['base']}")
            for method, arity in (spec.get("methods") or {}).items():
                ts_arity = (got.get("methods") or {}).get(method)
                if ts_arity is None:
                    problems.append(f"{name}.{method} is missing in TS")
                elif ts_arity != arity:
                    problems.append(f"{name}.{method}: arity Python {arity} vs TS {ts_arity}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_versions_match(self):
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as fh:
            version = fh.read().strip()
        with open(os.path.join(ROOT, "ts", "package.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], version)
        self.assertEqual(jaah_agent.__version__, version)


if __name__ == "__main__":
    unittest.main()
