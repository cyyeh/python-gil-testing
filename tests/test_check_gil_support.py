import importlib.util
import sys
import sysconfig
import unittest

import check_gil_support as cgs

FT_BUILD = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def probe(**over):
    base = {
        "name": "pkg", "importable": True, "error": None, "free_threaded_build": True,
        "gil_before": False, "gil_after": False, "gil_warnings": [], "reenabled_by": None,
        "extension_modules": [], "dist_name": "pkg", "version": "1.0", "wheel_tags": ["py3-none-any"],
    }
    base.update(over)
    return base


class ClassifyTest(unittest.TestCase):
    def test_not_free_threaded_interpreter_is_unknown(self):
        status, _ = cgs.classify(probe(free_threaded_build=False))
        self.assertEqual(status, "UNKNOWN")

    def test_import_failure_is_not_installed(self):
        status, detail = cgs.classify(probe(importable=False, error="ModuleNotFoundError: No module named 'pkg'"))
        self.assertEqual(status, "NOT_INSTALLED")
        self.assertIn("No module named", detail)

    def test_gil_already_on_before_import_is_unknown(self):
        status, detail = cgs.classify(probe(gil_before=True, gil_after=True))
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("PYTHON_GIL", detail)

    def test_extension_that_reenables_gil(self):
        status, detail = cgs.classify(probe(gil_after=True, reenabled_by="_duckdb", extension_modules=["_duckdb"]))
        self.assertEqual(status, "REENABLES_GIL")
        self.assertIn("_duckdb", detail)

    def test_pure_python_package(self):
        status, _ = cgs.classify(probe())
        self.assertEqual(status, "PURE_PYTHON")

    def test_extensions_that_keep_gil_off_are_supported(self):
        status, detail = cgs.classify(probe(extension_modules=["numpy._core._multiarray_umath", "numpy.linalg._umath_linalg"]))
        self.assertEqual(status, "SUPPORTED")
        self.assertIn("2", detail)


class WheelAbiTest(unittest.TestCase):
    def test_free_threaded_tag(self):
        self.assertEqual(cgs.abi_label(["cp314-cp314t-macosx_14_0_arm64"]), "cp314t")

    def test_pure_python_tag(self):
        self.assertEqual(cgs.abi_label(["py3-none-any"]), "pure (none-any)")

    def test_abi3_tag(self):
        self.assertEqual(cgs.abi_label(["cp38-abi3-manylinux_2_17_x86_64"]), "abi3")

    def test_unknown_when_no_tags(self):
        self.assertEqual(cgs.abi_label([]), "?")


class ReenabledByParserTest(unittest.TestCase):
    def test_extracts_module_name_from_runtime_warning(self):
        msg = ("The global interpreter lock (GIL) has been enabled to load module '_duckdb', "
               "which has not declared that it can run safely without the GIL.")
        self.assertEqual(cgs.reenabled_module([msg]), "_duckdb")

    def test_none_when_no_warning(self):
        self.assertIsNone(cgs.reenabled_module([]))


class ProbeIntegrationTest(unittest.TestCase):
    def test_probe_stdlib_pure_python_module(self):
        p = cgs.run_probe(sys.executable, "json")
        self.assertTrue(p["importable"])
        self.assertEqual(p["free_threaded_build"], FT_BUILD)
        self.assertEqual(p["extension_modules"], [])
        if FT_BUILD:
            self.assertFalse(p["gil_after"])

    def test_probe_missing_module(self):
        p = cgs.run_probe(sys.executable, "no_such_module_xyz_123")
        self.assertFalse(p["importable"])
        self.assertIn("No module named", p["error"])

    @unittest.skipIf(importlib.util.find_spec("numpy") is None, "numpy not installed")
    def test_probe_reports_distribution_metadata(self):
        p = cgs.run_probe(sys.executable, "numpy")
        self.assertEqual(p["dist_name"], "numpy")
        self.assertRegex(p["version"], r"^\d+\.\d+")
        self.assertTrue(p["wheel_tags"])

    @unittest.skipIf(importlib.util.find_spec("typing_extensions") is None, "typing-extensions not installed")
    def test_probe_accepts_distribution_name_that_differs_from_import_name(self):
        # dist name 'typing-extensions' vs import name 'typing_extensions'
        p = cgs.run_probe(sys.executable, "typing-extensions")
        self.assertTrue(p["importable"])
        self.assertEqual(p["import_name"], "typing_extensions")


class RenderTest(unittest.TestCase):
    def test_table_lists_each_package_with_status(self):
        rows = [probe(name="a"), probe(name="b", gil_after=True, reenabled_by="_b", extension_modules=["_b"])]
        text = cgs.render_table(rows, {"version": "3.14.6", "free_threaded_build": True})
        self.assertIn("PURE_PYTHON", text)
        self.assertIn("REENABLES_GIL", text)
        self.assertIn("a", text)

    def test_exit_code_is_nonzero_when_any_package_reenables_gil(self):
        self.assertEqual(cgs.exit_code([probe()]), 0)
        self.assertEqual(cgs.exit_code([probe(gil_after=True, reenabled_by="x")]), 1)
        self.assertEqual(cgs.exit_code([probe(importable=False)]), 1)


if __name__ == "__main__":
    unittest.main()
