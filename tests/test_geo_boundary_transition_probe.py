from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_boundary_transition_probe import analyze_boundary_transitions


class BoundaryTransitionTests(unittest.TestCase):
    def graphs(self, sources, changed="pkg/core.py"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in sources.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            return tuple(build_dependency_graph(
                root, changed_files=[changed], max_hops=0,
                include_import_use_test_fallback=enabled,
            ) for enabled in (False, True))

    def test_static_candidate_has_no_transition_and_inputs_unchanged(self):
        graphs = self.graphs({
            "pkg/core.py": "def run():\n    return 1\n",
            "tests/test_core.py": "from pkg.core import run\ndef test_run():\n    run()\n",
        })
        before = deepcopy(graphs)
        result = analyze_boundary_transitions(*graphs)
        self.assertEqual(result["comparison"]["added_candidates"], 0)
        self.assertEqual(result["candidate_tests"][0]["layer_transition_count"], 0)
        self.assertIsNotNone(result["candidate_tests"][0]["best_known_static_meeting"])
        self.assertEqual(graphs, before)

    def test_deleted_provider_fallback_stays_review_required(self):
        result = analyze_boundary_transitions(*self.graphs({
            "tests/test_core.py": "from pkg.core import run\ndef test_run():\n    run()\n",
        }))
        self.assertEqual(result["comparison"]["added_candidates"], 1)
        row = result["candidate_tests"][0]
        self.assertTrue(row["fallback_added"])
        self.assertEqual(row["layer_transition_count"], 1)
        self.assertEqual(row["evidence_status"], "review_required")
        self.assertIsNone(row["best_known_static_meeting"])
        self.assertEqual(result["graph_edges_added"], 0)
        self.assertEqual(result["actionable_promotions"], 0)

    def test_exact_depth_boundary_is_retained_even_without_selected_candidate(self):
        helpers = "from pkg.core import run\n"
        for index in range(8):
            target = f"helper_{index + 1}()" if index < 7 else "run()"
            helpers += f"def helper_{index}():\n    return {target}\n"
        result = analyze_boundary_transitions(*self.graphs({
            "pkg/core.py": "def run():\n    return 1\n",
            "pkg/helpers.py": helpers,
            "tests/test_chain.py": "from pkg.helpers import helper_0\ndef test_chain():\n    helper_0()\n",
        }))
        self.assertEqual(result["unselected_depth_boundary_tests"], ["tests/test_chain.py"])
        self.assertEqual(result["boundary_entries"][0]["limit"], 8)
        self.assertIn("untraversed_target", result["boundary_entries"][0])

    def test_fallback_does_not_clear_existing_depth_boundary(self):
        helpers = "from pkg.core import run\n"
        for index in range(8):
            target = f"helper_{index + 1}()" if index < 7 else "run()"
            helpers += f"def helper_{index}():\n    return {target}\n"
        result = analyze_boundary_transitions(*self.graphs({
            "pkg/core.py": "def run():\n    return 1\n",
            "pkg/helpers.py": helpers,
            "tests/test_chain.py": (
                "from pkg.helpers import helper_0\nfrom pkg.core import run\n"
                "def test_chain():\n    helper_0()\n    callback = run\n"
            ),
        }))
        row = result["candidate_tests"][0]
        self.assertTrue(row["fallback_added"])
        self.assertTrue(row["boundary_preserved"])
        self.assertEqual(row["evidence_status"], "review_required")
        self.assertEqual(result["comparison"]["added_candidates_with_boundary"], 1)

    def test_rejects_different_extraction_and_scope(self):
        graphs = self.graphs({"pkg/core.py": "VALUE = 1\n"})
        for key, value in (("edges", []), ("scope", {})):
            modified = deepcopy(graphs[1])
            if key == "edges":
                modified[key] = [{"source": "fake", "target": "fake", "type": "calls"}]
            else:
                modified[key]["max_hops"] = 3
            with self.subTest(key=key), self.assertRaises(ValueError):
                analyze_boundary_transitions(graphs[0], modified)

    def test_cycle_does_not_create_a_recommendation_or_artificial_boundary(self):
        result = analyze_boundary_transitions(*self.graphs({
            "pkg/core.py": "VALUE = 1\n",
            "pkg/other.py": "def a():\n    b()\ndef b():\n    a()\n",
            "tests/test_cycle.py": "from pkg.other import a\ndef test_cycle():\n    a()\n",
        }))
        self.assertEqual(result["candidate_tests"], [])
        self.assertEqual(result["boundary_entries"], [])
        self.assertEqual(result["comparison"]["added_candidates"], 0)

    def test_dynamic_boundary_on_static_route_is_never_cleared(self):
        result = analyze_boundary_transitions(*self.graphs({
            "pkg/core.py": "def run(plugin):\n    plugin.dispatch()\n",
            "tests/test_core.py": "from pkg.core import run\ndef test_run():\n    run(None)\n",
        }))
        row = result["candidate_tests"][0]
        self.assertFalse(row["fallback_added"])
        self.assertTrue(row["boundary_encountered"])
        self.assertTrue(row["boundary_preserved"])
        self.assertEqual(row["evidence_status"], "review_required")

    def continuation_graphs(self):
        helpers = "from pkg.core import run\n"
        for index in range(9):
            target = f"helper_{index + 1}()" if index < 8 else "run()"
            helpers += f"def helper_{index}():\n    return {target}\n"
        return self.graphs({
            "pkg/core.py": "def run():\n    return 1\n",
            "pkg/helpers.py": helpers,
            "tests/test_chain.py": "from pkg.helpers import helper_0\ndef test_chain():\n    helper_0()\n",
        })

    def test_extra_hop_budget_counts_the_boundary_crossing(self):
        graphs = self.continuation_graphs()
        before = deepcopy(graphs)
        short = analyze_boundary_transitions(*graphs, extra_call_hops=1)
        self.assertEqual(short["candidate_tests"], [])
        self.assertTrue(short["continuation"]["boundary_searches"][0]["hop_budget_exhausted"])
        reached = analyze_boundary_transitions(*graphs, extra_call_hops=2)
        row = reached["candidate_tests"][0]
        self.assertTrue(row["continuation_added"])
        self.assertFalse(row["fallback_added"])
        self.assertTrue(row["boundary_preserved"])
        self.assertEqual(row["evidence_status"], "review_required")
        witness = reached["continuation"]["boundary_searches"][0]["known_call_witness"]
        self.assertEqual(witness["extra_hops"], 2)
        self.assertEqual(len(witness["path"]), 3)
        self.assertEqual(graphs, before)

    def test_visit_budget_and_invalid_inputs(self):
        graphs = self.continuation_graphs()
        result = analyze_boundary_transitions(*graphs, extra_call_hops=10, max_extra_visits=1)
        self.assertEqual(result["continuation"]["visits"], 1)
        self.assertTrue(result["continuation"]["boundary_searches"][0]["visit_budget_exhausted"])
        self.assertEqual(result["candidate_tests"], [])
        for kwargs in ({"extra_call_hops": -1}, {"extra_call_hops": True},
                       {"max_extra_visits": 0}, {"max_extra_visits": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                analyze_boundary_transitions(*graphs, **kwargs)

    def test_cycle_and_unrelated_exit_cannot_recover_changed_code(self):
        graphs = list(self.continuation_graphs())
        for graph in graphs:
            for edge in graph["edges"]:
                if edge["type"] == "calls" and edge["target"] == "function:pkg/core.py:run":
                    edge["target"] = "function:pkg/helpers.py:helper_7"
        result = analyze_boundary_transitions(*graphs, extra_call_hops=100, max_extra_visits=10)
        self.assertEqual(result["candidate_tests"], [])
        self.assertLessEqual(result["continuation"]["visits"], 10)

    def test_visit_budget_is_shared_by_all_boundaries(self):
        graphs = list(self.continuation_graphs())
        for graph in graphs:
            original = next(
                issue for issue in graph["issues"]
                if issue["kind"] == "test_call_depth_limit_reached"
            )
            graph["issues"].append(deepcopy(original))
        result = analyze_boundary_transitions(*graphs, extra_call_hops=3, max_extra_visits=2)
        searches = result["continuation"]["boundary_searches"]
        self.assertEqual(result["continuation"]["visits"], 2)
        self.assertIsNotNone(searches[0]["known_call_witness"])
        self.assertIsNone(searches[1]["known_call_witness"])
        self.assertTrue(searches[1]["visit_budget_exhausted"])
        self.assertEqual(len(result["boundary_entries"]), 2)

    def test_unknown_dynamic_target_is_not_an_inferred_edge(self):
        graphs = list(self.continuation_graphs())
        for graph in graphs:
            graph["edges"] = [
                edge for edge in graph["edges"]
                if not (edge["type"] == "calls" and edge["target"] == "function:pkg/core.py:run")
            ]
        result = analyze_boundary_transitions(*graphs, extra_call_hops=20)
        self.assertEqual(result["candidate_tests"], [])
        self.assertEqual(result["graph_edges_added"], 0)

    def test_missing_boundary_call_fails_instead_of_imputing_edge(self):
        graphs = list(self.continuation_graphs())
        for graph in graphs:
            next(
                issue for issue in graph["issues"]
                if issue["kind"] == "test_call_depth_limit_reached"
            )["untraversed_target"] = "function:pkg/core.py:run"
        with self.assertRaisesRegex(ValueError, "existing known call edge"):
            analyze_boundary_transitions(*graphs, extra_call_hops=2)


if __name__ == "__main__":
    unittest.main()
