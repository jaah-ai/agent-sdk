// Prints the TS package's public surface as JSON for tests/test_parity.py:
// {name: {kind, length, base, methods: {name: length}}}
import * as sdk from "../ts/index.js";

const surface = {};
for (const [name, value] of Object.entries(sdk)) {
  if (typeof value !== "function") continue;
  const isClass = /^class\b/.test(Function.prototype.toString.call(value));
  const entry = { kind: isClass ? "class" : "function", length: value.length };
  if (isClass) {
    entry.base = Object.getPrototypeOf(value)?.name || null;
    entry.methods = {};
    for (const key of Object.getOwnPropertyNames(value.prototype)) {
      if (key === "constructor" || key.startsWith("_")) continue;
      const desc = Object.getOwnPropertyDescriptor(value.prototype, key);
      if (typeof desc.value === "function") entry.methods[key] = desc.value.length;
    }
  }
  surface[name] = entry;
}
process.stdout.write(JSON.stringify(surface));
