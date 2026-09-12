---
name: gil-lib-compare
description: Use when asked to benchmark, compare, or add a Python library (numpy, pandas, polars, duckdb, torch, ...) to this repo's GIL vs free-threaded comparison, or when asked whether a library "supports free-threading", "works without the GIL", "has a cp314t wheel", or "scales with threads on 3.14t".
---

# Comparing a library under the GIL vs free-threading

## Overview

One C extension that has not opted in to free-threading **re-enables the GIL for the
whole process** at import time (with only a `RuntimeWarning`). A benchmark run without
checking for that produces a "3.14t" column that is silently GIL-on. So: **check first,
tell the user, then measure.**

## The flow (each step gates the next)

1. **Install into both venvs, wheels only.** Never trigger a source build by accident.
   `python -m bench.compare_lib <pkg> --install` (uses `uv pip install --no-build`).
   No `cp314t` wheel -> stop and tell the user; offer a manual source build.
2. **Support check on 3.14t, before writing any workload.**
   `.venv314t/bin/python check_gil_support.py <pkg>`
   - `SUPPORTED` / `PURE_PYTHON` -> continue.
   - `REENABLES_GIL` -> **say so to the user now**, before benchmarking, with the module name from
     `detail`. State plainly that its free-threaded numbers will be GIL-on and that the harness adds a
     `3.14t (PYTHON_GIL=0 forced)` column that is *unsupported by the library, comparison only*.
   - `NOT_INSTALLED` -> back to step 1.
3. **Workload exists?** `python -m bench.compare_lib <pkg>` exits 3 and prints the contract if not.
   Add one to `bench/lib_workloads.py` (mirror an existing one - e.g. the pandas twin of a numpy
   workload - so numbers are comparable). REQUIRED in every workload:
   - `n_workers` is the first argument; a fixed number of `tasks` split with `run_tasks`.
   - Return value identical at every thread count (an integer/`fsum` checksum).
   - Lazy `import` inside the function; `requires=(...)`, `warmup=True`.
   - Description says whether the library **holds or releases the GIL** in its hot path.
   - **Pin the library's own parallelism** (BLAS env vars are already pinned; DuckDB uses
     `SET threads TO 1`; polars needs `POLARS_MAX_THREADS=1`, torch `set_num_threads(1)`).
     If pinning funnels all Python threads through one pool worker, say so in the description instead.
   - Add a test in `tests/test_lib_workloads.py` guarded by `@needs("<pkg>")`, plus `QUICK_KWARGS` in `bench/run.py`.
4. **Tests on BOTH interpreters:** `.venv312/bin/python -m unittest discover -s tests` and the same with `.venv314t`.
5. **Measure:** `python -m bench.compare_lib <pkg>` (add `--quick` first for a smoke run). It re-runs the
   check, warns, benchmarks only that library's workloads, merges into `results/results.json`,
   regenerates `results/report.html`, prints a summary.
6. **Report to the user:** the support status, then per workload the 1->8 speed-up on each configuration
   and the 8-thread time, the `[GIL re-enabled by import]` flag if present, and `results/report.html`'s path.

## Interpreting results

| pattern | meaning |
|---|---|
| scales on every build | the extension already releases the GIL in C; 3.14t adds little |
| scales only on 3.14t | the hot path holds the GIL (Python dispatch, Cython without `nogil`); free-threading unlocked it |
| flat everywhere + `REENABLES_GIL` | the import turned the GIL back on; look at the forced column, then say it is unsupported |
| 1-thread time higher on 3.14t | per-object locking / biased refcount overhead of the free-threaded build |

## Common mistakes

- Benchmarking before running `check_gil_support.py`, or burying the warning at the end.
- Pinning nothing and attributing the library's own thread pool to free-threading.
- Timing the first call (lazy import, server start) - `warmup=True` exists for this.
- Running the test suite on one interpreter only.
- Presenting `PYTHON_GIL=0 forced` numbers as if the library supported free-threading.
