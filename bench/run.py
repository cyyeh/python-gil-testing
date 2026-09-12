"""Benchmark driver: runs workloads under every interpreter configuration (each
in its own subprocess) and writes results/results.json.

    python -m bench.run                      # bare-Python (stdlib) workloads only
    python -m bench.run --libs               # + every library workload
    python -m bench.run --packages numpy     # + library workloads that need numpy
    python -m bench.run --only cpu_primes    # any named workloads
    python -m bench.run --quick --repeats 1  # smoke run

Library workloads go through the free-threading support check first
(check_gil_support.py). A package that re-enables the GIL is warned about, its
results are flagged, and an extra "PYTHON_GIL=0 forced" configuration is added
for the workloads that need it.

Results are merged into an existing --out file (re-run workloads replace their
old records) unless --fresh is given.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from bench.worker import ALL_WORKLOADS, DEFAULT_REPEATS, default_workers, usable_cpus
from bench.workloads import Workload

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import check_gil_support as cgs  # noqa: E402  (single-file tool at repo root)

# Keep BLAS / OpenMP single-threaded so that scaling reflects Python threads only.
_PIN_BLAS = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

# Smaller sizes for --quick smoke runs.
QUICK_KWARGS = {
    "cpu_primes": {"limit": 30_000},
    "cpu_float": {"iterations": 300_000},
    "io_sleep": {"waits": 5, "seconds": 0.005},
    "contended_list_append": {"total": 200_000},
    "contended_dict_update": {"total": 200_000},
    "mp_primes": {"limit": 30_000},
    "numpy_small_ops": {"iters": 2_000},
    "numpy_matmul": {"size": 64, "reps": 4},
    "pandas_groupby": {"rows": 20_000},
    "duckdb_query": {"rows": 500_000},
    "sklearn_fit": {"samples": 400, "trees": 3},
    "fastapi_sync_cpu": {"requests": 8, "prime_limit": 5_000},
    "fastapi_async_json": {"requests": 40},
}

FORCED_LABEL = "3.14t (PYTHON_GIL=0 forced)"


def venv_python(venv: str, win: bool = (sys.platform == "win32")) -> str:
    """Path of the interpreter inside a venv, for the current OS layout."""
    return f"{venv}/Scripts/python.exe" if win else f"{venv}/bin/python"


DEFAULT_PY312 = venv_python(str(ROOT / ".venv312"))
DEFAULT_PY314T = venv_python(str(ROOT / ".venv314t"))


def build_configs(py312: str, py314t: str, forced_workloads: list[str]) -> list[dict]:
    cfgs = [
        {"label": "3.12", "python": py312, "env": {**_PIN_BLAS},
         "note": "Stock CPython 3.12 (GIL). Baseline."},
        {"label": "3.14 (GIL on)", "python": py314t, "env": {**_PIN_BLAS, "PYTHON_GIL": "1"},
         "note": "Free-threaded 3.14 binary run with PYTHON_GIL=1: same code paths, GIL re-enabled."},
        {"label": "3.14t (free-threaded)", "python": py314t, "env": {**_PIN_BLAS},
         "note": "Free-threaded 3.14 binary, GIL disabled (default for this build)."},
    ]
    if forced_workloads:
        cfgs.append({"label": FORCED_LABEL, "python": py314t, "env": {**_PIN_BLAS, "PYTHON_GIL": "0"},
                     "note": "GIL forced off even for extension modules that do not declare free-threading "
                             "support. Unsupported by those libraries; shown for comparison only.",
                     "only_workloads": list(forced_workloads)})
    return cfgs


def select_workloads(only: set[str], libs: bool, packages: set[str] | None = None) -> list[Workload]:
    if only:
        return [s for s in ALL_WORKLOADS if s.name in only]
    packages = packages or set()
    return [s for s in ALL_WORKLOADS
            if s.category != "library" and not packages
            or s.category == "library" and (libs or packages & set(s.requires))]


def gil_support_report(py314t: str, packages: list[str]) -> dict:
    out = {}
    for pkg in packages:
        probe = cgs.run_probe(py314t, pkg)
        status, detail = cgs.classify(probe)
        out[pkg] = {"status": status, "detail": detail, "version": probe.get("version"),
                    "wheel_abi": cgs.abi_label(probe.get("wheel_tags") or []),
                    "reenabled_by": probe.get("reenabled_by"),
                    "extension_modules": probe.get("extension_modules") or []}
    return out


def forced_workloads(gil_support: dict) -> list[str]:
    bad = {pkg for pkg, e in gil_support.items() if e["status"] == "REENABLES_GIL"}
    return [s.name for s in ALL_WORKLOADS if bad & set(s.requires)]


def merge_results(existing: dict, new: dict) -> dict:
    rerun = {r["workload"] for r in new["records"]}
    merged = {**existing, **{k: v for k, v in new.items() if k not in ("records", "gil_support", "configs")}}
    merged["records"] = [r for r in existing.get("records", []) if r["workload"] not in rerun] + new["records"]
    merged["gil_support"] = {**existing.get("gil_support", {}), **new.get("gil_support", {})}
    old_cfg = {c["label"]: c for c in existing.get("configs", [])}
    configs = []
    for c in new.get("configs", []):
        if not c.get("info") and c["label"] in old_cfg:
            c = {**c, "info": old_cfg[c["label"]].get("info")}
        configs.append(c)
    seen = {c["label"] for c in configs}
    configs += [c for c in existing.get("configs", []) if c["label"] not in seen]  # keep configs not re-run
    merged["configs"] = configs
    return merged


def resolve_workers(explicit: str | None, existing: dict) -> tuple[list[int], str]:
    """--workers wins; then the counts already in the file being merged into; then the machine."""
    if explicit:
        return [int(w) for w in explicit.split(",") if w.strip()], "--workers"
    if existing.get("workers"):
        # a merged file whose workloads were measured at different thread counts cannot be
        # compared row to row, so an incremental run inherits the counts already in it
        return [int(w) for w in existing["workers"]], "already in the results file"
    return default_workers(), f"{usable_cpus()} usable cores"


def host_info() -> dict:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        try:
            cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    elif sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    return {"os": platform.platform(), "machine": platform.machine(), "cpu": cpu, "ncpu": os.cpu_count()}


def run_one(cfg: dict, workload_name: str, workers: str, repeats: int, kwargs: dict) -> tuple[dict | None, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    env.update(cfg["env"])
    env["PYTHONPATH"] = str(ROOT)
    cmd = [cfg["python"], "-m", "bench.worker", "--workload", workload_name,
           "--workers", workers, "--repeats", str(repeats), "--kwargs", json.dumps(kwargs)]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    for line in proc.stderr.splitlines():
        print("   " + line, file=sys.stderr, flush=True)
    if proc.returncode != 0:
        return None, proc.stderr
    return json.loads(proc.stdout), proc.stderr


def collect_facts(cfg: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    env.update(cfg["env"])
    env["PYTHONPATH"] = str(ROOT)
    return json.loads(subprocess.check_output([cfg["python"], "-m", "bench.facts"], cwd=ROOT, env=env, text=True))


def warn_unsupported(gil_support: dict) -> None:
    for pkg, e in gil_support.items():
        if e["status"] == "REENABLES_GIL":
            print(f"\n!! WARNING: '{pkg}' does NOT support free-threading: {e['detail']}\n"
                  f"   Its '3.14t (free-threaded)' results below run WITH the GIL. An extra "
                  f"'{FORCED_LABEL}' configuration is added for comparison only.", file=sys.stderr, flush=True)
        elif e["status"] == "NOT_INSTALLED":
            print(f"\n!! WARNING: '{pkg}' is not installed in the 3.14t environment: {e['detail']}",
                  file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="results/results.json")
    ap.add_argument("--workers", default=None,
                    help="comma-separated thread counts; default: 1..cores on this machine, or the "
                         "counts already in --out when adding to an existing run")
    ap.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                    help=f"timed repeats per thread count (default {DEFAULT_REPEATS}); median is plotted")
    ap.add_argument("--only", default="", help="comma-separated workload names (overrides tiers)")
    ap.add_argument("--libs", action="store_true", help="include all library workloads")
    ap.add_argument("--packages", default="", help="comma-separated packages: include library workloads needing them")
    ap.add_argument("--quick", action="store_true", help="tiny problem sizes for a smoke run")
    ap.add_argument("--fresh", action="store_true", help="do not merge into an existing --out file")
    ap.add_argument("--py312", default=DEFAULT_PY312)
    ap.add_argument("--py314t", default=DEFAULT_PY314T)
    args = ap.parse_args(argv)

    only = {n for n in args.only.split(",") if n}
    packages = {n for n in args.packages.split(",") if n}
    selected = select_workloads(only, args.libs, packages)
    if not selected:
        sys.exit("no workloads selected")

    needed_pkgs = sorted({p for s in selected for p in s.requires})
    gil_support = gil_support_report(args.py314t, needed_pkgs) if needed_pkgs else {}
    if gil_support:
        print("\n== free-threading support check (3.14t)", file=sys.stderr)
        for pkg, e in gil_support.items():
            print(f"   {pkg:14s} {e['version'] or '-':10s} {e['wheel_abi']:16s} {e['status']}", file=sys.stderr)
        warn_unsupported(gil_support)

    configs = build_configs(args.py312, args.py314t, forced_workloads(gil_support))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(out.read_text()) if out.exists() and not args.fresh else {}
    workers, why = resolve_workers(args.workers, existing)
    print(f"\n== thread counts: {', '.join(map(str, workers))}  ({why})", file=sys.stderr, flush=True)
    if existing.get("workers") and [int(w) for w in existing["workers"]] != workers:
        print(f"   !! {out} holds records measured at {existing['workers']}. Workloads not re-run now keep "
              f"those counts, so the report will have gaps; re-run everything (--fresh) to keep it comparable.",
              file=sys.stderr, flush=True)

    doc = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "host": host_info(),
        "workers": workers,
        "repeats": args.repeats,
        "quick": args.quick,
        "configs": [],
        "workloads": [{"name": s.name, "category": s.category, "description": s.description,
                       "requires": list(s.requires)} for s in ALL_WORKLOADS],
        "gil_support": gil_support,
        "records": [],
    }

    def save() -> None:
        out.write_text(json.dumps(merge_results(existing, doc) if existing else doc, indent=1))

    for cfg in configs:
        print(f"\n== {cfg['label']}  ({cfg['python']})", file=sys.stderr, flush=True)
        cfg_doc = {**cfg, "info": None, "stderr": {}, "facts": collect_facts(cfg)}
        doc["configs"].append(cfg_doc)
        for spec in selected:
            if cfg.get("only_workloads") and spec.name not in cfg["only_workloads"]:
                continue
            kwargs = QUICK_KWARGS.get(spec.name, {}) if args.quick else {}
            result, stderr = run_one(cfg, spec.name, ",".join(map(str, workers)), args.repeats, kwargs)
            if result is None:
                doc["records"].append({"config": cfg["label"], "workload": spec.name, "category": spec.category,
                                       "workers": None, "seconds": [], "median": None, "stats": None, "result": None,
                                       "gil_enabled": None, "skipped": None, "error": stderr[-2000:]})
            else:
                cfg_doc["info"] = cfg_doc["info"] or result["env"]
                for rec in result["records"]:
                    doc["records"].append({"config": cfg["label"], **rec})
            if "GIL" in stderr:  # keep the interpreter's own warning text for the report
                cfg_doc["stderr"][spec.name] = "\n".join(l for l in stderr.splitlines() if "GIL" in l)
            save()
    save()
    print(f"\nwrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
