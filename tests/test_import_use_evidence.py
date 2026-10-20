from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_boundary_transition_probe import analyze_boundary_transitions


class ImportUseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tests").mkdir()
        (self.root / "pkg").mkdir()
        (self.root / "pkg/core.py").write_text(
            "class Config:\n    pass\nclass ExitCode:\n    OK = 0\n",
            encoding="utf-8",
        )

    def write_test(self, text):
        (self.root / "tests/test_core.py").write_text(text, encoding="utf-8")

    def graph(self, **kwargs):
        return build_dependency_graph(
            self.root, changed_files=["pkg/core.py"], max_hops=0, **kwargs,
        )

    def test_exact_reference_kinds_and_provenance(self):
        self.write_test(
            "from pkg.core import Config, ExitCode\n"
            "import pkg.core as core\n"
            "from typing import cast\n"
            "def test_contract(request: Config) -> Config:\n"
            "    fake = cast(Config, object())\n"
            "    assert ExitCode.OK == 0\n"
            "    getattr(core, 'dynamic')()\n"
            "    return core.dynamic()\n"
        )
        graph = self.graph()
        evidence = graph["scope"]["import_use_fallback_evidence"]["tests/test_core.py"]
        self.assertFalse(evidence["truncated"])
        self.assertEqual(evidence["record_count"], 6)
        records = {record["line"]: record for record in evidence["records"] if record["line"] != 4}
        self.assertEqual(records[5]["reference_kinds"], ["cast_type_argument_syntax"])
        self.assertEqual(records[6]["reference_kinds"], ["attribute_reference", "uppercase_attribute_syntax"])
        self.assertEqual(records[6]["expression"], "ExitCode.OK")
        self.assertEqual(records[7]["reference_kinds"], ["dynamic_attribute_syntax"])
        self.assertEqual(records[8]["reference_kinds"], ["attribute_reference", "call_target"])
        annotations = [record for record in evidence["records"] if record["line"] == 4]
        self.assertEqual(len(annotations), 2)
        self.assertTrue(all(record["reference_kinds"] == ["annotation"] for record in annotations))
        self.assertEqual(records[6]["import_line"], 1)
        self.assertEqual(records[6]["import_symbol"], "ExitCode")
        self.assertEqual(records[6]["matched_modules"], ["pkg.core"])
        self.assertEqual(records[6]["test_node"], "test:tests/test_core.py:test_contract")
        static = self.graph(include_import_use_test_fallback=False)
        self.assertEqual(static["scope"]["import_use_fallback_evidence"], {})
        for key in ("nodes", "edges", "issues", "status", "incomplete_causes"):
            self.assertEqual(graph[key], static[key])
        before = deepcopy(graph)
        probe = analyze_boundary_transitions(static, graph)
        self.assertEqual(probe["candidate_tests"][0]["import_use_fallback_evidence"], evidence)
        self.assertEqual(graph, before)

    def test_sampling_is_bounded_counted_and_deterministic_with_cache(self):
        self.write_test(
            "from pkg.core import ExitCode\n"
            "def test_contract():\n"
            + "    assert ExitCode.OK == 0\n" * 35
        )
        cache = self.root / "cache.json"
        graph = self.graph(cache_path=cache)
        evidence = graph["scope"]["import_use_fallback_evidence"]["tests/test_core.py"]
        self.assertEqual(evidence["record_count"], 35)
        self.assertEqual(len(evidence["records"]), 20)
        self.assertTrue(evidence["truncated"])
        hit = self.graph(cache_path=cache)
        self.assertEqual(hit["cache"]["status"], "hit")
        self.assertEqual(hit["scope"], graph["scope"])
        (self.root / "pkg/core.py").write_text("class ExitCode:\n    OK = 1\n", encoding="utf-8")
        warm = self.graph(cache_path=cache)
        cold = self.graph()
        self.assertEqual(warm["scope"], cold["scope"])
        self.assertEqual(warm["scope"]["import_use_fallback_evidence"], graph["scope"]["import_use_fallback_evidence"])

    def test_unused_shadowed_and_unrelated_imports_do_not_add_evidence(self):
        self.write_test(
            "from pkg.core import Config\n"
            "from pkg.other import ExitCode\n"
            "def test_shadow(Config):\n"
            "    return Config()\n"
            "def test_other():\n"
            "    return ExitCode.OK\n"
        )
        graph = self.graph()
        self.assertEqual(graph["scope"]["linked_test_files"], [])
        self.assertEqual(graph["scope"]["import_use_fallback_evidence"], {})

    def test_deleted_provider_and_child_module_matches_are_explained(self):
        (self.root / "pkg/core.py").unlink()
        self.write_test(
            "from pkg import core\n"
            "def test_contract():\n"
            "    return core.dynamic()\n"
        )
        graph = self.graph()
        record = graph["scope"]["import_use_fallback_evidence"]["tests/test_core.py"]["records"][0]
        self.assertEqual(record["import_module"], "pkg")
        self.assertEqual(record["import_symbol"], "core")
        self.assertEqual(record["matched_modules"], ["pkg.core"])
        self.assertEqual(record["expression"], "core.dynamic")

    def test_labels_describe_syntax_not_resolved_builtins_or_constants(self):
        self.write_test(
            "from pkg.core import Config\n"
            "def test_contract():\n"
            "    def cast(kind, value):\n"
            "        return value\n"
            "    cast(Config, None)\n"
            "    assert Config.MAYBE_CONSTANT\n"
        )
        evidence = self.graph()["scope"]["import_use_fallback_evidence"]["tests/test_core.py"]
        kinds = {kind for record in evidence["records"] for kind in record["reference_kinds"]}
        self.assertIn("cast_type_argument_syntax", kinds)
        self.assertIn("uppercase_attribute_syntax", kinds)
        self.assertEqual(evidence["evidence_kind"], "syntactic_import_reference_not_runtime_proof")

    def test_arguments_do_not_inherit_call_target_or_result_attributes(self):
        self.write_test(
            "from pkg.core import Config\n"
            "def test_contract():\n"
            "    return unknown(Config).RESULT()\n"
        )
        record = self.graph()["scope"]["import_use_fallback_evidence"]["tests/test_core.py"]["records"][0]
        self.assertEqual(record["reference_kinds"], ["name_reference"])
        self.assertEqual(record["expression"], "Config")
