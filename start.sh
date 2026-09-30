#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/.dmimu-ready ] || ! cmp -s requirements.txt .venv/.dmimu-ready; then
  .venv/bin/python -m pip install -r requirements.txt
  cp requirements.txt .venv/.dmimu-ready
fi
exec .venv/bin/python app.py "$@"
