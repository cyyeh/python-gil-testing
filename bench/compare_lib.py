"""One-command library comparison: support check -> warning -> benchmark -> report.

    python -m bench.compare_lib numpy
    python -m bench.compare_lib pandas duckdb --quick
    python -m bench.compare_lib polars --install      # pip-install into both venvs first

For each package this
  1. finds the library workloads in bench/lib_workloads.py that require it
     (if there are none, explains how to add one),
  2. runs check_gil_support.py on the 3.14t interpreter and WARNS loudly if the
     package re-enables the GIL or is missing (bench.run does this),
  3. benchmarks only those workloads on every interpreter configuration,
     merging into results/results.json,
  4. regenerates results/report.html, checks that every number on it comes back out of the
     raw timings (exit 4 if not), and prints a short summary.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bench import report, run, verify_report
from bench.run import DEFAULT_PY312, DEFAULT_PY314T, DEFAULT_REPEATS
from bench.report import FT, cell_stats, pivot, speedup, _fmt_s, _fmt_x
from bench.worker import ALL_WORKLOADS
from bench.workloads import Workload

ROOT = Path(__file__).resolve().parent.parent


def workloads_for(packages: set[str]) -> list[Workload]:
    return [s for s in ALL_WORKLOADS if s.category == "library" and packages & set(s.requires)]


def guidance(package: str) -> str:
    return f"""No workload uses '{package}' yet. Add one to bench/lib_workloads.py following the contract:

    def {package}_workload(n_workers: int, tasks: int = 8, ...) -> int | float:
        import {package}                     # lazy import, inside the function
        def kernel(i: int): ...              # one unit of work, deterministic in i
        return sum(run_tasks(kernel, tasks, n_workers))

    WORKLOADS.append(Workload("{package}_workload", "library", {package}_workload,
                              "what it measures", requires=("{package}",), warmup=True))

  * n_workers is the FIRST argument; total work must not depend on it.
  * The return value must be identical for every n_workers (tests check this).
  * Add a test in tests/test_lib_workloads.py, run it with both .venv312 and .venv314t,
    then re-run:  python -m bench.compare_lib {package}
  (The repo skill .agents/skills/gil-lib-compare/SKILL.md walks through this.)"""


def install_command(packages: list[str], python: str) -> list[str]:
    # --no-build: if there is no wheel for this interpreter (typically no cp314t wheel
    # for the free-threaded build) fail fast instead of silently compiling from source.
    return ["uv", "pip", "install", "--python", python, "--no-build", *packages]


def install(packages: list[str], py312: str, py314t: str) -> None:
    for py in (py312, py314t):
        print(f"== installing {' '.join(packages)} into {py} (wheels only)", file=sys.stderr, flush=True)
        proc = subprocess.run(install_command(packages, py))
        if proc.returncode != 0:
            sys.exit(f"no installable wheel for {' '.join(packages)} on {py}. For the free-threaded build this "
                     f"usually means the project ships no cp314t wheel yet; build from source manually with "
                     f"`uv pip install --python {py} {' '.join(packages)}` if you want to try anyway.")


def summarize(doc: dict, workload_names: list[str]) -> str:
    p = pivot(doc)
    max_w = max(doc["workers"])
    lines = []
    for pkg, e in doc.get("gil_support", {}).items():
        if e["status"] == "REENABLES_GIL":
            lines.append(f"WARNING: {pkg} does NOT support free-threading - {e['detail']}")
        elif e["status"] == "NOT_INSTALLED":
            lines.append(f"WARNING: {pkg} is not installed on 3.14t - {e['detail']}")
    for name in workload_names:
        if name not in p:
            continue
        lines.append(f"{name}  (speed-up 1->{max_w} workers; median ± stdev at {max_w})")
        for cfg in report._configs_present(doc):
            bw = p[name].get(cfg)
            if not bw:
                continue
            rec = bw.get(max_w, {})
            st = cell_stats(rec) if rec else None
            spread = f" ± {_fmt_s(st['stdev'])} (n={st['n']}{', NOISY' if st['cv'] > report.NOISY_CV else ''})" if st else ""
            flag = "  [GIL re-enabled by import]" if cfg == FT and rec.get("gil_enabled") else ""
            lines.append(f"    {cfg:30s} {_fmt_x(speedup(bw)):>6s}  {_fmt_s(rec.get('median')):>10s}{spread}{flag}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("packages", nargs="+", help="import names, e.g. numpy pandas duckdb")
    ap.add_argument("--install", action="store_true", help="uv pip install the packages into both venvs first")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    ap.add_argument("--workers", default="1,2,4,8")
    ap.add_argument("--out", default="results/results.json")
    ap.add_argument("--report", default="results/report.html")
    ap.add_argument("--py312", default=DEFAULT_PY312)
    ap.add_argument("--py314t", default=DEFAULT_PY314T)
    args = ap.parse_args(argv)

    packages = set(args.packages)
    if args.install:
        install(sorted(packages), args.py312, args.py314t)

    selected = workloads_for(packages)
    covered = {p for s in selected for p in s.requires} & packages
    for pkg in sorted(packages - covered):
        print(guidance(pkg), file=sys.stderr)
    if not selected:
        return 3

    run_args = ["--packages", ",".join(sorted(covered)), "--out", args.out, "--repeats", str(args.repeats),
                "--workers", args.workers, "--py312", args.py312, "--py314t", args.py314t]
    if args.quick:
        run_args.append("--quick")
    run.main(run_args)
    report.main(["--in", args.out, "--out", args.report])

    doc = json.loads(Path(args.out).read_text())
    # the page is the deliverable, so it is checked before it is handed over: every number on it
    # has to come back out of the raw timings. Prose still needs a reader - see the skill.
    problems, _ = verify_report.verify(doc, Path(args.report).read_text())
    if problems:
        print(f"\n!! {args.report} does not match {args.out} - do NOT report these numbers:", file=sys.stderr)
        for p in problems[:10]:
            print(f"   - {p}", file=sys.stderr)
        print("   run `python -m bench.verify_report` for the full list", file=sys.stderr)

    print("\n" + summarize(doc, [s.name for s in selected]))
    print(f"\nreport: {Path(args.report).resolve()}")
    if problems:
        return 4
    unsupported = [pkg for pkg in covered
                   if doc.get("gil_support", {}).get(pkg, {}).get("status") in ("REENABLES_GIL", "NOT_INSTALLED")]
    return 1 if unsupported else 0


if __name__ == "__main__":
    sys.exit(main())
