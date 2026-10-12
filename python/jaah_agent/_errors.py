"""The three ways an SDK call can fail. Every call raises one of these or returns a real result —
never a partial or default value that could be mistaken for an empty answer."""
from __future__ import annotations


class JaahError(Exception):
    """Base class: this call did not produce a result."""


class JaahUnavailable(JaahError):
    """No answer from the node daemon, or the daemon could not reach the control plane. RETRYABLE: the
    SDK already retried what was safe to retry before raising this."""


class JaahRefused(JaahError):
    """The daemon or the control plane answered, and the answer was no. NEVER retried: the same request
    earns the same refusal. `reason` is the daemon's coarse code (`rejected`, `bad_request`, ...);
    `detail` is the control plane's own explanation when it sent one."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NotARunner(JaahError):
    """The daemon cannot tell which task this process belongs to: it is not running under a Jaah node, or
    it left the runner's process tree (never daemonize, double-fork or `setsid` away from it)."""
