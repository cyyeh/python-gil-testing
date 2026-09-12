#!/usr/bin/env bash
# Install CPython 3.12 and free-threaded 3.14 with uv, create one venv per
# interpreter, and (optionally) install the library tier.
#
#   ./setup.sh            # interpreters + venvs only (bare-Python benchmarks)
#   ./setup.sh --libs     # + numpy pandas duckdb scikit-learn fastapi uvicorn
set -euo pipefail
cd "$(dirname "$0")"

PY312=${PY312:-cpython-3.12.13}
PY314T=${PY314T:-cpython-3.14.6+freethreaded}
LIBS=(numpy pandas duckdb scikit-learn fastapi uvicorn)

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

uv python install "$PY312" "$PY314T"
[[ -d .venv312  ]] || uv venv -q --python "$PY312"  .venv312
[[ -d .venv314t ]] || uv venv -q --python "$PY314T" .venv314t

if [[ "${1:-}" == "--libs" ]]; then
  uv pip install -q --python .venv312/bin/python  "${LIBS[@]}"
  uv pip install -q --python .venv314t/bin/python "${LIBS[@]}"
fi

for v in .venv312 .venv314t; do
  "$v/bin/python" -c "import json, sys; sys.path.insert(0, '.'); from bench.facts import collect; f = collect(); print(f'{sys.executable}: Python {f[\"version\"]} | free-threaded build={f[\"free_threaded_build\"]} | GIL enabled now={f[\"gil_enabled\"]}')"
done
