"""The session's events file (`JAAH_AGENT_EVENTS`): one JSON object per line, each with `timestamp`
(ISO-8601 UTC) and `type`. Appended with O_APPEND and ONE write() per line, so lines from the SDK and
the node supervisor interleave whole. Jaah uploads this file as the session transcript and prices its
`usage` lines, so the `usage` shape is a contract — do not change its keys."""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventsFile:
    def __init__(self, path: str | None) -> None:
        self.path = path

    def write(self, line: dict[str, Any]) -> None:
        record = {"type": line["type"], "timestamp": now_iso(),
                  **{k: v for k, v in line.items() if k != "type"}}
        data = (json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str) + "\n").encode()
        if not self.path:
            # Not under a node (a local dry run): stderr, which the node supervisor would capture anyway.
            sys.stderr.write(data.decode())
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def usage_line(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> dict:
    return {"type": "usage", "message": {"id": str(uuid.uuid4()), "model": model, "usage": {
        "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
        "cache_read_input_tokens": int(cache_read), "cache_creation_input_tokens": int(cache_write)}}}
