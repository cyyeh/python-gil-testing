import json
import tempfile
import unittest
from pathlib import Path

from bench import report

CFGS = ["3.12", "3.14 (GIL on)", "3.14t (free-threaded)"]


def rec(config, workload, workers, median, category="cpu", gil=True, **over):
    r = {"config": config, "workload": workload, "category": category, "workers": workers,
         "seconds": [median] * 3, "median": median, "result": "1", "gil_enabled": gil,
         "skipped": None, "error": None}
    r.update(over)
    return r


def facts(version, ft):
    return {"version": version, "abiflags": "t" if ft else "", "soabi": "x", "free_threaded_build": ft,
            "gil_enabled": not ft, "gil_flag": None, "thread_inherit_context": int(ft), "context_aware_warnings": int(ft),
            "jit_available": False, "gc_threshold": [2000, 10, 10],
            "sizeof": {"object()": 32 if ft else 16, "[]": 56, "1": 44 if ft else 28}, "compiler": "clang"}


def make_doc(with_libs=False):
    doc = {
        "generated_at": "2026-09-12T12:00:00", "host": {"os": "macOS", "machine": "arm64", "cpu": "Apple M5", "ncpu": 10},
        "workers": [1, 2, 4, 8], "repeats": 3, "quick": False,
        "configs": [{"label": c, "python": "/py", "env": {}, "note": "n",
                     "info": {"version": "3.12.13" if c == "3.12" else "3.14.6", "free_threaded_build": c != "3.12"},
                     "facts": facts("3.12.13" if c == "3.12" else "3.14.6", c != "3.12"), "stderr": {}} for c in CFGS],
        "workloads": [{"name": "cpu_primes", "category": "cpu", "description": "count primes", "requires": []},
                      {"name": "io_sleep", "category": "io", "description": "sleep", "requires": []},
                      {"name": "numpy_matmul", "category": "library", "description": "matmul", "requires": ["numpy"]},
                      {"name": "duckdb_query", "category": "library", "description": "query", "requires": ["duckdb"]}],
        "gil_support": {},
        "records": [],
    }
    for c in CFGS:
        for w in [1, 2, 4, 8]:
            # cpu: only 3.14t scales; io: everyone scales
            t = 1.0 / w if c == "3.14t (free-threaded)" else 1.0
            doc["records"].append(rec(c, "cpu_primes", w, t * (1.08 if c != "3.12" else 1.0), gil=c != "3.14t (free-threaded)"))
            doc["records"].append(rec(c, "io_sleep", w, 0.2, "io", gil=c != "3.14t (free-threaded)"))
    if with_libs:
        doc["gil_support"] = {"numpy": {"status": "SUPPORTED", "detail": "ok", "version": "2.5.3", "wheel_abi": "cp314t",
                                        "reenabled_by": None, "extension_modules": ["a"]},
                              "duckdb": {"status": "REENABLES_GIL", "detail": "'_duckdb' has not declared", "version": "1.5.5",
                                         "wheel_abi": "cp314t", "reenabled_by": "_duckdb", "extension_modules": ["_duckdb"]}}
        for c in CFGS:
            for w in [1, 2, 4, 8]:
                doc["records"].append(rec(c, "numpy_matmul", w, 0.5 / w, "library", gil=c != "3.14t (free-threaded)"))
                doc["records"].append(rec(c, "duckdb_query", w, 0.5 / w, "library", gil=True))  # import re-enabled it
    return doc


class PivotTest(unittest.TestCase):
    def test_pivot_groups_by_workload_config_workers(self):
        p = report.pivot(make_doc())
        self.assertEqual(p["cpu_primes"]["3.12"][4]["median"], 1.0)
        self.assertAlmostEqual(p["cpu_primes"]["3.14t (free-threaded)"][4]["median"], 0.27, places=2)


class SpeedupTest(unittest.TestCase):
    def test_speedup_is_one_worker_time_over_max_worker_time(self):
        p = report.pivot(make_doc())
        self.assertAlmostEqual(report.speedup(p["cpu_primes"]["3.14t (free-threaded)"]), 8.0)
        self.assertAlmostEqual(report.speedup(p["cpu_primes"]["3.12"]), 1.0)

    def test_speedup_none_when_data_missing(self):
        self.assertIsNone(report.speedup({}))
        self.assertIsNone(report.speedup({1: {"median": None}, 8: {"median": 1}}))

    def test_single_thread_ratio_against_baseline(self):
        p = report.pivot(make_doc())
        self.assertAlmostEqual(report.single_thread_ratio(p["cpu_primes"], "3.14t (free-threaded)", "3.12"), 1.08)


class ChartTest(unittest.TestCase):
    def test_line_chart_svg_has_one_path_and_markers_per_series(self):
        svg = report.line_chart_svg({"A": {1: 1.0, 2: 0.5}, "B": {1: 1.0, 2: 1.0}}, [1, 2], "t")
        self.assertIn("<svg", svg)
        self.assertEqual(svg.count('class="series-line'), 2)
        self.assertGreaterEqual(svg.count("<circle") + svg.count("<rect class=\"marker") + svg.count("<path class=\"marker"), 4)

    def test_line_chart_skips_missing_points(self):
        svg = report.line_chart_svg({"A": {1: 1.0, 2: None, 4: 0.3}}, [1, 2, 4], "t")
        self.assertIn("<svg", svg)


class RenderTest(unittest.TestCase):
    def test_core_report_has_every_section_and_no_library_section(self):
        html = report.render_html(make_doc())
        for needle in ("cpu_primes", "io_sleep", "3.14t (free-threaded)", "<svg", "Implementation differences",
                       "Key findings", "Bare Python", "Reproduce"):
            self.assertIn(needle, html)
        self.assertNotIn("Library workloads", html)

    def test_library_section_present_only_with_library_records(self):
        html = report.render_html(make_doc(with_libs=True))
        self.assertIn("Library workloads", html)
        self.assertIn("numpy_matmul", html)
        self.assertIn("REENABLES_GIL", html)

    def test_flags_free_threaded_runs_where_gil_was_reenabled(self):
        html = report.render_html(make_doc(with_libs=True))
        self.assertIn("GIL re-enabled", html)

    def test_measured_facts_table_shows_object_size_difference(self):
        html = report.render_html(make_doc())
        self.assertIn("object()", html)
        self.assertIn(">32<", html)

    def test_html_is_self_contained(self):
        html = report.render_html(make_doc(with_libs=True))
        self.assertNotIn("<script src=", html)
        self.assertNotIn("<link ", html)


class KeyFindingsTest(unittest.TestCase):
    def test_findings_mention_cpu_scaling_and_io_parity(self):
        text = " ".join(report.key_findings(make_doc()))
        self.assertIn("cpu_primes", text)
        self.assertIn("8.0", text)
        self.assertIn("I/O", text)


class MainTest(unittest.TestCase):
    def test_main_writes_html_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "r.json"
            src.write_text(json.dumps(make_doc()))
            out = Path(d) / "report.html"
            report.main(["--in", str(src), "--out", str(out)])
            self.assertIn("<title>", out.read_text())


if __name__ == "__main__":
    unittest.main()
