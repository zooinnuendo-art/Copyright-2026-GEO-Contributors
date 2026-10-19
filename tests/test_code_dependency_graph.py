import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.code_dependency_graph import build_dependency_graph


class CodeDependencyGraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "pkg").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "tests" / "evolution_fixture.py").write_text(
            "from pkg.core import helper\n"
            "def isolated_evolution():\n    return None\n"
            "def exercise_production_helper():\n    helper(None)\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "pkg" / "leaf.py").write_text(
            "def target(value):\n    return value\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "types.py").write_text(
            "class Token:\n    pass\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "helpers.py").write_text(
            "from pkg.leaf import target\n"
            "def remote(value):\n    return value\n"
            "def intermediate(value):\n    return target(value)\n"
            "class Worker:\n"
            "    def execute(self, value):\n"
            "        return value\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "core.py").write_text(
            "from pkg.types import Token\n"
            "from pkg.helpers import Worker\n"
            "from pkg.plugins import Plugin\n"
            "import pkg.helpers as helpers\n"
            "VALUE = 0\n"
            "def helper(value: Token) -> Token:\n"
            "    global VALUE\n"
            "    previous = VALUE\n"
            "    VALUE += 1\n"
            "    return Token()\n"
            "class Service:\n"
            "    def __init__(self):\n"
            "        self.worker = Worker()\n"
            "    def run(self, value: Token) -> Token:\n"
            "        self.worker.execute(value)\n"
            "        def inner(item: Token) -> Token:\n"
            "            return helper(item)\n"
            "        return inner(value)\n",
            encoding="utf-8",
        )
        with (self.root / "pkg" / "core.py").open("a", encoding="utf-8") as core_file:
            core_file.write(
                "    def invoke(self, value):\n"
                "        return self.run(value)\n"
                "def module_call(value):\n"
                "    return helpers.remote(value)\n"
                "from pkg.helpers import intermediate\n"
                "def nested_call(value):\n"
                "    return intermediate(value)\n"
                "def uncertain_call():\n"
                "    unknown_local_target()\n"
                "def lazy_import(value):\n"
                "    from pkg.helpers import remote\n"
                "    return remote(value)\n"
                "def accepts_plugin(value: Plugin):\n"
                "    return value\n"
            )
        (self.root / "tests" / "test_core.py").write_text(
            "from pkg.core import helper\n"
            "from pkg.core import nested_call\n"
            "from tests.evolution_fixture import isolated_evolution\n"
            "def test_helper():\n"
            "    isolated_evolution()\n"
            "    helper(None)\n"
            "    nested_call(None)\n"
            "def test_dynamic_attribute():\n"
            "    helper.__call__(None)\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_indirect.py").write_text(
            "from pkg.core import helper\n"
            "class IndirectTests:\n"
            "    def _exercise_target(self):\n"
            "        helper(None)\n"
            "    def test_target_via_helper(self):\n"
            "        self._exercise_target()\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_via_support.py").write_text(
            "from tests.evolution_fixture import exercise_production_helper\n"
            "def test_production_via_support_file():\n"
            "    exercise_production_helper()\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    @staticmethod
    def _without_cache_metadata(graph):
        return {key: value for key, value in graph.items() if key != "cache"}

    def test_extracts_heterogeneous_nodes_and_cross_layer_edges(self):
        graph = build_dependency_graph(self.root, changed_files=["pkg/core.py"])
        node_types = {node["type"] for node in graph["nodes"]}
        edge_types = {edge["type"] for edge in graph["edges"]}

        self.assertTrue({"file", "class", "function", "global_variable", "test"} <= node_types)
        self.assertTrue({"contains", "imports", "calls", "type_depends_on", "reads", "writes", "tests"} <= edge_types, edge_types)
        self.assertIn("tests/test_core.py", graph["scope"]["linked_test_files"])
        self.assertIn("tests/test_indirect.py", graph["scope"]["linked_test_files"])
        self.assertIn("tests/test_via_support.py", graph["scope"]["linked_test_files"])
        self.assertNotIn("tests/evolution_fixture.py", graph["scope"]["linked_test_files"])
        self.assertNotIn("tests/test_core.py", graph["scope"]["distances"])
        self.assertNotIn("unresolved_graph_endpoint", {issue["kind"] for issue in graph["issues"]})
        self.assertEqual(graph["status"], "snapshot_only/review_required")

        calls = {(edge["source"], edge["target"]) for edge in graph["edges"] if edge["type"] == "calls"}
        self.assertIn(
            ("function:pkg/core.py:Service.invoke", "function:pkg/core.py:Service.run"),
            calls,
        )
        self.assertIn(
            ("function:pkg/core.py:module_call", "function:pkg/helpers.py:remote"),
            calls,
        )
        self.assertIn(
            ("function:pkg/core.py:Service.run", "function:pkg/helpers.py:Worker.execute"),
            calls,
        )
        imports = {edge["target"] for edge in graph["edges"] if edge["source"] == "file:pkg/core.py" and edge["type"] == "imports"}
        self.assertIn("file:pkg/helpers.py", imports)
        self.assertIn("pkg/helpers.py", graph["scope"]["distances"])
        attribute_issue = next(issue for issue in graph["issues"] if issue["kind"] == "unresolved_attribute_call_summary")
        self.assertGreaterEqual(attribute_issue["count"], 1)
        self.assertLessEqual(len(attribute_issue["examples"]), 20)
        simple_issue = next(issue for issue in graph["issues"] if issue["kind"] == "unresolved_simple_call_summary")
        self.assertEqual(simple_issue["count"], 2)

    def test_local_function_shadow_does_not_create_false_imported_call_edge(self):
        (self.root / "tests" / "test_shadow.py").write_text(
            "from pkg.core import helper\n"
            "def test_local_helper():\n"
            "    def helper(value):\n"
            "        return value\n"
            "    assert helper(3) == 3\n",
            encoding="utf-8",
        )

        graph = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=0)
        calls = {(edge["source"], edge["target"]) for edge in graph["edges"] if edge["type"] == "calls"}

        self.assertNotIn(("test:tests/test_shadow.py:test_local_helper", "function:pkg/core.py:helper"), calls)
        self.assertNotIn("tests/test_shadow.py", graph["scope"]["linked_test_files"])
        unresolved = next(issue for issue in graph["issues"] if issue["kind"] == "unresolved_simple_call_summary")
        self.assertGreaterEqual(unresolved["count"], 1)

    def test_test_call_traversal_obeys_exact_eight_hop_budget(self):
        def write_chain(root, helper_count):
            (root / "pkg").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pkg" / "core.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            helper_source = "from pkg.core import run\n"
            for index in range(helper_count):
                target = f"helper_{index + 1}()" if index + 1 < helper_count else "run()"
                helper_source += f"def helper_{index}():\n    return {target}\n"
            (root / "pkg" / "helpers.py").write_text(helper_source, encoding="utf-8")
            (root / "tests" / "test_chain.py").write_text(
                "from pkg.helpers import helper_0\n"
                "def test_chain():\n    return helper_0()\n",
                encoding="utf-8",
            )

        eight_hop_root = self.root / "eight_hops"
        nine_hop_root = self.root / "nine_hops"
        write_chain(eight_hop_root, helper_count=7)
        write_chain(nine_hop_root, helper_count=8)
        (nine_hop_root / "pkg" / "unresolved.py").write_text(
            "def uncertain():\n    return unknown_target()\n",
            encoding="utf-8",
        )

        eight_hop = build_dependency_graph(
            eight_hop_root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            include_import_use_test_fallback=False,
        )
        nine_hop = build_dependency_graph(
            nine_hop_root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            include_import_use_test_fallback=False,
        )

        self.assertEqual(eight_hop["scope"]["linked_test_files"], ["tests/test_chain.py"])
        self.assertEqual(nine_hop["scope"]["linked_test_files"], [])
        self.assertEqual(nine_hop["status"], "incomplete/review")
        depth_issue = next(issue for issue in nine_hop["issues"] if issue["kind"] == "test_call_depth_limit_reached")
        self.assertEqual(depth_issue["path"], "tests/test_chain.py")
        self.assertEqual(depth_issue["limit"], 8)
        self.assertIn("unresolved_simple_call_summary", {issue["kind"] for issue in nine_hop["issues"]})
        self.assertEqual(nine_hop["incomplete_causes"]["global"], [])
        self.assertEqual([issue["kind"] for issue in nine_hop["incomplete_causes"]["scope"]], ["test_call_depth_limit_reached"])

    def test_test_call_cycles_and_unrelated_production_helpers_do_not_reach_changed_file(self):
        (self.root / "pkg" / "other.py").write_text(
            "def cycle_a():\n    return cycle_b()\n"
            "def cycle_b():\n    return cycle_a()\n"
            "def unrelated():\n    return 7\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_unrelated_helpers.py").write_text(
            "from pkg.other import cycle_a, unrelated\n"
            "def test_cycle():\n    return cycle_a()\n"
            "def test_unrelated():\n    return unrelated()\n",
            encoding="utf-8",
        )

        graph = build_dependency_graph(
            self.root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            include_import_use_test_fallback=False,
        )

        self.assertNotIn("tests/test_unrelated_helpers.py", graph["scope"]["linked_test_files"])

    def test_test_call_traversal_crosses_production_class_methods(self):
        (self.root / "pkg" / "pipeline.py").write_text(
            "from pkg.core import helper\n"
            "class Runner:\n"
            "    def execute(self):\n"
            "        return helper(None)\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_class_chain.py").write_text(
            "from pkg.pipeline import Runner\n"
            "def test_class_chain():\n    return Runner.execute(None)\n",
            encoding="utf-8",
        )

        graph = build_dependency_graph(
            self.root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            include_import_use_test_fallback=False,
        )

        self.assertIn("tests/test_class_chain.py", graph["scope"]["linked_test_files"])

    def test_scope_is_deterministic_and_limited_to_two_hops(self):
        first = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=2)
        second = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=2)

        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["scope"]["max_hops"], 2)
        with self.assertRaises(ValueError):
            build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=3)

    def test_missing_changed_file_and_file_budget_fail_closed(self):
        missing = build_dependency_graph(self.root, changed_files=["pkg/missing.py"])
        limited = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_files=1)
        node_limited = build_dependency_graph(self.root, max_nodes=1)
        edge_limited = build_dependency_graph(self.root, max_edges=1)

        self.assertEqual(missing["status"], "incomplete/review")
        self.assertIn("changed_file_not_found", {issue["kind"] for issue in missing["issues"]})
        self.assertEqual(limited["status"], "incomplete/review")
        self.assertIn("file_budget_exceeded", {issue["kind"] for issue in limited["issues"]})
        self.assertEqual(len(node_limited["nodes"]), 1)
        self.assertIn("node_budget_exceeded", {issue["kind"] for issue in node_limited["issues"]})
        self.assertEqual(len(edge_limited["edges"]), 1)
        self.assertIn("edge_budget_exceeded", {issue["kind"] for issue in edge_limited["issues"]})

    def test_parse_errors_are_reported_as_incomplete(self):
        (self.root / "pkg" / "broken.py").write_text("def unfinished(:\n", encoding="utf-8")

        graph = build_dependency_graph(self.root)

        self.assertEqual(graph["status"], "incomplete/review")
        self.assertIn("parse_error", {issue["kind"] for issue in graph["issues"]})

    def test_snapshot_cache_hits_exact_manifest_and_invalidates_one_file_change(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 2}

        initial = build_dependency_graph(**arguments, cache_path=cache_path)
        cached = build_dependency_graph(**arguments, cache_path=cache_path)

        self.assertEqual(initial["cache"]["status"], "miss")
        self.assertEqual(cached["cache"]["status"], "hit")
        self.assertEqual(self._without_cache_metadata(initial), self._without_cache_metadata(cached))

        with (self.root / "pkg" / "helpers.py").open("a", encoding="utf-8") as helper_file:
            helper_file.write("\n    def added_method(self, value):\n        return value\n")

        invalidated = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached = build_dependency_graph(**arguments)

        self.assertEqual(invalidated["cache"]["status"], "miss")
        self.assertEqual(invalidated["cache"]["reason"], "snapshot_or_configuration_changed")
        self.assertEqual(invalidated["cache"]["file_artifacts"], {"hits": 8, "misses": 1})
        self.assertEqual(self._without_cache_metadata(invalidated), self._without_cache_metadata(uncached))
        self.assertIn("function:pkg/helpers.py:Worker.added_method", {node["id"] for node in invalidated["nodes"]})

    def test_snapshot_cache_removes_stale_cross_file_call_after_method_deletion(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 2}
        initial = build_dependency_graph(**arguments, cache_path=cache_path)
        expected_call = (
            "function:pkg/core.py:Service.run",
            "function:pkg/helpers.py:Worker.execute",
        )
        initial_calls = {(edge["source"], edge["target"]) for edge in initial["edges"] if edge["type"] == "calls"}
        self.assertIn(expected_call, initial_calls)

        helper_path = self.root / "pkg" / "helpers.py"
        helper_source = helper_path.read_text(encoding="utf-8")
        helper_path.write_text(
            helper_source.replace(
                "class Worker:\n    def execute(self, value):\n        return value\n",
                "class Worker:\n    pass\n",
            ),
            encoding="utf-8",
        )

        incremental = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached = build_dependency_graph(**arguments)
        incremental_calls = {(edge["source"], edge["target"]) for edge in incremental["edges"] if edge["type"] == "calls"}

        self.assertEqual(incremental["cache"]["file_artifacts"], {"hits": 8, "misses": 1})
        self.assertEqual(incremental["cache"]["relation_facts"], {"hits": 5, "misses": 2})
        self.assertNotIn(expected_call, incremental_calls)
        self.assertEqual(self._without_cache_metadata(incremental), self._without_cache_metadata(uncached))

    def test_snapshot_cache_removes_leaf_edge_through_unchanged_intermediate_chain(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 2}
        initial = build_dependency_graph(**arguments, cache_path=cache_path)
        leaf_edge = (
            "function:pkg/helpers.py:intermediate",
            "function:pkg/leaf.py:target",
        )
        self.assertIn(leaf_edge, {
            (edge["source"], edge["target"])
            for edge in initial["edges"]
            if edge["type"] == "calls"
        })
        self.assertIn("tests/test_core.py", initial["scope"]["linked_test_files"])

        leaf_path = self.root / "pkg" / "leaf.py"
        leaf_path.write_text("def replacement(value):\n    return value\n", encoding="utf-8")

        incremental = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached = build_dependency_graph(**arguments)
        calls = {(edge["source"], edge["target"]) for edge in incremental["edges"] if edge["type"] == "calls"}

        self.assertEqual(incremental["cache"]["file_artifacts"], {"hits": 8, "misses": 1})
        self.assertEqual(incremental["cache"]["relation_facts"], {"hits": 5, "misses": 2})
        self.assertEqual(incremental["cache"]["dependency_invalidation"]["changed_modules"], ["pkg.core"])
        self.assertGreaterEqual(len(incremental["cache"]["dependency_invalidation"]["affected_importers"]), 1)
        self.assertNotIn(leaf_edge, calls)
        self.assertIn("unresolved_simple_call_summary", {issue["kind"] for issue in incremental["issues"]})
        self.assertEqual(self._without_cache_metadata(incremental), self._without_cache_metadata(uncached))

    def test_opt_in_relation_recompute_reuses_unaffected_facts_and_matches_cold_graph(self):
        cache_path = self.root.parent / "dependency-graph-selective-relations.json"
        arguments = {"root": self.root, "changed_files": ["pkg/leaf.py"], "max_hops": 0}
        build_dependency_graph(**arguments, cache_path=cache_path)

        leaf_path = self.root / "pkg" / "leaf.py"
        leaf_path.write_text("def replacement(value):\n    return value\n", encoding="utf-8")

        selective = build_dependency_graph(
            **arguments,
            cache_path=cache_path,
            recompute_affected_importers=True,
        )
        cold = build_dependency_graph(**arguments)

        self.assertEqual(selective["cache"]["dependency_invalidation"]["affected_importers"], ["pkg/helpers.py"])
        self.assertEqual(selective["cache"]["relation_facts"], {"hits": 5, "misses": 2})
        self.assertEqual(selective["nodes"], cold["nodes"])
        self.assertEqual(selective["edges"], cold["edges"])
        self.assertEqual(selective["issues"], cold["issues"])
        self.assertEqual(selective["status"], cold["status"])
        self.assertNotEqual(selective["scope"], cold["scope"])

    def test_dependency_invalidation_is_audit_metadata_only(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 2}
        initial = build_dependency_graph(**arguments, cache_path=cache_path)
        self.assertIn("dependency_invalidation", initial["cache"])
        self.assertIsInstance(initial["cache"]["dependency_invalidation"]["changed_modules"], list)
        self.assertIsInstance(initial["cache"]["dependency_invalidation"]["affected_importers"], list)

        leaf_path = self.root / "pkg" / "leaf.py"
        leaf_path.write_text("def replacement(value):\n    return value\n", encoding="utf-8")

        incremental = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached = build_dependency_graph(**arguments)

        self.assertIn("dependency_invalidation", incremental["cache"])
        self.assertGreaterEqual(len(incremental["cache"]["dependency_invalidation"]["affected_importers"]), 1)
        self.assertEqual(self._without_cache_metadata(incremental), self._without_cache_metadata(uncached))

    def test_optional_recompute_affected_importers_expands_scope_when_requested(self):
        broadcast_path = self.root / "pkg" / "broadcast.py"
        broadcast_path.write_text(
            "from pkg.core import helper\n"
            "def deliver():\n"
            "    return helper(None)\n",
            encoding="utf-8",
        )

        default_cache_path = self.root.parent / "dependency-graph-default-cache.json"
        selective_cache_path = self.root.parent / "dependency-graph-selective-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 0}
        default_graph = build_dependency_graph(**arguments, cache_path=default_cache_path)
        selective_graph = build_dependency_graph(**arguments, cache_path=selective_cache_path, recompute_affected_importers=True)
        uncached_selective_graph = build_dependency_graph(**arguments, recompute_affected_importers=True)

        self.assertIn("pkg/core.py", default_graph["scope"]["files"])
        self.assertIn("tests/test_core.py", default_graph["scope"]["files"])
        self.assertNotIn("pkg/broadcast.py", default_graph["scope"]["files"])
        self.assertEqual(default_graph["cache"]["status"], "miss")
        self.assertIn(selective_graph["cache"]["status"], {"miss", "hit"})
        self.assertIn(selective_graph["cache"]["reason"], {"cache_not_found", "snapshot_or_configuration_changed"})
        self.assertIn("pkg/broadcast.py", selective_graph["scope"]["files"])
        self.assertEqual(selective_graph["scope"]["files"], uncached_selective_graph["scope"]["files"])
        self.assertEqual(self._without_cache_metadata(selective_graph), self._without_cache_metadata(uncached_selective_graph))
        self.assertIn("dependency_invalidation", selective_graph["cache"])
        self.assertIn("pkg/broadcast.py", selective_graph["cache"]["dependency_invalidation"]["affected_importers"])
        self.assertGreater(len(selective_graph["scope"]["files"]), len(default_graph["scope"]["files"]))

    def test_selection_seed_matches_cold_build_for_same_subset(self):
        broadcast_path = self.root / "pkg" / "broadcast.py"
        broadcast_path.write_text(
            "from pkg.core import helper\n"
            "def deliver():\n"
            "    return helper(None)\n",
            encoding="utf-8",
        )

        selective = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=0, recompute_affected_importers=True)
        selection_seed = selective["scope"]["selection_seed"]
        cold = build_dependency_graph(self.root, changed_files=selection_seed, max_hops=0)

        self.assertEqual(selective["scope"]["selection_seed"], selection_seed)
        self.assertEqual(selective["scope"]["files"], cold["scope"]["files"])
        self.assertEqual(selective["nodes"], cold["nodes"])
        self.assertEqual(selective["edges"], cold["edges"])
        self.assertNotEqual(selective["cache"]["dependency_invalidation"], cold["cache"]["dependency_invalidation"])

    def test_snapshot_cache_re_resolves_multifile_module_rename(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 2}
        initial = build_dependency_graph(**arguments, cache_path=cache_path)
        expected_call = (
            "function:pkg/core.py:module_call",
            "function:pkg/helpers.py:remote",
        )
        initial_calls = {(edge["source"], edge["target"]) for edge in initial["edges"] if edge["type"] == "calls"}
        self.assertIn(expected_call, initial_calls)

        old_module = self.root / "pkg" / "helpers.py"
        new_module = self.root / "pkg" / "workers.py"
        old_module.rename(new_module)
        for relative_path in ("pkg/core.py", "tests/evolution_fixture.py"):
            consumer = self.root / relative_path
            consumer.write_text(
                consumer.read_text(encoding="utf-8").replace("pkg.helpers", "pkg.workers"),
                encoding="utf-8",
            )

        renamed = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached = build_dependency_graph(**arguments)
        renamed_calls = {(edge["source"], edge["target"]) for edge in renamed["edges"] if edge["type"] == "calls"}

        self.assertEqual(renamed["cache"]["file_artifacts"], {"hits": 7, "misses": 2})
        self.assertEqual(renamed["cache"]["relation_facts"], {"hits": 5, "misses": 2})
        self.assertEqual(renamed["cache"]["relation_facts"], {"hits": 5, "misses": 2})
        self.assertEqual(renamed["cache"]["dependency_invalidation"]["changed_modules"], ["pkg.core"])
        self.assertEqual(renamed["cache"]["dependency_invalidation"]["affected_importers"], [
            "tests/evolution_fixture.py",
            "tests/test_core.py",
            "tests/test_indirect.py",
        ])
        self.assertIn(("function:pkg/core.py:module_call", "function:pkg/workers.py:remote"), renamed_calls)
        self.assertNotIn(expected_call, renamed_calls)
        self.assertIn("pkg/workers.py", renamed["scope"]["distances"])
        self.assertNotIn(("pkg/core.py", "pkg.helpers"), {
            (issue.get("path"), issue.get("target"))
            for issue in renamed["issues"]
            if issue["kind"] == "unresolved_local_import"
        })
        self.assertEqual(self._without_cache_metadata(renamed), self._without_cache_metadata(uncached))

    def test_snapshot_cache_key_includes_scope_configuration_and_recovers_from_corruption(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        base = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=0, cache_path=cache_path)
        changed_scope = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=1, cache_path=cache_path)
        selective = build_dependency_graph(
            self.root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            cache_path=cache_path,
            recompute_affected_importers=True,
        )
        selective_hit = build_dependency_graph(
            self.root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            cache_path=cache_path,
            recompute_affected_importers=True,
        )
        fallback_disabled = build_dependency_graph(
            self.root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            cache_path=cache_path,
            recompute_affected_importers=True,
            include_import_use_test_fallback=False,
        )
        fallback_disabled_hit = build_dependency_graph(
            self.root,
            changed_files=["pkg/core.py"],
            max_hops=0,
            cache_path=cache_path,
            recompute_affected_importers=True,
            include_import_use_test_fallback=False,
        )

        self.assertEqual(base["cache"]["status"], "miss")
        self.assertEqual(changed_scope["cache"]["status"], "miss")
        self.assertEqual(changed_scope["cache"]["reason"], "snapshot_or_configuration_changed")
        self.assertEqual(selective["cache"]["status"], "miss")
        self.assertEqual(selective["cache"]["reason"], "snapshot_or_configuration_changed")
        self.assertEqual(selective_hit["cache"]["status"], "hit")
        self.assertEqual(selective["scope"], selective_hit["scope"])
        self.assertEqual(fallback_disabled["cache"]["status"], "miss")
        self.assertEqual(fallback_disabled["cache"]["reason"], "snapshot_or_configuration_changed")
        self.assertEqual(fallback_disabled_hit["cache"]["status"], "hit")
        self.assertEqual(fallback_disabled["scope"], fallback_disabled_hit["scope"])

        cache_path.write_text("{invalid json", encoding="utf-8")
        recovered = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=1, cache_path=cache_path)
        uncached = build_dependency_graph(self.root, changed_files=["pkg/core.py"], max_hops=1)

        self.assertEqual(recovered["cache"]["status"], "miss")
        self.assertEqual(recovered["cache"]["reason"], "cache_unreadable_or_invalid")
        self.assertEqual(self._without_cache_metadata(recovered), self._without_cache_metadata(uncached))

    def test_snapshot_cache_invalidates_on_python_file_addition_and_removal(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 2}
        initial = build_dependency_graph(**arguments, cache_path=cache_path)
        self.assertEqual(initial["cache"]["status"], "miss")

        added_path = self.root / "pkg" / "plugins.py"
        added_path.write_text("class Plugin:\n    pass\n", encoding="utf-8")
        after_addition = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached_after_addition = build_dependency_graph(**arguments)
        self.assertEqual(after_addition["cache"]["status"], "miss")
        self.assertEqual(after_addition["cache"]["file_artifacts"], {"hits": 9, "misses": 1})
        self.assertEqual(self._without_cache_metadata(after_addition), self._without_cache_metadata(uncached_after_addition))
        self.assertIn(("function:pkg/core.py:accepts_plugin", "class:pkg/plugins.py:Plugin"), {
            (edge["source"], edge["target"])
            for edge in after_addition["edges"]
            if edge["type"] == "type_depends_on"
        })
        self.assertNotIn("unresolved_local_import", {issue["kind"] for issue in after_addition["issues"]})

        added_path.unlink()
        after_removal = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached_after_removal = build_dependency_graph(**arguments)
        self.assertEqual(after_removal["cache"]["status"], "miss")
        self.assertEqual(after_removal["cache"]["file_artifacts"], {"hits": 9, "misses": 0})
        self.assertEqual(self._without_cache_metadata(after_removal), self._without_cache_metadata(uncached_after_removal))
        self.assertIn("unresolved_local_import", {issue["kind"] for issue in after_removal["issues"]})

    def test_snapshot_cache_fails_closed_when_file_changes_during_hashing(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        build_dependency_graph(self.root, changed_files=["pkg/core.py"], cache_path=cache_path)
        target = (self.root / "pkg" / "helpers.py").resolve()
        original_read_bytes = Path.read_bytes
        mutated = False

        def read_bytes_with_mutation(path):
            nonlocal mutated
            content = original_read_bytes(path)
            if path == target and not mutated:
                mutated = True
                with path.open("ab") as stream:
                    stream.write(b"\n# concurrent edit\n")
            return content

        with mock.patch.object(Path, "read_bytes", read_bytes_with_mutation):
            graph = build_dependency_graph(self.root, changed_files=["pkg/core.py"], cache_path=cache_path)

        self.assertTrue(mutated)
        self.assertEqual(graph["status"], "incomplete/review")
        self.assertEqual(graph["cache"]["status"], "bypassed")
        self.assertEqual(graph["cache"]["reason"], "snapshot_changed_during_read")
        self.assertIn("snapshot_changed_during_read", {issue["kind"] for issue in graph["issues"]})

    def test_corrupt_file_artifact_is_reparsed_without_reusing_its_facts(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "max_hops": 2}
        initial = build_dependency_graph(**arguments, cache_path=cache_path)
        self.assertEqual(initial["cache"]["status"], "miss")

        envelope = json.loads(cache_path.read_text(encoding="utf-8"))
        artifact_entry = envelope["file_artifacts"]["pkg/types.py"]
        artifact = json.loads(artifact_entry["artifact_json"])
        artifact["module_globals"].append("corrupt_fact")
        artifact_entry["artifact_json"] = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
        relation_entry = envelope["file_artifacts"]["pkg/core.py"]["resolved_relations"]
        relation_entry["edges"].append({
            "source": "function:pkg/core.py:helper",
            "target": "class:pkg/types.py:Token",
            "type": "calls",
            "line": 1,
            "confidence": 1.0,
        })
        cache_path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
        helper_path = self.root / "pkg" / "helpers.py"
        with helper_path.open("a", encoding="utf-8") as helper_file:
            helper_file.write("\n# source mutation\n")

        recovered = build_dependency_graph(**arguments, cache_path=cache_path)
        uncached = build_dependency_graph(**arguments)

        self.assertEqual(recovered["cache"]["status"], "miss")
        self.assertEqual(recovered["cache"]["file_artifacts"], {"hits": 7, "misses": 2})
        self.assertNotIn(("function:pkg/core.py:helper", "class:pkg/types.py:Token", 1), {
            (edge["source"], edge["target"], edge["line"])
            for edge in recovered["edges"]
            if edge["type"] == "calls"
        })
        self.assertGreaterEqual(recovered["cache"]["relation_facts"]["misses"], 1)
        self.assertEqual(self._without_cache_metadata(recovered), self._without_cache_metadata(uncached))

    def test_snapshot_cache_revalidates_manifest_before_returning_a_hit(self):
        cache_path = self.root.parent / "dependency-graph-cache.json"
        arguments = {"root": self.root, "changed_files": ["pkg/core.py"], "cache_path": cache_path}
        build_dependency_graph(**arguments)
        build_dependency_graph(**arguments)
        target = (self.root / "pkg" / "helpers.py").resolve()
        original_read_text = Path.read_text
        mutated = False

        def read_cache_then_mutate(path, *args, **kwargs):
            nonlocal mutated
            result = original_read_text(path, *args, **kwargs)
            if path == cache_path and not mutated:
                mutated = True
                with target.open("a", encoding="utf-8") as stream:
                    stream.write("\n# edit before cache return\n")
            return result

        with mock.patch.object(Path, "read_text", read_cache_then_mutate):
            graph = build_dependency_graph(**arguments)

        self.assertTrue(mutated)
        self.assertEqual(graph["status"], "incomplete/review")
        self.assertEqual(graph["cache"]["status"], "bypassed")
        self.assertEqual(graph["cache"]["reason"], "snapshot_changed_before_cache_return")
        self.assertIn("snapshot_changed_before_cache_return", {issue["kind"] for issue in graph["issues"]})


if __name__ == "__main__":
    unittest.main()