// A fake node daemon: a unix socket that reads one JSON line and answers one line, per connection.
// `handler(verb, args)` returns the reply object ({ok, ...}) or null to close without replying.
// Every request is recorded in `calls` so tests can assert exactly what reached the wire.
import { mkdtempSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

export class FakeDaemon {
  constructor(handler) {
    this.handler = handler;
    this.calls = [];
    this.path = join(mkdtempSync(join(tmpdir(), "jaah-fake-")), "sock");
    this.server = createServer((conn) => {
      let buf = "";
      conn.on("data", (chunk) => {
        buf += chunk.toString("utf8");
        const nl = buf.indexOf("\n");
        if (nl === -1) return;
        const req = JSON.parse(buf.slice(0, nl));
        this.calls.push([req.call, req.args]);
        const reply = this.handler(req.call, req.args);
        if (reply == null) conn.destroy();
        else conn.end(JSON.stringify(reply) + "\n");
      });
      conn.on("error", () => {});
    });
  }

  async start() {
    await new Promise((done) => this.server.listen(this.path, done));
    return this;
  }

  verbs() {
    return this.calls.map((c) => c[0]);
  }

  close() {
    return new Promise((done) => this.server.close(() => done()));
  }
}

export const ok = (result) => ({ ok: true, result });
export const err = (reason, detail) => (detail ? { ok: false, error: reason, detail } : { ok: false, error: reason });
