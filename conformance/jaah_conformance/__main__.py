"""`jaah-conformance --profile <acp-core|jaah-extension> [--spec FILE] [--json] -- <agent command...>`.

Exit 0 when every check passes, 1 when one fails, 2 on bad usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .runner import DEFAULT_PROMPT, PROFILES, find_spec, run_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jaah-conformance", description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=PROFILES, default="jaah-extension")
    parser.add_argument("--spec", type=Path, help="the extension spec (default: spec/jaah-extension.json above)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="the text of the prompt turn")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per protocol step")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the agent's command line, after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("name the agent command after --")
    report = asyncio.run(run_profile(args.profile, command, args.spec or find_spec(), args.prompt, args.timeout))
    print(json.dumps(report.to_json(), indent=2) if args.json else report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
