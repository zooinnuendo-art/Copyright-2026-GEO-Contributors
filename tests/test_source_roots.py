import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_boundary_transition_probe import analyze_boundary_transitions


class SourceRootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("src/pkg/__init__.py", "from .core import run\n")
        self.write("src/pkg/core.py", "from .leaf import target\ndef run():\n    return target()\n")
        self.write("src/pkg/leaf.py", "def target():\n    return 1\n")
        self.write("tests/test_core.py", "from pkg.core import run\ndef test_run():\n    return run()\n")

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def graph(self, **kwargs):
        return build_dependency_graph(
            self.root, changed_files=["src/pkg/leaf.py"], max_hops=0, **kwargs,
        )

    def test_static_calls_relative_imports_and_paths(self):
        default = self.graph(include_import_use_test_fallback=False)
        self.assertEqual(default["scope"]["linked_test_files"], [])
        graph = self.graph(source_roots=["src"], include_import_use_test_fallback=False)
        self.assertEqual(graph["scope"]["linked_test_files"], ["tests/test_core.py"])
        self.assertEqual(graph["source_roots"], ["src"])
        self.assertIn(
            ("function:src/pkg/core.py:run", "function:src/pkg/leaf.py:target"),
            {(edge["source"], edge["target"]) for edge in graph["edges"] if edge["type"] == "calls"},
        )
        self.assertNotIn("source_roots", default)
        self.assertEqual(default, self.graph(source_roots=[], include_import_use_test_fallback=False))

    def test_used_import_fallback_for_deleted_provider(self):
        (self.root / "src/pkg/leaf.py").unlink()
        self.write("tests/test_leaf.py", "import pkg.leaf as leaf\ndef test_leaf():\n    return leaf.dynamic()\n")
        static = self.graph(source_roots=["src"], include_import_use_test_fallback=False)
        fallback = self.graph(source_roots=["src"])
        self.assertNotIn("tests/test_leaf.py", static["scope"]["linked_test_files"])
        self.assertIn("tests/test_leaf.py", fallback["scope"]["linked_test_files"])
        comparison = analyze_boundary_transitions(static, fallback)
        self.assertGreater(comparison["comparison"]["added_candidates"], 0)

    def test_cache_configuration_change_and_incremental_invalidation(self):
        cache = self.root / "graph.json"
        self.graph(cache_path=cache)
        configured = self.graph(source_roots=["src"], cache_path=cache)
        self.assertEqual(configured["cache"]["status"], "miss")
        self.assertEqual(configured["cache"]["file_artifacts"]["hits"], 0)
        cached = self.graph(source_roots=["src"], cache_path=cache)
        self.assertEqual(cached["cache"]["status"], "hit")
        self.write("src/pkg/leaf.py", "def target():\n    return 2\n")
        incremental = self.graph(source_roots=["src"], cache_path=cache)
        cold = self.graph(source_roots=["src"])
        self.assertEqual(incremental["cache"]["dependency_invalidation"]["changed_modules"], ["pkg.leaf"])
        self.assertIn("src/pkg/core.py", incremental["cache"]["dependency_invalidation"]["affected_importers"])
        self.assertEqual(
            {k: v for k, v in incremental.items() if k != "cache"},
            {k: v for k, v in cold.items() if k != "cache"},
        )
        reverted = self.graph(cache_path=cache)
        self.assertEqual(reverted["cache"]["file_artifacts"]["hits"], 0)
        self.assertEqual(
            {k: v for k, v in reverted.items() if k != "cache"},
            {k: v for k, v in self.graph().items() if k != "cache"},
        )

    def test_multiple_and_nested_roots_are_deterministic(self):
        self.write("lib/other.py", "def call():\n    return 1\n")
        self.write("tests/test_other.py", "from other import call\ndef test_other():\n    return call()\n")
        roots = ["src", "src/pkg", "lib", "src"]
        first = self.graph(source_roots=roots)
        second = self.graph(source_roots=list(reversed(roots)))
        self.assertEqual(first, second)
        self.assertEqual(first["source_roots"], ["lib", "src", "src/pkg"])
        # The nested root makes core and leaf top-level modules.
        self.assertEqual(first["scope"]["linked_test_files"], [])
        other = build_dependency_graph(
            self.root, changed_files=["lib/other.py"], max_hops=0,
            source_roots=roots, include_import_use_test_fallback=False,
        )
        self.assertEqual(other["scope"]["linked_test_files"], ["tests/test_other.py"])

    def test_package_and_dotted_module_imports(self):
        self.write(
            "tests/test_import_forms.py",
            "from pkg import core\n"
            "import pkg.core\n"
            "from pkg import run\n"
            "def test_forms():\n"
            "    core.run()\n"
            "    pkg.core.run()\n"
            "    run()\n",
        )
        graph = self.graph(source_roots=["src"], include_import_use_test_fallback=False)
        self.assertIn("tests/test_import_forms.py", graph["scope"]["linked_test_files"])
        calls = {
            edge["target"] for edge in graph["edges"]
            if edge["type"] == "calls" and edge["source"] == "test:tests/test_import_forms.py:test_forms"
        }
        self.assertIn("function:src/pkg/core.py:run", calls)

    def test_boundary_continuation_retains_original_evidence(self):
        self.write(
            "src/pkg/chain.py",
            "from .leaf import target\n"
            + "".join(f"def step_{i}():\n    return step_{i + 1}()\n" for i in range(8))
            + "def step_8():\n    return target()\n",
        )
        self.write("tests/test_chain.py", "from pkg.chain import step_0\ndef test_chain():\n    return step_0()\n")
        static = self.graph(source_roots=["src"], include_import_use_test_fallback=False)
        fallback = self.graph(source_roots=["src"])
        baseline = analyze_boundary_transitions(static, fallback)
        extended = analyze_boundary_transitions(static, fallback, extra_call_hops=2)
        self.assertEqual(extended["boundary_entries"], baseline["boundary_entries"])
        self.assertGreater(len(extended["boundary_entries"]), 0)
        self.assertEqual(extended["comparison"]["continuation_added_candidates"], 1)
        self.assertEqual(extended["continuation"]["visits"], 2)
        candidate = next(row for row in extended["candidate_tests"] if row["test_file"] == "tests/test_chain.py")
        self.assertTrue(candidate["continuation_added"])

    def test_invalid_and_colliding_roots_are_rejected(self):
        for root in ("missing", "../", str(self.root), "src/pkg/core.py"):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "source root"):
                self.graph(source_roots=[root])
        self.write("pkg/core.py", "def run():\n    pass\n")
        with self.assertRaisesRegex(ValueError, "ambiguous local module pkg.core"):
            self.graph(source_roots=["src"])

    def test_root_dot_preserves_module_names(self):
        graph = self.graph(source_roots=["."])
        plain = self.graph()
        graph.pop("source_roots")
        self.assertEqual(graph, plain)

    def test_clis_and_invalid_input(self):
        for module in ("tools.code_dependency_graph", "tools.geo_boundary_transition_probe"):
            command = [
                sys.executable, "-m", module, str(self.root),
                "--source-root", "src", "--changed", "src/pkg/leaf.py", "--max-hops", "0",
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            report = json.loads(result.stdout)
            if module.endswith("code_dependency_graph"):
                self.assertEqual(report["scope"]["linked_test_files"], ["tests/test_core.py"])
            else:
                self.assertEqual(report["comparison"]["static_candidates"], 1)
            command[command.index("src")] = "missing"
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stdout, "")
            self.assertIn("source root", failed.stderr)
