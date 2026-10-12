// The session's events file (`JAAH_AGENT_EVENTS`): one JSON object per line, each with `timestamp`
// (ISO-8601 UTC) and `type`. Appended with O_APPEND and ONE write() per line, so lines from the SDK and
// the node supervisor interleave whole. Jaah uploads this file as the session transcript and prices its
// `usage` lines, so the `usage` shape is a contract — do not change its keys.
import { closeSync, openSync, writeSync } from "node:fs";
import { randomUUID } from "node:crypto";

export class EventsFile {
  constructor(path) {
    this.path = path || null;
  }

  write(line) {
    const { type, ...rest } = line;
    const record = { type, timestamp: new Date().toISOString(), ...rest };
    const data = Buffer.from(JSON.stringify(record, jsonSafe) + "\n", "utf8");
    if (!this.path) {
      // Not under a node (a local dry run): stderr, which the node supervisor would capture anyway.
      process.stderr.write(data);
      return;
    }
    const fd = openSync(this.path, "a", 0o600);
    try {
      writeSync(fd, data);
    } finally {
      closeSync(fd);
    }
  }
}

export function usageLine(model, inputTokens, outputTokens, cacheRead, cacheWrite) {
  return {
    type: "usage",
    message: {
      id: randomUUID(),
      model,
      usage: {
        input_tokens: int(inputTokens),
        output_tokens: int(outputTokens),
        cache_read_input_tokens: int(cacheRead),
        cache_creation_input_tokens: int(cacheWrite),
      },
    },
  };
}

function int(value) {
  const n = Math.trunc(Number(value));
  if (!Number.isFinite(n)) throw new TypeError(`token counts must be numbers, got ${JSON.stringify(value)}`);
  return n;
}

function jsonSafe(_key, value) {
  return typeof value === "bigint" ? value.toString() : value;
}
