"""Jaah Agent SDK v2: write an ACP agent; the library speaks the protocol and the `jaah.ai` extension.

    import jaah_agent

    class Echo(jaah_agent.Agent):
        async def prompt(self, turn):
            await turn.say(turn.text)

    jaah_agent.run(Echo())

The contract is `spec/jaah-extension.json` beside this package; README.md is the guide.
"""

from ._agent import Agent, Turn, run, serve
from ._extension import (
    AlreadySettled,
    Extension,
    ExtensionError,
    ExtensionUnavailable,
    method_for,
    verb_for,
)

__all__ = [
    "Agent",
    "AlreadySettled",
    "Extension",
    "ExtensionError",
    "ExtensionUnavailable",
    "Turn",
    "method_for",
    "run",
    "serve",
    "verb_for",
]
