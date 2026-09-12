import importlib.util
import unittest

from bench import lib_workloads as lw


def needs(*pkgs):
    missing = [p for p in pkgs if importlib.util.find_spec(p) is None]
    return unittest.skipIf(missing, f"optional library not installed: {', '.join(missing)}")


class RunTasksTest(unittest.TestCase):
    def test_run_tasks_runs_every_task_once_in_order(self):
        out = lw.run_tasks(lambda i: i * 10, tasks=5, n_workers=2)
        self.assertEqual(out, [0, 10, 20, 30, 40])


@needs("numpy")
class NumpyTest(unittest.TestCase):
    def test_numpy_small_ops_result_independent_of_thread_count(self):
        one = lw.numpy_small_ops(1, tasks=4, iters=200)
        four = lw.numpy_small_ops(4, tasks=4, iters=200)
        self.assertEqual(one, four)
        self.assertGreater(one, 0)

    def test_numpy_matmul_result_independent_of_thread_count(self):
        one = lw.numpy_matmul(1, tasks=4, size=32, reps=2)
        four = lw.numpy_matmul(4, tasks=4, size=32, reps=2)
        self.assertAlmostEqual(one, four, places=3)


@needs("numpy", "pandas")
class PandasTest(unittest.TestCase):
    def test_pandas_groupby_result_independent_of_thread_count(self):
        one = lw.pandas_groupby(1, tasks=4, rows=2000)
        four = lw.pandas_groupby(4, tasks=4, rows=2000)
        self.assertEqual(one, four)


@needs("duckdb")
class DuckdbTest(unittest.TestCase):
    def test_duckdb_query_returns_exact_sum_of_squares(self):
        # sum(i*i) for i in range(100) == 328350, times 4 tasks
        self.assertEqual(lw.duckdb_query(2, tasks=4, rows=100), 4 * 328350)


@needs("fastapi", "uvicorn")
class FastapiTest(unittest.TestCase):
    def test_fastapi_sync_cpu_counts_successful_requests(self):
        self.assertEqual(lw.fastapi_sync_cpu(2, requests=4, prime_limit=500), 4)

    def test_fastapi_async_json_counts_successful_requests(self):
        self.assertEqual(lw.fastapi_async_json(2, requests=6), 6)


class RegistryTest(unittest.TestCase):
    def test_every_library_workload_declares_requirements(self):
        self.assertGreaterEqual(len(lw.WORKLOADS), 6)
        for spec in lw.WORKLOADS:
            self.assertEqual(spec.category, "library")
            self.assertTrue(spec.requires, f"{spec.name} must list required packages")
            self.assertTrue(callable(spec.fn))


if __name__ == "__main__":
    unittest.main()


@needs("sklearn")
class SklearnTest(unittest.TestCase):
    def test_sklearn_fit_result_independent_of_thread_count(self):
        one = lw.sklearn_fit(1, tasks=2, samples=300, trees=3)
        two = lw.sklearn_fit(2, tasks=2, samples=300, trees=3)
        self.assertEqual(one, two)
        self.assertGreater(one, 0)


class LibraryWarmupTest(unittest.TestCase):
    def test_all_library_workloads_request_warmup(self):
        for spec in lw.WORKLOADS:
            self.assertTrue(spec.warmup, f"{spec.name} should warm up (lazy imports / server start)")

    def test_sklearn_is_registered(self):
        self.assertIn("sklearn_fit", lw.WORKLOADS_BY_NAME)
        self.assertIn("sklearn", lw.WORKLOADS_BY_NAME["sklearn_fit"].requires)
