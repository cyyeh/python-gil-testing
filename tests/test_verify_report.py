"""The report verifier has to fail on a broken page, or it is only decoration."""

import re
import unittest

from bench import report, verify_report as v

from tests.test_report import make_doc


def rendered(with_libs=False):
    doc = make_doc(with_libs)
    return doc, report.render_html(doc)


class CleanReportTest(unittest.TestCase):
    def test_a_freshly_rendered_report_verifies(self):
        doc, page = rendered(with_libs=True)
        problems, counts = v.verify(doc, page)
        self.assertEqual(problems, [])
        self.assertTrue(all("checked" in line for line in counts))

    def test_it_actually_looks_at_something(self):
        doc, page = rendered(with_libs=True)
        _, counts = v.verify(doc, page)
        totals = [int(m.group(1)) for line in counts for m in [re.search(r"(\d+) checked", line)] if m]
        self.assertEqual(len(totals), 5)
        for t in totals:
            self.assertGreater(t, 0, counts)  # a check that counts nothing passes for free


class BrokenPageTest(unittest.TestCase):
    def assert_flags(self, page, doc=None, needle=""):
        problems, _ = v.verify(doc or make_doc(with_libs=True), page)
        self.assertTrue(problems, "the verifier accepted a page it should have rejected")
        if needle:
            self.assertTrue(any(needle in p for p in problems), problems)

    def test_detects_a_wrong_time_in_a_table(self):
        doc, page = rendered(with_libs=True)
        self.assert_flags(page.replace("1.080 s", "9.999 s", 1), doc)

    def test_detects_a_wrong_speed_up_column(self):
        doc, page = rendered(with_libs=True)
        self.assert_flags(page.replace('<td class="num">8.0x</td>', '<td class="num">2.0x</td>', 1),
                          doc, needle="speed-up")

    def test_detects_a_wrong_number_in_a_generated_finding(self):
        doc, page = rendered(with_libs=True)
        self.assert_flags(page.replace("3.14t (free-threaded): 8.0x", "3.14t (free-threaded): 2.0x", 1),
                          doc, needle="finding")

    def test_detects_a_chart_plotting_the_wrong_value(self):
        doc, page = rendered(with_libs=True)
        moved = re.sub(r'(<path class="series-line s1" d="M[\d.]+ )([\d.]+)',
                       lambda m: m.group(1) + f"{float(m.group(2)) + 25:.1f}", page, count=1)
        self.assert_flags(moved, doc, needle="chart")

    def test_detects_a_dropped_marker(self):
        doc, page = rendered(with_libs=True)
        self.assert_flags(page.replace('<circle class="marker s1"', "<circle class=\"gone\"", 1), doc, needle="chart")

    def test_detects_a_missing_gil_reenabled_flag(self):
        doc, page = rendered(with_libs=True)
        self.assert_flags(page.replace("&#9888;", "", 2), doc)

    def test_detects_stats_that_no_longer_match_the_raw_timings(self):
        doc, page = rendered()
        doc["records"][0]["stats"] = dict(doc["records"][0]["stats"], stdev=42.0)
        self.assert_flags(page, doc, needle="stats[stdev]")

    def test_detects_a_wrong_fact_or_host_number(self):
        doc, page = rendered()
        self.assert_flags(page, dict(doc, host=dict(doc["host"], ncpu=999)), needle="header")

    def test_a_missing_section_is_a_failure_not_a_silent_pass(self):
        doc, page = rendered(with_libs=True)
        start = page.index('<table class="data summary">')
        self.assert_flags(page[:start] + page[page.index("</table>", start):], doc, needle="not what was expected")


class WorkInvarianceTest(unittest.TestCase):
    def test_reports_a_workload_whose_total_work_moves_with_the_thread_count(self):
        doc = make_doc()
        for r in doc["records"]:
            if r["workload"] == "io_sleep":
                r["result"] = str(20 * r["workers"])
        notes = " ".join(v.work_invariance(doc))
        self.assertIn("io_sleep", notes)
        self.assertIn("VARIES", notes)

    def test_separates_float_rounding_from_a_real_difference(self):
        doc = make_doc()
        for i, r in enumerate(r for r in doc["records"] if r["workload"] == "cpu_primes"):
            r["result"] = repr(1.0 + i * 1e-13)
        notes = [n for n in v.work_invariance(doc) if n.startswith("cpu_primes")]
        self.assertTrue(notes and "VARIES" not in notes[0], notes)

    def test_silent_when_every_checksum_matches(self):
        self.assertEqual(v.work_invariance(make_doc()), [])


if __name__ == "__main__":
    unittest.main()
