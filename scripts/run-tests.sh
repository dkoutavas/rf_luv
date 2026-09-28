#!/usr/bin/env bash
set -euo pipefail

# Run every test suite in the repo (each <dir>/tests or ops/<dir>/tests).
# No hardware, no ClickHouse: the suites use fakes.
#
# Usage: bash scripts/run-tests.sh [extra pytest args]
# Needs pytest: sudo zypper install python313-pytest  (or any venv with pytest + numpy)

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
if ! "$PY" -m pytest --version >/dev/null 2>&1; then
    echo "pytest not found for $PY. Install it: sudo zypper install python313-pytest" >&2
    echo "or point PYTHON at a venv that has it: PYTHON=/path/to/venv/bin/python $0" >&2
    exit 2
fi

mapfile -t suites < <(find . -path ./graphify-out -prune -o -path ./docs -prune -o \
                           -type d -name tests -print | sort)
echo "suites: ${suites[*]}"
exec "$PY" -m pytest -q -p no:cacheprovider "${suites[@]}" "$@"
