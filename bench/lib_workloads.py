"""Workloads that exercise third-party libraries (numpy, pandas, duckdb, fastapi).

Imports are done lazily inside each function so this module can be loaded on an
interpreter that is missing some of the packages; the runner skips a workload
whose ``requires`` cannot be imported.

Work is expressed as a fixed number of identical *tasks* handed round-robin to
``n_workers`` threads, so total work is constant across thread counts.
"""

from __future__ import annotations

import atexit
import http.client
import os
import socket
import subprocess
import sys
import time
from typing import Callable

from bench.workloads import Workload, _run_threads


def run_tasks(kernel: Callable[[int], object], tasks: int, n_workers: int) -> list:
    """Run ``kernel(i)`` for i in range(tasks) across ``n_workers`` threads; results in task order."""
    results: list = [None] * tasks

    def worker(start: int) -> None:
        for i in range(start, tasks, n_workers):
            results[i] = kernel(i)

    _run_threads(worker, [(w,) for w in range(min(n_workers, tasks))])
    return results


# --------------------------------------------------------------------------
# numpy
# --------------------------------------------------------------------------


def numpy_small_ops(n_workers: int, tasks: int = 8, iters: int = 20_000) -> int:
    """Many tiny array ops (256 elements). Dominated by Python-level dispatch overhead,
    which holds the GIL, so GIL builds are not expected to scale."""
    import numpy as np

    def kernel(_: int) -> int:
        a = np.arange(256, dtype=np.int64)
        total = 0
        for _ in range(iters):
            total += int((a * 2 + 1).sum())
        return total

    return sum(run_tasks(kernel, tasks, n_workers))


def numpy_matmul(n_workers: int, tasks: int = 8, size: int = 512, reps: int = 8) -> float:
    """Repeated float64 matmul. numpy releases the GIL inside BLAS, so even GIL
    builds scale here (BLAS threading is pinned to 1 by the runner)."""
    import numpy as np

    def kernel(i: int) -> float:
        rng = np.random.default_rng(i)
        a = rng.random((size, size))
        b = rng.random((size, size))
        acc = 0.0
        for _ in range(reps):
            acc += float(np.trace(a @ b))
        return acc

    return float(sum(run_tasks(kernel, tasks, n_workers)))


# --------------------------------------------------------------------------
# pandas
# --------------------------------------------------------------------------


def pandas_groupby(n_workers: int, tasks: int = 8, rows: int = 1_000_000) -> int:
    """Build a DataFrame and run groupby-sum. Most of the wall time is spent in
    C/Cython with the GIL released, so this scales on GIL builds too."""
    import numpy as np
    import pandas as pd

    def kernel(i: int) -> int:
        rng = np.random.default_rng(i)
        df = pd.DataFrame(
            {"key": rng.integers(0, 100, rows), "v1": rng.integers(0, 1000, rows), "v2": rng.integers(0, 1000, rows)}
        )
        g = df.groupby("key")[["v1", "v2"]].sum()
        return int(g["v1"].sum() + g["v2"].sum())

    return sum(run_tasks(kernel, tasks, n_workers))


# --------------------------------------------------------------------------
# duckdb
# --------------------------------------------------------------------------


def duckdb_query(n_workers: int, tasks: int = 8, rows: int = 5_000_000) -> int:
    """Aggregate over a generated range on a per-task in-memory connection with
    DuckDB's own parallelism disabled (threads=1), so any scaling comes from
    Python threads. DuckDB releases the GIL while executing a query."""
    import duckdb

    def kernel(_: int) -> int:
        con = duckdb.connect()
        try:
            con.execute("SET threads TO 1")
            (total,) = con.execute(f"SELECT sum(i * i) FROM range({rows}) t(i)").fetchone()
            return int(total)
        finally:
            con.close()

    return sum(run_tasks(kernel, tasks, n_workers))


# --------------------------------------------------------------------------
# scikit-learn
# --------------------------------------------------------------------------


def sklearn_fit(n_workers: int, tasks: int = 8, samples: int = 4000, trees: int = 20) -> int:
    """Fit a RandomForestClassifier (n_jobs=1) per task on a synthetic dataset and
    predict. scikit-learn's Cython tree builder releases the GIL in its hot loops."""
    import numpy as np
    from sklearn.datasets import make_classification
    from sklearn.ensemble import RandomForestClassifier

    def kernel(i: int) -> int:
        X, y = make_classification(n_samples=samples, n_features=20, n_informative=8, random_state=i)
        clf = RandomForestClassifier(n_estimators=trees, random_state=i, n_jobs=1).fit(X, y)
        return int(np.sum(clf.predict(X[:200])))

    return sum(run_tasks(kernel, tasks, n_workers))


