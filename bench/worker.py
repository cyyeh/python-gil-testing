"""Runs ONE workload at several thread counts inside the current interpreter and
prints a JSON document on stdout. ``bench.run`` launches this in a fresh
subprocess per (interpreter, workload) so that a library which re-enables the
GIL at import time cannot affect other measurements.
"""

from __future__ import annotations

import argparse
import importlib
import json
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
    return {
        "workers": workers,
        "seconds": seconds,
        "median": statistics.median(seconds),
        "result": repr(result),
    }


def _base_record(spec: Workload, workers: int) -> dict:
    return {
        "workload": spec.name,
        "category": spec.category,
        "workers": workers,
        "seconds": [],
        "median": None,
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
    ap.add_argument("--workers", default="1,2,4,8")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--kwargs", default="{}", help="JSON dict of extra kwargs for the workload")
    args = ap.parse_args(argv)

    spec = ALL_BY_NAME[args.workload]
    workers_list = [int(w) for w in args.workers.split(",")]
    records = run_workload(spec, workers_list, args.repeats, json.loads(args.kwargs))
    print(json.dumps({"env": env_info(), "records": records}))


if __name__ == "__main__":
    main()
