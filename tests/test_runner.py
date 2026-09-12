import json
import unittest

from bench import run, worker
from bench.workloads import Workload


class EnvInfoTest(unittest.TestCase):
    def test_env_info_reports_build_and_gil_state(self):
        info = worker.env_info()
        for key in ("version", "executable", "free_threaded_build", "gil_enabled", "platform"):
            self.assertIn(key, info)
        self.assertIsInstance(info["free_threaded_build"], bool)
        self.assertIsInstance(info["gil_enabled"], bool)


class MeasureTest(unittest.TestCase):
    def test_measure_times_each_repeat_and_reports_median(self):
        calls = []

        def fn(n_workers):
            calls.append(n_workers)
            return n_workers * 7

        rec = worker.measure(fn, workers=3, repeats=3)
        self.assertEqual(calls, [3, 3, 3])
        self.assertEqual(rec["workers"], 3)
        self.assertEqual(len(rec["seconds"]), 3)
        self.assertEqual(rec["median"], sorted(rec["seconds"])[1])
        self.assertEqual(rec["result"], "21")


class RunWorkloadTest(unittest.TestCase):
    def test_missing_requirement_is_reported_as_skipped(self):
        spec = Workload("fake", "library", lambda n: n, "x", requires=("definitely_not_a_real_module_xyz",))
        out = worker.run_workload(spec, workers_list=[1, 2], repeats=1)
        self.assertEqual(len(out), 2)
        for rec in out:
            self.assertIn("definitely_not_a_real_module_xyz", rec["skipped"])
            self.assertIsNone(rec["median"])

    def test_exception_in_workload_is_captured_not_raised(self):
        def boom(n):
            raise ValueError("kaboom")

        spec = Workload("boom", "cpu", boom, "x")
        out = worker.run_workload(spec, workers_list=[1], repeats=1)
        self.assertIn("kaboom", out[0]["error"])
        self.assertIsNone(out[0]["median"])

    def test_records_carry_gil_state_and_identity(self):
        spec = Workload("ok", "cpu", lambda n: n, "x")
        out = worker.run_workload(spec, workers_list=[1, 4], repeats=2)
        self.assertEqual([r["workers"] for r in out], [1, 4])
        for rec in out:
            self.assertEqual(rec["workload"], "ok")
            self.assertEqual(rec["category"], "cpu")
            self.assertIsInstance(rec["gil_enabled"], bool)


class ConfigsTest(unittest.TestCase):
    def test_build_configs_three_main_configs_when_nothing_forced(self):
        cfgs = run.build_configs("/py312", "/py314t", forced_workloads=[])
        labels = [c["label"] for c in cfgs]
        self.assertEqual(labels, ["3.12", "3.14 (GIL on)", "3.14t (free-threaded)"])
        by = {c["label"]: c for c in cfgs}
        self.assertEqual(by["3.12"]["python"], "/py312")
        self.assertEqual(by["3.14 (GIL on)"]["env"]["PYTHON_GIL"], "1")
        self.assertNotIn("PYTHON_GIL", by["3.14t (free-threaded)"]["env"])

    def test_forced_gil_off_config_added_only_for_named_workloads(self):
        cfgs = run.build_configs("/py312", "/py314t", forced_workloads=["duckdb_query"])
        forced = cfgs[-1]
        self.assertEqual(forced["label"], "3.14t (PYTHON_GIL=0 forced)")
        self.assertEqual(forced["env"]["PYTHON_GIL"], "0")
        self.assertEqual(forced["only_workloads"], ["duckdb_query"])

    def test_every_config_pins_blas_to_one_thread(self):
        for cfg in run.build_configs("/a", "/b", forced_workloads=["x"]):
            self.assertEqual(cfg["env"]["OPENBLAS_NUM_THREADS"], "1")
            self.assertEqual(cfg["env"]["OMP_NUM_THREADS"], "1")


