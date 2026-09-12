import unittest

from bench import workloads as w


class CountPrimesTest(unittest.TestCase):
    def test_count_primes_known_value(self):
        # 25 primes below 100
        self.assertEqual(w.count_primes(2, 100), 25)

    def test_count_primes_empty_range(self):
        self.assertEqual(w.count_primes(50, 50), 0)


class SplitRangeTest(unittest.TestCase):
    def test_split_range_covers_whole_range_contiguously(self):
        chunks = w.split_range(0, 10, 3)
        self.assertEqual(chunks, [(0, 4), (4, 7), (7, 10)])

    def test_split_range_single_chunk(self):
        self.assertEqual(w.split_range(2, 9, 1), [(2, 9)])


class CpuWorkloadTest(unittest.TestCase):
    def test_cpu_primes_result_independent_of_thread_count(self):
        # 303 primes below 2000
        self.assertEqual(w.cpu_primes(1, limit=2000), 303)
        self.assertEqual(w.cpu_primes(4, limit=2000), 303)

    def test_cpu_float_result_independent_of_thread_count(self):
        one = w.cpu_float(1, iterations=4000)
        four = w.cpu_float(4, iterations=4000)
        self.assertAlmostEqual(one, four, places=6)


class IoWorkloadTest(unittest.TestCase):
    def test_io_sleep_reports_total_waits_performed(self):
        self.assertEqual(w.io_sleep(3, waits=2, seconds=0.001), 6)


class ContendedWorkloadTest(unittest.TestCase):
    def test_contended_list_append_keeps_every_item_and_total_is_fixed(self):
        self.assertEqual(w.contended_list_append(1, total=4000), 4000)
        self.assertEqual(w.contended_list_append(4, total=4000), 4000)
        self.assertEqual(w.contended_list_append(3, total=4000), 4000)  # uneven split still covers everything

    def test_contended_dict_update_counts_every_increment_and_total_is_fixed(self):
        self.assertEqual(w.contended_dict_update(1, total=4000, keys=8), 4000)
        self.assertEqual(w.contended_dict_update(4, total=4000, keys=8), 4000)


class MultiprocessingWorkloadTest(unittest.TestCase):
    def test_mp_primes_matches_threaded_result(self):
        self.assertEqual(w.mp_primes(2, limit=2000), 303)


class RegistryTest(unittest.TestCase):
    def test_every_workload_has_name_category_and_callable(self):
        self.assertGreaterEqual(len(w.WORKLOADS), 5)
        for spec in w.WORKLOADS:
            self.assertIn(spec.category, {"cpu", "io", "contended", "multiprocessing"})
            self.assertTrue(callable(spec.fn))
            self.assertTrue(spec.name)
            self.assertTrue(spec.description)

    def test_registry_names_are_unique(self):
        names = [s.name for s in w.WORKLOADS]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
