#!/usr/bin/env bash
# One-time setup: Python virtualenv, dependencies, AWS preflight check, .env.
#   ./setup.sh --profile <aws-profile> --region <aws-region> [--create-bucket | --bucket NAME]
# All options are passed to scripts/setup_env.py (see --help).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10+ is required (set PYTHON=/path/to/python if it is not on PATH)." >&2
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10+ is required; found $("$PY" --version 2>&1)." >&2
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment in .venv ..."
  "$PY" -m venv .venv
fi
echo "Installing dependencies ..."
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt

exec .venv/bin/python scripts/setup_env.py "$@"
