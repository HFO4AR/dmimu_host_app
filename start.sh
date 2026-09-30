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
if [ "${DMIMU_SKIP_MODEL:-0}" = "1" ]; then
  printf '%s\n' 'Official CAD preparation skipped (DMIMU_SKIP_MODEL=1).'
else
  if ! .venv/bin/python scripts/prepare_model.py --install; then
    printf '%s\n' 'Official CAD preparation failed; starting the workbench without a model. Retry: .venv/bin/python scripts/prepare_model.py --install' >&2
  fi
fi
exec .venv/bin/python app.py "$@"