class SelectWorkloadsTest(unittest.TestCase):
    def test_default_is_core_only(self):
        names = [s.name for s in run.select_workloads(only=set(), libs=False)]
        self.assertIn("cpu_primes", names)
        self.assertNotIn("numpy_matmul", names)

    def test_libs_flag_adds_library_workloads(self):
        names = [s.name for s in run.select_workloads(only=set(), libs=True)]
        self.assertIn("cpu_primes", names)
        self.assertIn("numpy_matmul", names)

    def test_only_overrides_tier(self):
        names = [s.name for s in run.select_workloads(only={"numpy_matmul"}, libs=False)]
        self.assertEqual(names, ["numpy_matmul"])

    def test_packages_filter_selects_library_workloads_requiring_them(self):
        names = [s.name for s in run.select_workloads(only=set(), libs=False, packages={"pandas"})]
        self.assertEqual(names, ["pandas_groupby"])


class MergeTest(unittest.TestCase):
    def test_merge_replaces_records_for_rerun_workloads_and_keeps_others(self):
        existing = {"records": [{"config": "3.12", "workload": "a", "workers": 1},
                                {"config": "3.12", "workload": "b", "workers": 1}],
                    "gil_support": {"numpy": {"status": "SUPPORTED"}},
                    "configs": [{"label": "3.12", "info": {"version": "3.12.13"}}]}
        new = {"records": [{"config": "3.12", "workload": "b", "workers": 4}],
               "gil_support": {"duckdb": {"status": "REENABLES_GIL"}},
               "configs": [{"label": "3.12", "info": None}, {"label": "3.14t", "info": {"version": "3.14.6"}}]}
        merged = run.merge_results(existing, new)
        self.assertEqual([(r["workload"], r["workers"]) for r in merged["records"]], [("a", 1), ("b", 4)])
        self.assertEqual(set(merged["gil_support"]), {"numpy", "duckdb"})
        self.assertEqual([c["label"] for c in merged["configs"]], ["3.12", "3.14t"])
        self.assertEqual(merged["configs"][0]["info"]["version"], "3.12.13")  # kept when new run had none


class GilSupportTest(unittest.TestCase):
    def test_gil_support_report_classifies_each_package(self):
        import sys
        rep = run.gil_support_report(sys.executable, ["json", "no_such_pkg_qq"])
        self.assertEqual(set(rep), {"json", "no_such_pkg_qq"})
        for entry in rep.values():
            self.assertIn(entry["status"], {"SUPPORTED", "PURE_PYTHON", "REENABLES_GIL", "NOT_INSTALLED", "UNKNOWN"})
            self.assertIn("detail", entry)

    def test_forced_workloads_are_those_needing_a_gil_reenabling_package(self):
        rep = {"duckdb": {"status": "REENABLES_GIL"}, "numpy": {"status": "SUPPORTED"}}
        self.assertEqual(run.forced_workloads(rep), ["duckdb_query"])
        self.assertEqual(run.forced_workloads({"numpy": {"status": "SUPPORTED"}}), [])


