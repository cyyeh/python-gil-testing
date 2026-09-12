#!/usr/bin/env python3
"""Check whether Python packages can run on a free-threaded (GIL-less) interpreter.

Each package is imported in a fresh free-threaded subprocess and we observe:

  * the wheel ABI tag it was installed from (cp314t / pure / abi3 ...)
  * which compiled extension modules the import loaded
  * whether the interpreter *re-enabled the GIL* while loading one of them.
    CPython does that (with a RuntimeWarning) for any extension module that does
    not declare ``Py_mod_gil = Py_MOD_GIL_NOT_USED`` -- and once that happens the
    whole process runs with the GIL again, silently defeating free-threading.

Usage (run with a free-threaded interpreter, e.g. python3.14t):

    python3.14t check_gil_support.py numpy pandas duckdb fastapi
    python3.14t check_gil_support.py --json numpy
    python3.12  check_gil_support.py --python .venv314t/bin/python numpy   # probe with another interpreter

Exit status is 1 if any package is not installed or re-enables the GIL.
Stdlib only; copy this file anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

STATUS_HELP = {
    "SUPPORTED": "compiled extensions loaded and the GIL stayed off",
    "PURE_PYTHON": "no compiled extensions; runs without the GIL (its own thread-safety is still the library's job)",
    "REENABLES_GIL": "an extension module re-enabled the GIL for the whole process",
    "NOT_INSTALLED": "import failed",
    "UNKNOWN": "could not determine",
}

_WARN_RE = re.compile(r"to load module '([^']+)'")


# --------------------------------------------------------------------------
# pure helpers (unit-tested)
# --------------------------------------------------------------------------


def reenabled_module(warnings_: list[str]) -> str | None:
    for msg in warnings_:
        m = _WARN_RE.search(msg)
        if m:
            return m.group(1)
    return None


def abi_label(tags: list[str]) -> str:
    if not tags:
        return "?"
    for tag in tags:
        parts = tag.split("-")
        if len(parts) >= 2:
            abi = parts[1]
            if abi.endswith("t") and abi.startswith("cp"):
                return abi
            if abi == "abi3":
                return "abi3"
            if abi == "none":
                return "pure (none-any)"
    return tags[0]


def classify(p: dict) -> tuple[str, str]:
    if not p.get("free_threaded_build"):
        return "UNKNOWN", "not a free-threaded interpreter; re-run with python3.14t (or --python)"
    if not p.get("importable"):
        return "NOT_INSTALLED", p.get("error") or "import failed"
    if p.get("gil_before"):
        return "UNKNOWN", "GIL was already enabled before the import (PYTHON_GIL=1 / -X gil=1 set?)"
    if p.get("gil_after"):
        who = p.get("reenabled_by") or "an extension module"
        return "REENABLES_GIL", (f"'{who}' has not declared Py_MOD_GIL_NOT_USED, so CPython re-enabled the GIL "
                                 f"for the whole process (override with PYTHON_GIL=0 at your own risk)")
    ext = p.get("extension_modules") or []
    if not ext:
        return "PURE_PYTHON", "no compiled extension modules were loaded"
    return "SUPPORTED", f"{len(ext)} extension module(s) loaded, GIL stayed off"


def exit_code(probes: list[dict]) -> int:
    return int(any(classify(p)[0] in {"REENABLES_GIL", "NOT_INSTALLED"} for p in probes))


def render_table(probes: list[dict], interp: dict) -> str:
    head = (f"Interpreter: Python {interp.get('version')} | free-threaded build: {interp.get('free_threaded_build')}"
            f" | GIL enabled at start: {interp.get('gil_enabled', '?')}\n")
    rows = [("package", "version", "wheel ABI", "status", "detail")]
    for p in probes:
        status, detail = classify(p)
        rows.append((p["name"], p.get("version") or "-", abi_label(p.get("wheel_tags") or []), status, detail))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = [head]
    for i, r in enumerate(rows):
        lines.append("  ".join(str(c).ljust(widths[j]) for j, c in enumerate(r[:4])) + "  " + r[4])
        if i == 0:
            lines.append("-" * (sum(widths) + 8 + 40))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# the probe: runs inside the target interpreter
# --------------------------------------------------------------------------


def _gil_enabled() -> bool:
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


def _resolve_names(name: str) -> tuple[str, str | None]:
    """Accept either an import name ('sklearn') or a distribution name ('scikit-learn')."""
    import importlib.metadata as md

    pkg_to_dists = md.packages_distributions()
    if name in pkg_to_dists:
        return name, pkg_to_dists[name][0]
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        return name.replace("-", "_"), None
    dist_name = dist.metadata["Name"]
    for import_name, dists in pkg_to_dists.items():
        if dist_name in dists and not import_name.startswith("_"):
            return import_name, dist_name
    top = dist.read_text("top_level.txt")
    if top and top.split():
        return top.split()[0], dist_name
    return name.replace("-", "_"), dist_name


def _probe(name: str) -> dict:
    import importlib
    import importlib.metadata as md
    import warnings

    import_name, dist_name = _resolve_names(name)
    out = {
        "name": name,
        "import_name": import_name,
        "dist_name": dist_name,
        "version": None,
        "wheel_tags": [],
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_before": _gil_enabled(),
        "importable": False,
        "error": None,
        "gil_warnings": [],
        "reenabled_by": None,
        "extension_modules": [],
        "gil_after": None,
    }
    if dist_name:
        dist = md.distribution(dist_name)
        out["version"] = dist.version
        wheel = dist.read_text("WHEEL") or ""
        out["wheel_tags"] = [l.split(":", 1)[1].strip() for l in wheel.splitlines() if l.startswith("Tag:")]

    before = set(sys.modules)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            importlib.import_module(import_name)
            out["importable"] = True
        except BaseException as e:  # noqa: BLE001 - report anything the import raises
            out["error"] = f"{type(e).__name__}: {e}"
        out["gil_warnings"] = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    out["gil_after"] = _gil_enabled()
    out["reenabled_by"] = reenabled_module(out["gil_warnings"])
    out["extension_modules"] = sorted(
        m for m in set(sys.modules) - before
        if type(getattr(getattr(sys.modules[m], "__spec__", None), "loader", None)).__name__ == "ExtensionFileLoader"
    )
    return out


def run_probe(python: str, name: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    proc = subprocess.run([python, os.path.abspath(__file__), "--_probe", name],
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"name": name, "importable": False, "free_threaded_build": True,
                "error": f"probe crashed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}",
                "gil_before": False, "gil_after": None, "extension_modules": [], "wheel_tags": [], "version": None}
    return json.loads(proc.stdout)


def interpreter_info(python: str) -> dict:
    code = ("import sys, sysconfig, json, platform;"
            "print(json.dumps({'version': platform.python_version(),"
            "'free_threaded_build': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),"
            "'gil_enabled': bool(getattr(sys, '_is_gil_enabled', lambda: True)())}))")
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    return json.loads(subprocess.check_output([python, "-c", code], text=True, env=env))


def find_free_threaded_python() -> str | None:
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        return sys.executable
    here = Path(__file__).resolve().parent
    for venv in (".venv314t", ".venv313t"):
        for cand in (here / venv / "bin" / "python", here / venv / "Scripts" / "python.exe"):
            if cand.exists():
                return str(cand)
    for name in ("python3.14t", "python3.13t", "python3t", "python3.14t.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("packages", nargs="*", help="import or distribution names")
    ap.add_argument("-r", "--requirements", help="read package names from a requirements.txt")
    ap.add_argument("--python", help="free-threaded interpreter to probe with (default: auto-detect)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--_probe", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args._probe:
        print(json.dumps(_probe(args._probe)))
        return 0

    names = list(args.packages)
    if args.requirements:
        for line in Path(args.requirements).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                names.append(re.split(r"[<>=!~\[; ]", line, 1)[0])
    if not names:
        ap.error("give at least one package name (or -r requirements.txt)")

    python = args.python or find_free_threaded_python()
    if not python:
        print("error: no free-threaded interpreter found. Install one (e.g. `uv python install 3.14t`) "
              "and pass it with --python.", file=sys.stderr)
        return 2
    interp = interpreter_info(python)
    if not interp["free_threaded_build"]:
        print(f"error: {python} is not a free-threaded build; pass --python <python3.14t>", file=sys.stderr)
        return 2

    probes = [run_probe(python, n) for n in names]
    if args.json:
        print(json.dumps({"interpreter": interp, "python": python,
                          "packages": [{**p, "status": classify(p)[0], "detail": classify(p)[1]} for p in probes]},
                         indent=1))
    else:
        print(render_table(probes, interp))
        bad = [p["name"] for p in probes if classify(p)[0] == "REENABLES_GIL"]
        if bad:
            print(f"WARNING: importing {', '.join(bad)} re-enables the GIL. Any process that imports "
                  f"{'it' if len(bad) == 1 else 'them'} loses free-threading entirely, including for "
                  f"other libraries loaded in that process.", file=sys.stderr)
    return exit_code(probes)


if __name__ == "__main__":
    sys.exit(main())
