#!/usr/bin/env bash
# Build and test the whole SDK from this directory alone — no Jaah checkout needed.
#
#   ./check.sh    install pinned dependencies (constraints.txt, hash-checked; the lockfiles), build both
#                 packages, run the Python and TypeScript suites, the conformance suite (both reference
#                 agents included, each generated first by examples/vendor.py and run from a copy with its
#                 dependencies installed as a node installs them: pip and npm download through their caches),
#                 and the spec page's drift check.
#
# Needs python3 >= 3.11 and Node >= 22.18 with npm. Installs into .venv and node_modules (gitignored),
# reinstalling only when a pin changed.
set -euo pipefail
cd "$(dirname "$0")"

venv=.venv
stamp="$venv/.pins.sha256"
# The interpreter is part of the stamp: a host Python upgrade leaves the old venv pointing at nothing.
pins=$({ python3 -VV; cat constraints.txt python/pyproject.toml conformance/pyproject.toml; } | sha256sum | cut -d' ' -f1)
if [ "$(cat "$stamp" 2>/dev/null)" != "$pins" ] || ! "$venv/bin/python" -c '' 2>/dev/null; then
  rm -rf -- "$venv"
  python3 -m venv "$venv"
  # Every third-party package from constraints.txt at its pinned hash; then the two local packages, which
  # bring nothing else (their dependencies are already in), built by the hatchling just installed.
  "$venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes -r constraints.txt
  "$venv/bin/pip" install --quiet --disable-pip-version-check --no-index --no-deps --no-build-isolation \
    -e python -e conformance
  "$venv/bin/pip" check --disable-pip-version-check
  printf '%s\n' "$pins" >"$stamp"
fi
py="$venv/bin/python"

npm_ci() {  # npm ci only when the lockfile changed since the last install
  local dir="$1" sha
  sha=$(sha256sum "$dir/package-lock.json" | cut -d' ' -f1)
  if [ "$(cat "$dir/node_modules/.lockfile.sha256" 2>/dev/null)" != "$sha" ]; then
    (cd "$dir" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error)
    printf '%s\n' "$sha" >"$dir/node_modules/.lockfile.sha256"
  fi
}

echo "== spec page"
python3 spec/render.py --check
for copy in python/LICENSE conformance/LICENSE; do
  cmp -s LICENSE "$copy" || { echo "$copy differs from LICENSE: cp LICENSE $copy" >&2; exit 1; }
done

echo "== typescript: build + tests"
npm_ci typescript
(cd typescript && npm test --silent)
npm_ci examples/typescript

echo "== reference agents: generate their folders"
"$py" examples/vendor.py

echo "== python: tests"
"$py" -m pytest -q python/tests

echo "== conformance: suite + reference agents"
"$py" -m pytest -q conformance/tests

echo "== packages build"
out=$(mktemp -d)
trap 'rm -rf -- "$out"' EXIT
for pkg in python conformance; do
  "$py" -m build --no-isolation --outdir "$out" "$pkg" >"$out/build.log" 2>&1 || { cat "$out/build.log" >&2; exit 1; }
done
rm -f -- "$out/build.log"
"$venv/bin/pip" install --quiet --no-deps --target "$out/wheel-check" "$out"/jaah_acp_conformance-*.whl
[ -f "$out/wheel-check/jaah_conformance/jaah-extension.json" ] || { echo "conformance wheel lacks the spec" >&2; exit 1; }
rm -rf -- "$out/wheel-check"
(cd typescript && npm pack --silent --pack-destination "$out" >/dev/null)
ls "$out"
echo "agent-sdk: all green"
