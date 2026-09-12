import unittest

from bench import compare_lib
from tests.test_report import make_doc


class WorkloadsForTest(unittest.TestCase):
    def test_selects_library_workloads_requiring_any_of_the_packages(self):
        names = [s.name for s in compare_lib.workloads_for({"numpy"})]
        self.assertIn("numpy_small_ops", names)
        self.assertIn("numpy_matmul", names)
        self.assertIn("pandas_groupby", names)  # requires numpy + pandas
        self.assertNotIn("cpu_primes", names)
        self.assertNotIn("sklearn_fit", names)

    def test_unknown_package_selects_nothing(self):
        self.assertEqual(compare_lib.workloads_for({"definitely_unknown"}), [])


class GuidanceTest(unittest.TestCase):
    def test_guidance_tells_user_how_to_add_a_workload(self):
        text = compare_lib.guidance("foo")
        self.assertIn("bench/lib_workloads.py", text)
        self.assertIn("requires", text)
        self.assertIn("foo", text)


class SummarizeTest(unittest.TestCase):
    def test_summary_lists_speedups_per_config_and_warns_on_unsupported(self):
        doc = make_doc(with_libs=True)
        text = compare_lib.summarize(doc, ["numpy_matmul", "duckdb_query"])
        self.assertIn("numpy_matmul", text)
        self.assertIn("3.14t (free-threaded)", text)
        self.assertIn("8.0x", text)
        self.assertIn("WARNING", text)
        self.assertIn("duckdb", text)

    def test_summary_has_no_warning_when_everything_is_supported(self):
        doc = make_doc(with_libs=True)
        doc["gil_support"].pop("duckdb")
        text = compare_lib.summarize(doc, ["numpy_matmul"])
        self.assertNotIn("WARNING", text)


if __name__ == "__main__":
    unittest.main()


class InstallCommandTest(unittest.TestCase):
    def test_install_refuses_source_builds_so_missing_cp314t_wheels_fail_fast(self):
        cmd = compare_lib.install_command(["polars"], "/py314t")
        self.assertEqual(cmd[:3], ["uv", "pip", "install"])
        self.assertIn("--no-build", cmd)
        self.assertIn("polars", cmd)
        self.assertIn("/py314t", cmd)
