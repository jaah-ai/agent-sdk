"""A fake node daemon: a unix socket that reads one JSON line and answers one line, per connection.

`handler(verb, args)` returns the reply object ({"ok": ..., ...}) or None to close without replying.
Every request is recorded in `calls` so tests can assert exactly what reached the wire."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading


class FakeDaemon:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        self._dir = tempfile.mkdtemp(prefix="jaah-fake-")
        self.path = os.path.join(self._dir, "sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(16)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n", 1)[0])
                self.calls.append((req["call"], req["args"]))
                reply = self.handler(req["call"], req["args"])
                if reply is not None:
                    conn.sendall((json.dumps(reply) + "\n").encode())

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]

    def close(self):
        self._stop = True
        self._server.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def ok(result):
    return {"ok": True, "result": result}


def err(reason, detail=None):
    out = {"ok": False, "error": reason}
    if detail:
        out["detail"] = detail
    return out