class WorkerCliTest(unittest.TestCase):
    def test_worker_main_emits_json_with_env_and_records(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            worker.main(["--workload", "io_sleep", "--workers", "1,2", "--repeats", "1",
                         "--kwargs", json.dumps({"waits": 1, "seconds": 0.001})])
        doc = json.loads(buf.getvalue())
        self.assertIn("env", doc)
        self.assertEqual([r["workers"] for r in doc["records"]], [1, 2])
        self.assertEqual(doc["records"][0]["result"], "1")


if __name__ == "__main__":
    unittest.main()


class WarmupTest(unittest.TestCase):
    def test_warmup_runs_fn_once_untimed_before_measuring(self):
        calls = []
        spec = Workload("w", "library", lambda n, **kw: calls.append(n), "x", warmup=True)
        out = worker.run_workload(spec, workers_list=[4], repeats=2)
        self.assertEqual(calls, [1, 4, 4])  # one untimed warm-up at 1 worker, then 2 timed repeats
        self.assertEqual(len(out[0]["seconds"]), 2)

    def test_warmup_is_on_by_default_for_every_workload(self):
        calls = []
        spec = Workload("w", "cpu", lambda n, **kw: calls.append(n), "x")
        worker.run_workload(spec, workers_list=[4], repeats=1)
        self.assertEqual(calls, [1, 4])

    def test_warmup_can_be_disabled(self):
        calls = []
        spec = Workload("w", "cpu", lambda n, **kw: calls.append(n), "x", warmup=False)
        worker.run_workload(spec, workers_list=[4], repeats=1)
        self.assertEqual(calls, [4])


class CollectFactsTest(unittest.TestCase):
    def test_collect_facts_runs_in_the_configured_interpreter_and_env(self):
        import platform
        import sys
        cfg = {"label": "x", "python": sys.executable, "env": {}}
        f = run.collect_facts(cfg)
        # against platform.python_version(), the same source bench.facts reports from:
        # version_info[:3] drops the release level, so on a pre-release interpreter it would
        # compare "3.14.0rc2" with "3.14.0" and fail on a perfectly good build.
        self.assertEqual(f["version"], platform.python_version())
        # still pinned to this interpreter, whatever platform's formatting does
        self.assertTrue(f["version"].startswith(".".join(map(str, sys.version_info[:3]))), f["version"])
        self.assertIn("sizeof", f)


class MergeKeepsConfigsTest(unittest.TestCase):
    def test_configs_absent_from_the_new_run_are_kept(self):
        existing = {"records": [{"config": "3.14t (PYTHON_GIL=0 forced)", "workload": "duckdb_query", "workers": 1}],
                    "configs": [{"label": "3.12", "info": {"version": "3.12.13"}},
                                {"label": "3.14t (PYTHON_GIL=0 forced)", "info": {"version": "3.14.6"}}]}
        new = {"records": [{"config": "3.12", "workload": "cpu_primes", "workers": 1}],
               "configs": [{"label": "3.12", "info": {"version": "3.12.13"}}]}
        merged = run.merge_results(existing, new)
        self.assertEqual([c["label"] for c in merged["configs"]], ["3.12", "3.14t (PYTHON_GIL=0 forced)"])


class VenvPythonTest(unittest.TestCase):
    def test_posix_layout(self):
        self.assertEqual(run.venv_python("/r/.venv312", win=False), "/r/.venv312/bin/python")

    def test_windows_layout(self):
        self.assertEqual(run.venv_python("C:/r/.venv312", win=True), "C:/r/.venv312/Scripts/python.exe")


class StatsTest(unittest.TestCase):
    def test_stats_summarise_a_sample(self):
        s = worker.stats([1.0, 2.0, 3.0, 4.0, 10.0])
        self.assertEqual(s["n"], 5)
        self.assertEqual(s["median"], 3.0)
        self.assertEqual(s["mean"], 4.0)
        self.assertEqual(s["min"], 1.0)
        self.assertEqual(s["max"], 10.0)
        self.assertAlmostEqual(s["stdev"], 3.5355, places=3)  # sample stdev
        self.assertAlmostEqual(s["cv"], 3.5355 / 4.0, places=3)

    def test_stats_single_sample_has_zero_spread(self):
        s = worker.stats([2.5])
        self.assertEqual(s["stdev"], 0.0)
        self.assertEqual(s["cv"], 0.0)
        self.assertEqual(s["min"], s["max"])

    def test_measure_record_carries_stats(self):
        rec = worker.measure(lambda n: n, workers=2, repeats=4)
        self.assertEqual(rec["stats"]["n"], 4)
        self.assertEqual(rec["stats"]["median"], rec["median"])


class DefaultRepeatsTest(unittest.TestCase):
    def test_default_repeats_is_five_everywhere(self):
        from bench import compare_lib
        self.assertEqual(run.DEFAULT_REPEATS, 5)
        self.assertEqual(compare_lib.DEFAULT_REPEATS, 5)
        self.assertEqual(worker.DEFAULT_REPEATS, 5)