# --------------------------------------------------------------------------
# fastapi (uvicorn subprocess on the same interpreter, http.client load generators)
# --------------------------------------------------------------------------

_SERVER: tuple[subprocess.Popen, str, int] | None = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop_server() -> None:
    global _SERVER
    if _SERVER is not None:
        proc = _SERVER[0]
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _SERVER = None


def _server() -> tuple[str, int]:
    """Start uvicorn once per process (same interpreter & env as the caller)."""
    global _SERVER
    if _SERVER is None:
        port = _free_port()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "bench.fastapi_app:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited during start-up")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            proc.kill()
            raise RuntimeError("uvicorn did not start within 30s")
        _SERVER = (proc, "127.0.0.1", port)
        atexit.register(_stop_server)
    return _SERVER[1], _SERVER[2]


def _hit(path: str, n: int) -> int:
    host, port = _server()
    con = http.client.HTTPConnection(host, port, timeout=60)
    ok = 0
    try:
        for _ in range(n):
            con.request("GET", path)
            resp = con.getresponse()
            resp.read()
            ok += resp.status == 200
    finally:
        con.close()
    return ok


def _spread(total: int, n: int) -> list[int]:
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def fastapi_sync_cpu(n_workers: int, requests: int = 32, prime_limit: int = 30_000) -> int:
    """``n_workers`` concurrent clients share ``requests`` calls to a sync (def) endpoint
    that counts primes. Starlette runs def endpoints on worker threads, so this is
    where free-threading can help a web server."""
    path = f"/sync-cpu?limit={prime_limit}"
    return sum(_run_threads(_hit, [(path, n) for n in _spread(requests, n_workers) if n]))


def fastapi_async_json(n_workers: int, requests: int = 400) -> int:
    """Same client pattern against a trivial async endpoint: measures event-loop /
    JSON overhead where free-threading is not expected to help."""
    return sum(_run_threads(_hit, [("/async-json", n) for n in _spread(requests, n_workers) if n]))


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

WORKLOADS: list[Workload] = [
    Workload("numpy_small_ops", "library", numpy_small_ops,
             "8 tasks x 20k tiny (256-element) array ops. Python dispatch overhead dominates and "
             "holds the GIL, so GIL builds should not scale.", ("numpy",), warmup=True),
    Workload("numpy_matmul", "library", numpy_matmul,
             "8 tasks x 8 matmuls of 512x512 float64 (BLAS threads pinned to 1). numpy releases the "
             "GIL inside BLAS, so all builds should scale.", ("numpy",), warmup=True),
    Workload("pandas_groupby", "library", pandas_groupby,
             "8 tasks: build a 1M-row DataFrame and groupby-sum. Most of the wall time is spent in "
             "C/Cython with the GIL released, so this scales on every build - the GIL is not the "
             "bottleneck here.", ("numpy", "pandas"), warmup=True),
    Workload("duckdb_query", "library", duckdb_query,
             "8 tasks: SELECT sum(i*i) FROM range(5M) on a per-task connection with SET threads=1. "
             "DuckDB releases the GIL during execution. NOTE: importing duckdb re-enables the GIL "
             "on free-threaded builds unless PYTHON_GIL=0 is forced.", ("duckdb",), warmup=True),
    Workload("sklearn_fit", "library", sklearn_fit,
             "8 tasks: make_classification(4000x20) + RandomForestClassifier(20 trees, n_jobs=1).fit + "
             "predict. Cython tree building releases the GIL in hot loops.", ("sklearn",), warmup=True),
    Workload("fastapi_sync_cpu", "library", fastapi_sync_cpu,
             "32 requests to a sync def endpoint (prime count to 30k) from N concurrent clients; "
             "uvicorn runs in a subprocess on the same interpreter. def endpoints run on a thread "
             "pool, so free-threading can parallelise them.", ("fastapi", "uvicorn"), warmup=True),
    Workload("fastapi_async_json", "library", fastapi_async_json,
             "400 requests to a trivial async endpoint from N concurrent clients. Event-loop "
             "bound; measures per-request overhead of each build.", ("fastapi", "uvicorn"), warmup=True),
]

WORKLOADS_BY_NAME = {spec.name: spec for spec in WORKLOADS}
