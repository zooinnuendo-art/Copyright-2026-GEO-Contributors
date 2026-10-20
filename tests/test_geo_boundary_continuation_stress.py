from copy import deepcopy
import unittest

from tools.geo_boundary_transition_probe import analyze_boundary_transitions


def graph_for_calls(calls, targets):
    ids = {node for edge in calls for node in edge} | {"goal"}
    nodes = [
        {"id": node, "type": "function", "path": "goal.py" if node == "goal" else "helpers.py"}
        for node in sorted(ids)
    ]
    nodes.append({"id": "file:goal.py", "type": "file", "path": "goal.py"})
    nodes.extend(
        {"id": f"file:tests/test_{index}.py", "type": "file", "path": f"tests/test_{index}.py"}
        for index in range(len(targets))
    )
    return {
        "nodes": nodes,
        "edges": [{"source": left, "target": right, "type": "calls"} for left, right in calls],
        "issues": [
            {"kind": "test_call_depth_limit_reached", "path": f"tests/test_{index}.py",
             "terminal_node": terminal, "untraversed_target": target, "limit": 8}
            for index, (terminal, target) in enumerate(targets)
        ],
        "scope": {
            "changed_files": ["goal.py"], "distances": {"goal.py": 0},
            "max_hops": 0, "linked_test_files": [],
        },
    }


class BoundaryContinuationStressTests(unittest.TestCase):
    def analyze(self, graph, hops=100, visits=1000):
        before = deepcopy(graph)
        result = analyze_boundary_transitions(
            graph, graph, extra_call_hops=hops, max_extra_visits=visits,
        )
        self.assertEqual(graph, before)
        continuation = result["continuation"]
        self.assertLessEqual(continuation["visits"], visits)
        self.assertLessEqual(continuation["peak_queue_size"], visits)
        self.assertEqual(
            continuation["visits"],
            sum(search["visits"] for search in continuation["boundary_searches"]),
        )
        self.assertEqual(result["graph_edges_added"], 0)
        self.assertEqual(result["actionable_promotions"], 0)
        for search in continuation["boundary_searches"]:
            witness = search["known_call_witness"]
            if witness:
                known = {(edge["source"], edge["target"]) for edge in graph["edges"]}
                self.assertTrue(all(edge in known for edge in zip(witness["path"], witness["path"][1:])))
                self.assertLessEqual(witness["extra_hops"], hops)
        return result

    def test_wide_fanout_caps_queue_and_reports_pruned_goal(self):
        calls = [("terminal", "branch")]
        calls += [("branch", f"leaf_{index:05d}") for index in range(5000)]
        calls += [("branch", "zz_goal_route"), ("zz_goal_route", "goal")]
        graph = graph_for_calls(calls, [("terminal", "branch")])
        result = self.analyze(graph, visits=10)
        continuation = result["continuation"]
        search = continuation["boundary_searches"][0]
        self.assertEqual(continuation["visits"], 10)
        self.assertEqual(continuation["peak_queue_size"], 9)
        self.assertEqual(search["search_status"], "visit_budget_exhausted")
        self.assertTrue(search["visit_budget_exhausted"])
        self.assertIsNone(search["known_call_witness"])

    def test_deep_unrelated_chain_exhaustion_is_not_runtime_independence(self):
        calls = [("terminal", "node_0")] + [
            (f"node_{index}", f"node_{index + 1}") for index in range(50)
        ]
        graph = graph_for_calls(calls, [("terminal", "node_0")])
        exhausted = self.analyze(graph, hops=100)
        search = exhausted["continuation"]["boundary_searches"][0]
        self.assertEqual(search["search_status"], "known_calls_exhausted")
        self.assertEqual(search["visits"], 51)
        self.assertFalse(search["hop_budget_exhausted"])
        self.assertFalse(search["visit_budget_exhausted"])
        limited = self.analyze(graph, hops=5)
        self.assertEqual(
            limited["continuation"]["boundary_searches"][0]["search_status"],
            "hop_budget_exhausted",
        )
        self.assertEqual(limited["continuation"]["visits"], 5)

    def test_cycle_and_diamond_terminate_with_bounded_duplicate_attempts(self):
        calls = [
            ("terminal", "a"), ("a", "b"), ("a", "c"),
            ("b", "d"), ("c", "d"), ("d", "a"),
        ]
        result = self.analyze(graph_for_calls(calls, [("terminal", "a")]))
        search = result["continuation"]["boundary_searches"][0]
        self.assertEqual(search["search_status"], "known_calls_exhausted")
        self.assertEqual(search["visits"], 5)
        self.assertIsNone(search["known_call_witness"])

    def test_tight_shared_budget_depends_on_boundary_order_and_reports_unsearched(self):
        graph = graph_for_calls(
            [("terminal_a", "dead"), ("terminal_b", "goal")],
            [("terminal_a", "dead"), ("terminal_b", "goal")],
        )
        first = self.analyze(graph, visits=1)
        searches = first["continuation"]["boundary_searches"]
        self.assertIsNone(searches[0]["known_call_witness"])
        self.assertEqual(searches[1]["visits"], 0)
        self.assertEqual(searches[1]["peak_queue_size"], 0)
        self.assertEqual(searches[1]["search_status"], "visit_budget_exhausted")
        reversed_graph = deepcopy(graph)
        reversed_graph["issues"].reverse()
        second = self.analyze(reversed_graph, visits=1)
        self.assertIsNotNone(second["continuation"]["boundary_searches"][0]["known_call_witness"])
        for candidate in second["candidate_tests"]:
            self.assertEqual(candidate["evidence_status"], "review_required")
            self.assertTrue(candidate["boundary_preserved"])
        for ordered in (graph, reversed_graph):
            full = self.analyze(ordered, visits=2)
            self.assertEqual(
                {search["test_file"] for search in full["continuation"]["boundary_searches"]
                 if search["known_call_witness"]}, {"tests/test_1.py"},
            )

    def test_witness_found_does_not_hide_truncated_other_branches(self):
        graph = graph_for_calls(
            [("terminal", "branch"), ("branch", "goal"), ("branch", "z_other")],
            [("terminal", "branch")],
        )
        search = self.analyze(graph, visits=2)["continuation"]["boundary_searches"][0]
        self.assertEqual(search["search_status"], "known_call_witness_found")
        self.assertTrue(search["visit_budget_exhausted"])
        self.assertIsNotNone(search["known_call_witness"])

    def test_disabled_continuation_does_not_visit_even_with_large_graph(self):
        graph = graph_for_calls(
            [("terminal", "goal")], [("terminal", "goal")],
        )
        result = self.analyze(graph, hops=0, visits=1)
        self.assertFalse(result["continuation"]["enabled"])
        self.assertEqual(result["continuation"]["visits"], 0)
        self.assertEqual(result["continuation"]["peak_queue_size"], 0)
        self.assertEqual(result["candidate_tests"], [])


if __name__ == "__main__":
    unittest.main()
