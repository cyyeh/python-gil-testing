"""Collect build/runtime facts about the *current* interpreter (run as a subprocess
per configuration by bench.run) so the report can show measured, not remembered,
implementation differences.

    python -m bench.facts
"""

from __future__ import annotations

import gc
import json
import platform
import sys
import sysconfig


class _Plain:
    pass


class _Slotted:
    __slots__ = ("a", "b")


def collect() -> dict:
    samples = {
        "object()": object(),
        "1": 1,
        "2**40": 2 ** 40,
        '"abc"': "abc",
        "(1, 2)": (1, 2),
        "[]": [],
        "{}": {},
        "set()": set(),
        "class instance": _Plain(),
        "__slots__ instance": _Slotted(),
        "lambda": (lambda: 0),
    }
    return {
        "version": platform.python_version(),
        "version_string": sys.version,
        "abiflags": sys.abiflags,
        "soabi": sysconfig.get_config_var("SOABI"),
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": bool(getattr(sys, "_is_gil_enabled", lambda: True)()),
        "gil_flag": getattr(sys.flags, "gil", None),  # None = build default, 0/1 = overridden
        "thread_inherit_context": getattr(sys.flags, "thread_inherit_context", None),
        "context_aware_warnings": getattr(sys.flags, "context_aware_warnings", None),
        "jit_available": bool(sys._jit.is_available()) if hasattr(sys, "_jit") else None,
        "gc_threshold": list(gc.get_threshold()),
        "sizeof": {k: sys.getsizeof(v) for k, v in samples.items()},
        "compiler": platform.python_compiler(),
    }


if __name__ == "__main__":
    print(json.dumps(collect()))
