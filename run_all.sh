#!/usr/bin/env bash
# setup -> benchmark -> report, in one go.
#
#   ./run_all.sh            # bare-Python workloads
#   ./run_all.sh --libs     # + library workloads (numpy, pandas, duckdb, scikit-learn, fastapi)
#   ./run_all.sh --quick    # smoke run with tiny problem sizes
set -euo pipefail
cd "$(dirname "$0")"

LIBS=""; QUICK=""
for arg in "$@"; do
  case "$arg" in
    --libs)  LIBS="--libs" ;;
    --quick) QUICK="--quick --repeats 1" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[[ -x .venv312/bin/python && -x .venv314t/bin/python ]] || ./setup.sh ${LIBS:+--libs}

.venv312/bin/python -m bench.run --fresh $LIBS $QUICK
.venv312/bin/python -m bench.report
echo "open results/report.html"
