#!/usr/bin/env bash
# Quality gate: lint, format check, type check, tests. Stops at the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/}"
"${PY}ruff" check .
"${PY}ruff" format --check .
"${PY}mypy"
"${PY}pytest" "$@"
