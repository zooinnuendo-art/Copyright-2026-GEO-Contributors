from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_boundary_transition_probe import analyze_boundary_transitions
from tools.temporal_ast_delta import build_python_ast_delta


class ChangedSymbolAlignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        before = b"VALUE = 1\ndef run():\n    return VALUE\nclass Config:\n    pass\n"
        after = b"VALUE = 2\ndef run():\n    return VALUE + 1\nclass Config:\n    pass\n"
        self.delta = build_python_ast_delta({"pkg/core.py": before}, {"pkg/core.py": after})
        sources = {
            "pkg/core.py": after.decode(),
            "tests/test_direct.py": "from pkg.core import run\ndef test_direct():\n    run()\n",
            "tests/test_file.py": "from pkg.core import Config\ndef test_file():\n    assert Config.UNKNOWN\n",
            "tests/test_type.py": "from pkg.core import Config\ndef test_type(value: Config):\n    pass\n",
        }
        for name, text in sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.graphs = [
            build_dependency_graph(
                self.root, changed_files=["pkg/core.py"], max_hops=0,
                include_import_use_test_fallback=enabled,
            ) for enabled in (False, True)
        ]

    def report(self, delta=None):
        return analyze_boundary_transitions(*self.graphs, ast_delta=self.delta if delta is None else delta)

    def test_classification_and_exact_identity_preserve_prior_output(self):
        original = analyze_boundary_transitions(*self.graphs)
        before = deepcopy((self.graphs, self.delta))
        report = self.report()
        rows = {row["test_file"]: row["changed_symbol_alignment"] for row in report["candidate_tests"]}
        self.assertEqual(rows["tests/test_direct.py"]["classification"], "direct_symbol_reference")
        self.assertEqual(rows["tests/test_direct.py"]["direct_matches"], [
            {"symbol_id": "function:pkg/core.py:run", "relation": "calls"},
        ])
        self.assertEqual(rows["tests/test_file.py"]["classification"], "file_level_relation")
        self.assertEqual(rows["tests/test_type.py"]["classification"], "undetermined")
        for row in report["candidate_tests"]:
            self.assertFalse(row.pop("changed_symbol_alignment")["runtime_impact_proven"])
        self.assertEqual(report, original)
        self.assertEqual((self.graphs, self.delta), before)

    def test_uncertain_empty_and_unrelated_delta_are_not_negative_evidence(self):
        for delta in (
            {"symbols": {"added": [], "changed": [], "removed": []}, "uncertain_files": [], "issues": []},
            {"symbols": {"added": [], "changed": [], "removed": []}, "uncertain_files": ["pkg/core.py"], "issues": []},
            {"symbols": {"added": [{"symbol_id": "function:other.py:run"}], "changed": [], "removed": []}, "uncertain_files": [], "issues": []},
        ):
            with self.subTest(delta=delta):
                self.assertTrue(all(
                    row["changed_symbol_alignment"]["classification"] == "undetermined"
                    for row in self.report(delta)["candidate_tests"]
                ))

    def test_truncation_does_not_imply_file_only_evidence(self):
        self.graphs[1]["scope"]["import_use_fallback_evidence"]["tests/test_file.py"]["truncated"] = True
        row = next(row for row in self.report()["candidate_tests"] if row["test_file"] == "tests/test_file.py")
        self.assertEqual(row["changed_symbol_alignment"]["classification"], "undetermined")

    def test_unrelated_parse_issues_do_not_poison_changed_files(self):
        delta = deepcopy(self.delta)
        delta["issues"].append({"kind": "ast_parse_error", "path": "unrelated.py"})
        delta["uncertain_files"].append("unrelated.py")
        row = next(row for row in self.report(delta)["candidate_tests"] if row["test_file"] == "tests/test_file.py")
        self.assertEqual(row["changed_symbol_alignment"]["classification"], "file_level_relation")
        self.assertFalse(row["changed_symbol_alignment"]["delta_uncertain"])

    def test_invalid_delta_fails_explicitly(self):
        for delta in ({}, {"symbols": {}}, {"symbols": {"added": [{}], "changed": [], "removed": []}}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self.report(delta)

    def test_partial_duplicates_preserve_unique_match_and_uncertainty_audit(self):
        delta = deepcopy(self.delta)
        delta["uncertain_files"] = ["pkg/core.py"]
        delta["partially_uncertain_files"] = ["pkg/core.py"]
        delta["uncertain_symbols"] = ["function:pkg/core.py:ambiguous"]
        delta["issues"] = [{
            "kind": "duplicate_symbol_id", "path": "pkg/core.py",
            "symbol_id": "function:pkg/core.py:ambiguous",
        }]
        rows = {row["test_file"]: row["changed_symbol_alignment"] for row in self.report(delta)["candidate_tests"]}
        self.assertEqual(rows["tests/test_direct.py"]["classification"], "direct_symbol_reference")
        self.assertTrue(rows["tests/test_direct.py"]["delta_partially_uncertain"])
        self.assertFalse(rows["tests/test_direct.py"]["delta_uncertain"])
        self.assertEqual(rows["tests/test_file.py"]["classification"], "file_level_relation")

    def test_ambiguous_changed_ids_cannot_produce_direct_matches(self):
        delta = deepcopy(self.delta)
        delta["uncertain_symbols"] = ["function:pkg/core.py:run"]
        row = next(row for row in self.report(delta)["candidate_tests"] if row["test_file"] == "tests/test_direct.py")
        self.assertEqual(row["changed_symbol_alignment"]["direct_matches"], [])
        self.assertNotEqual(row["changed_symbol_alignment"]["classification"], "direct_symbol_reference")

    def test_direct_call_detail_is_additive_and_not_runtime_proof(self):
        row = next(row for row in self.report()["candidate_tests"] if row["test_file"] == "tests/test_direct.py")
        alignment = row["changed_symbol_alignment"]
        self.assertEqual(alignment["direct_reference_kinds"], ["changed_function_call"])
        self.assertEqual(alignment["direct_match_details"], [{
            "symbol_id": "function:pkg/core.py:run",
            "relation": "calls",
            "reference_kind": "changed_function_call",
            "changed_descendant_symbol_ids": [],
            "container_ast_includes_changed_descendants": False,
            "reference_to_changed_descendant_proven": False,
        }])

    def test_class_reference_distinguishes_changed_nested_method(self):
        before = b"class Config:\n    def run(self):\n        return 1\n"
        after = b"class Config:\n    def run(self):\n        return 2\n"
        delta = build_python_ast_delta({"pkg/core.py": before}, {"pkg/core.py": after})
        # Existing test_type has a static type edge to Config, not Config.run.
        row = next(row for row in self.report(delta)["candidate_tests"] if row["test_file"] == "tests/test_type.py")
        alignment = row["changed_symbol_alignment"]
        self.assertEqual(alignment["classification"], "direct_symbol_reference")
        self.assertEqual(alignment["direct_reference_kinds"], ["changed_class_type_reference"])
        detail = alignment["direct_match_details"][0]
        self.assertEqual(detail["changed_descendant_symbol_ids"], ["function:pkg/core.py:Config.run"])
        self.assertTrue(detail["container_ast_includes_changed_descendants"])
        self.assertFalse(detail["reference_to_changed_descendant_proven"])

    def test_relation_details_cover_reads_writes_constructors_and_other(self):
        # Exercise supported edge shapes independently of the conservative extractor.
        target = "global:pkg/core.py:VALUE"
        for graph in self.graphs:
            graph["edges"].extend([
                {"source": "test:tests/test_direct.py:test_direct", "target": target, "type": relation}
                for relation in ("reads", "writes")
            ])
            graph["edges"].append({
                "source": "test:tests/test_direct.py:test_direct",
                "target": "class:pkg/core.py:Config", "type": "calls",
            })
        delta = deepcopy(self.delta)
        delta["symbols"]["changed"].append({"symbol_id": "class:pkg/core.py:Config"})
        row = next(row for row in self.report(delta)["candidate_tests"] if row["test_file"] == "tests/test_direct.py")
        self.assertEqual(row["changed_symbol_alignment"]["direct_reference_kinds"], [
            "changed_class_call", "changed_function_call", "changed_variable_read", "changed_variable_write",
        ])

    def test_descendant_names_require_same_path_and_dot_boundary(self):
        delta = deepcopy(self.delta)
        delta["symbols"]["changed"].extend([
            {"symbol_id": "function:other.py:run.child"},
            {"symbol_id": "function:pkg/core.py:runner.child"},
        ])
        row = next(row for row in self.report(delta)["candidate_tests"] if row["test_file"] == "tests/test_direct.py")
        detail = row["changed_symbol_alignment"]["direct_match_details"][0]
        self.assertEqual(detail["changed_descendant_symbol_ids"], [])
