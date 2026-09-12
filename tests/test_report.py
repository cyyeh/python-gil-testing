import json
import tempfile
import unittest
from pathlib import Path

from bench import report

CFGS = ["3.12", "3.14 (GIL on)", "3.14t (free-threaded)"]


def rec(config, workload, workers, median, category="cpu", gil=True, spread=0.02, **over):
    from bench.worker import stats
    seconds = [median * (1 - spread), median, median * (1 + spread), median, median]
    r = {"config": config, "workload": workload, "category": category, "workers": workers,
         "seconds": seconds, "median": median, "stats": stats(seconds), "result": "1", "gil_enabled": gil,
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


class StatsInReportTest(unittest.TestCase):
    def test_cell_stats_prefers_recorded_stats_and_falls_back_to_seconds(self):
        r = rec("3.12", "cpu_primes", 1, 1.0)
        self.assertEqual(report.cell_stats(r)["n"], 5)
        legacy = {"seconds": [1.0, 1.2, 0.8], "median": 1.0}
        self.assertAlmostEqual(report.cell_stats(legacy)["stdev"], 0.2, places=6)
        self.assertIsNone(report.cell_stats({"seconds": [], "median": None}))

    def test_noisy_cells_are_flagged_above_the_cv_threshold(self):
        quiet = rec("3.12", "w", 1, 1.0, spread=0.02)
        noisy = rec("3.12", "w", 1, 1.0, spread=0.30)
        self.assertFalse(report.is_noisy(quiet))
        self.assertTrue(report.is_noisy(noisy))

    def test_table_shows_plus_minus_and_marks_noisy_cells(self):
        by_config = {"3.12": {1: rec("3.12", "w", 1, 1.0, spread=0.30), 8: rec("3.12", "w", 8, 0.5)}}
        html = report.workload_table(by_config, [1, 8], ["3.12"])
        self.assertIn("&plusmn;", html)
        self.assertIn('class="noisy ', html)

    def test_key_findings_include_a_noise_summary(self):
        text = " ".join(report.key_findings(make_doc()))
        self.assertIn("noise", text.lower())
        self.assertIn("%", text)


class ChartTest(unittest.TestCase):
    def test_line_chart_draws_error_bars_when_ranges_given(self):
        svg = report.line_chart_svg({"A": {1: 1.0, 2: 0.5}}, [1, 2], "t",
                                    ranges={"A": {1: (0.9, 1.1), 2: (0.4, 0.6)}})
        self.assertEqual(svg.count('class="range'), 2)

    def test_line_chart_without_ranges_has_no_error_bars(self):
        svg = report.line_chart_svg({"A": {1: 1.0, 2: 0.5}}, [1, 2], "t")
        self.assertEqual(svg.count('class="range'), 0)

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
                       "Key findings", "Bare Python", "Reproduce", "&plusmn;", 'class="range'):
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


class HoverExplanationTest(unittest.TestCase):
    """Rule: every glyph, badge, flag or abbreviation in generated HTML explains itself on hover/focus."""

    GLYPH_CLASSES = ("flag", "noisy", "pm", "badge", "abbr")

    def test_every_glyph_has_a_data_tip_and_is_focusable(self):
        import re
        html = report.render_html(make_doc(with_libs=True))
        tags = re.findall(r"<span[^>]*>", html)
        glyphs = [t for t in tags if re.search(r'class="(?:[^"]*\s)?(%s)(?=[\s"])' % "|".join(self.GLYPH_CLASSES), t)]
        self.assertGreater(len(glyphs), 5)
        for t in glyphs:
            self.assertIn('data-tip="', t, t)
            self.assertIn('tabindex="0"', t, t)
            self.assertNotIn(" title=", t, t)  # native tooltips are delayed and invisible on touch

    def test_summary_table_gil_flag_is_explained(self):
        html = report.render_html(make_doc(with_libs=True))
        summary = html[html.index('<table class="data summary">'):html.index("</table>", html.index('<table class="data summary">'))]
        self.assertIn("&#9888;", summary)
        self.assertIn('data-tip="GIL re-enabled', summary)

    def test_speedup_and_gil_column_headers_are_explained(self):
        html = report.render_html(make_doc())
        self.assertRegex(html, r'<th[^>]*>\s*<span class="abbr[^>]*data-tip="[^"]*1-thread[^"]*"[^>]*>speed-up')
        self.assertRegex(html, r'data-tip="[^"]*_is_gil_enabled[^"]*"[^>]*>GIL enabled at run')

    def test_page_ships_the_tooltip_runtime(self):
        html = report.render_html(make_doc())
        self.assertIn("querySelectorAll('[data-tip]')", html)  # the handler
        self.assertIn(".tip {", html)  # its styling
