"""Runs ONE workload at several thread counts inside the current interpreter and
prints a JSON document on stdout. ``bench.run`` launches this in a fresh
subprocess per (interpreter, workload) so that a library which re-enables the
GIL at import time cannot affect other measurements.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import statistics
import sys
import sysconfig
import time
import traceback
from typing import Callable

from bench import lib_workloads, workloads
from bench.workloads import Workload

ALL_WORKLOADS: list[Workload] = workloads.WORKLOADS + lib_workloads.WORKLOADS
ALL_BY_NAME = {spec.name: spec for spec in ALL_WORKLOADS}

DEFAULT_REPEATS = 5  # timed repeats per (workload, thread count); override with --repeats
MAX_LADDER = 6       # each extra thread count costs a full (config x workload x repeats) pass


def usable_cpus() -> int:
    """Cores this process may actually run on, not the ones the box happens to have."""
    try:
        return len(os.sched_getaffinity(0))  # honours taskset / cpuset pinning where it exists
    except AttributeError:                   # macOS, Windows
        return os.cpu_count() or 1


def default_workers(ncpu: int | None = None) -> list[int]:
    """Thread counts to measure on this machine: 1, doubling, up to the core count.

    1 is always in it - every speed-up in the report is measured against it - and the top of
    the ladder is the core count, because that is where a free-threaded build should stop
    gaining. Past MAX_LADDER points the middle is thinned: run time grows linearly with the
    ladder, while the two ends carry the finding.
    """
    n = max(1, ncpu if ncpu else usable_cpus())
    ladder, w = [], 1
    while w < n:
        ladder.append(w)
        w *= 2
    ladder.append(n)
    if len(ladder) > MAX_LADDER:  # keep both ends, spread the rest evenly across the doublings
        keep = {round(i * (len(ladder) - 1) / (MAX_LADDER - 1)) for i in range(MAX_LADDER)}
        ladder = [w for i, w in enumerate(ladder) if i in keep]
    return ladder


def stats(seconds: list[float]) -> dict:
    """Summary statistics of one cell's timed repeats (sample stdev; cv = stdev / mean)."""
    mean = statistics.fmean(seconds)
    stdev = statistics.stdev(seconds) if len(seconds) > 1 else 0.0
    return {
        "n": len(seconds),
        "median": statistics.median(seconds),
        "mean": mean,
        "stdev": stdev,
        "min": min(seconds),
        "max": max(seconds),
        "cv": stdev / mean if mean else 0.0,
    }


def gil_enabled() -> bool:
    # sys._is_gil_enabled() exists on 3.13+; older builds always have the GIL.
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


def env_info() -> dict:
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": gil_enabled(),
        "platform": platform.platform(),
    }


def measure(fn: Callable, workers: int, repeats: int, **kwargs) -> dict:
    seconds = []
    result = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        result = fn(workers, **kwargs)
        seconds.append(time.perf_counter() - t0)
    st = stats(seconds)
    return {
        "workers": workers,
        "seconds": seconds,
        "median": st["median"],
        "stats": st,
        "result": repr(result),
    }


def _base_record(spec: Workload, workers: int) -> dict:
    return {
        "workload": spec.name,
        "category": spec.category,
        "workers": workers,
        "seconds": [],
        "median": None,
        "stats": None,
        "result": None,
        "gil_enabled": gil_enabled(),
        "skipped": None,
        "error": None,
    }


def run_workload(spec: Workload, workers_list: list[int], repeats: int, kwargs: dict | None = None) -> list[dict]:
    kwargs = kwargs or {}
    missing = None
    for pkg in spec.requires:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing = pkg
            break

    warm_error = None
    if not missing and spec.warmup:
        try:
            spec.fn(1, **kwargs)
        except Exception:  # noqa: BLE001
            warm_error = traceback.format_exc()

    records = []
    for workers in workers_list:
        rec = _base_record(spec, workers)
        if missing:
            rec["skipped"] = f"missing package: {missing}"
        elif warm_error:
            rec["error"] = warm_error
        else:
            try:
                rec.update(measure(spec.fn, workers, repeats, **kwargs))
                rec["gil_enabled"] = gil_enabled()  # re-read: imports inside fn may flip it
            except Exception:  # noqa: BLE001 - we want the benchmark to keep going
                rec["error"] = traceback.format_exc()
        records.append(rec)
        print(f"  {spec.name:24s} workers={workers:<2d} "
              f"{'skipped' if rec['skipped'] else 'ERROR' if rec['error'] else f'{rec['median']:.3f}s'}",
              file=sys.stderr, flush=True)
    return records


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", required=True, choices=sorted(ALL_BY_NAME))
    ap.add_argument("--workers", default=",".join(map(str, default_workers())),
                    help="comma-separated thread counts (default: this machine's ladder)")
    ap.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    ap.add_argument("--kwargs", default="{}", help="JSON dict of extra kwargs for the workload")
    args = ap.parse_args(argv)

    spec = ALL_BY_NAME[args.workload]
    workers_list = [int(w) for w in args.workers.split(",")]
    records = run_workload(spec, workers_list, args.repeats, json.loads(args.kwargs))
    print(json.dumps({"env": env_info(), "records": records}))


if __name__ == "__main__":
    main()
