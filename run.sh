#!/usr/bin/env bash
# ./run.sh                     start the web UI on http://127.0.0.1:7860
# ./run.sh evaluate [...]      batch accuracy report over data/ (see --help)
# ./run.sh template <file>     empty truth file from data/schema/<file>.json
# ./run.sh check               re-run the AWS preflight check (no changes)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Run ./setup.sh first." >&2
  exit 1
fi

cmd="${1:-ui}"
[ $# -gt 0 ] && shift
case "$cmd" in
  ui)       exec .venv/bin/python app.py "$@" ;;
  evaluate) exec .venv/bin/python evaluate.py "$@" ;;
  template) exec .venv/bin/python scripts/make_truth_template.py "$@" ;;
  check)    exec .venv/bin/python scripts/setup_env.py --check-only "$@" ;;
  *) echo "Unknown command: $cmd (use ui | evaluate | template | check)" >&2; exit 1 ;;
esac
