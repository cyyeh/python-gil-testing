import json
import unittest

from bench import facts


class FactsTest(unittest.TestCase):
    def test_facts_report_build_identity_and_object_sizes(self):
        f = facts.collect()
        for key in ("version", "abiflags", "soabi", "free_threaded_build", "gil_enabled", "gil_flag",
                    "jit_available", "gc_threshold", "sizeof", "thread_inherit_context"):
            self.assertIn(key, f)
        self.assertIn("object()", f["sizeof"])
        self.assertIn("[]", f["sizeof"])
        self.assertIsInstance(f["sizeof"]["object()"], int)

    def test_facts_are_json_serialisable(self):
        json.dumps(facts.collect())


if __name__ == "__main__":
    unittest.main()
