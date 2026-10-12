"""Jaah Agent SDK — plug any agent into Jaah's governance pipeline.

    import jaah_agent
    agent = jaah_agent.connect()
    ctx = agent.task()
    ...
    agent.settle("Did the thing", "no_pr", no_pr_reason="no_code_needed", no_pr_evidence="...")

Stdlib only. The node puts this package on PYTHONPATH; never add it to your own dependencies."""
from ._agent import Agent, Answer, CredentialInfo, TaskContext, connect
from ._errors import JaahError, JaahRefused, JaahUnavailable, NotARunner


def _version() -> str:
    import os

    for candidate in (os.path.join(os.path.dirname(__file__), "VERSION"),
                      os.path.join(os.path.dirname(__file__), "..", "..", "VERSION")):
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8") as fh:
                return fh.read().strip()
    return "0.0.0"


__version__ = _version()

__all__ = [
    "connect", "Agent", "Answer", "TaskContext", "CredentialInfo",
    "JaahError", "JaahUnavailable", "JaahRefused", "NotARunner",
]
