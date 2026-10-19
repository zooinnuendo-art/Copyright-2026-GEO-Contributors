import unittest

from tools.geo_geodesic_probe import (
    compute_bidirectional_dependency_meetings,
    compute_dependency_geodesics,
)


class GeoGeodesicProbeTests(unittest.TestCase):
    def setUp(self):
        self.graph = {
            "nodes": [
                {"id": "file:src/core.py", "type": "file", "path": "src/core.py"},
                {"id": "function:src/core.py:run", "type": "function", "path": "src/core.py"},
                {"id": "file:tests/test_core.py", "type": "file", "path": "tests/test_core.py"},
                {"id": "test:tests/test_core.py:test_run", "type": "test", "path": "tests/test_core.py"},
            ],
            "edges": [
                {"source": "file:src/core.py", "target": "function:src/core.py:run", "type": "contains"},
                {"source": "file:tests/test_core.py", "target": "test:tests/test_core.py:test_run", "type": "contains"},
                {
                    "source": "test:tests/test_core.py:test_run",
                    "target": "function:src/core.py:run",
                    "type": "calls",
                    "confidence": 0.6,
                },
            ],
            "scope": {
                "files": ["src/core.py"],
                "linked_test_files": ["tests/test_core.py"],
            },
            "issues": [{
                "kind": "unresolved_attribute_call_summary",
                "count": 2,
                "examples": [{"path": "tests/test_core.py", "line": 9, "expression": "obj.run"}],
            }],
        }

    def test_computes_weighted_dependency_path_and_boundary_separately(self):
        result = compute_dependency_geodesics(self.graph, ["src/core.py"])

        test_distance = result["test_distances"][0]
        self.assertTrue(test_distance["reachable"])
        self.assertEqual(test_distance["distance"], 1.9)
        self.assertEqual(test_distance["hops"], 3)
        self.assertEqual(test_distance["path"], [
            "file:src/core.py",
            "function:src/core.py:run",
            "test:tests/test_core.py:test_run",
            "file:tests/test_core.py",
        ])
        self.assertFalse(result["distance_is_metric"])
        self.assertEqual(result["unresolved_boundary_evidence"][0]["examples_in_scope"][0]["line"], 9)

    def test_missing_change_is_reported_without_inventing_a_path(self):
        result = compute_dependency_geodesics(self.graph, ["src/missing.py"])

        self.assertEqual(result["missing_changed_files"], ["src/missing.py"])
        self.assertFalse(result["test_distances"][0]["reachable"])
        self.assertIsNone(result["test_distances"][0]["distance"])

    def test_rejects_invalid_confidence_and_empty_change_set(self):
        invalid = {**self.graph, "edges": [
            {"source": "file:src/core.py", "target": "function:src/core.py:run", "type": "contains", "confidence": 1.5},
        ]}
        with self.assertRaisesRegex(ValueError, "confidence"):
            compute_dependency_geodesics(invalid, ["src/core.py"])
        with self.assertRaisesRegex(ValueError, "non-empty"):
            compute_dependency_geodesics(self.graph, [])

    def test_bidirectional_waves_report_shared_route_and_preserve_boundary(self):
        graph = {**self.graph, "status": "snapshot_only/review_required"}

        result = compute_bidirectional_dependency_meetings(graph, ["src/core.py"])

        self.assertEqual(result["connected_candidate_count"], 1)
        candidate = result["candidate_tests"][0]
        self.assertEqual(candidate["classification"], "connected_with_boundary_evidence")
        self.assertEqual(candidate["boundary_evidence"][0]["kind"], "unresolved_attribute_call_summary")
        meeting_ids = {meeting["node_id"] for meeting in candidate["meeting_points"]}
        self.assertIn("function:src/core.py:run", meeting_ids)
        self.assertIn("test:tests/test_core.py:test_run", meeting_ids)

    def test_bidirectional_waves_do_not_bridge_disconnected_boundary(self):
        disconnected = {
            "nodes": [
                {"id": "file:src/core.py", "type": "file", "path": "src/core.py"},
                {"id": "file:tests/test_core.py", "type": "file", "path": "tests/test_core.py"},
            ],
            "edges": [],
            "scope": {"linked_test_files": ["tests/test_core.py"]},
            "issues": [{"kind": "unresolved_local_import", "path": "tests/test_core.py", "target": "src.dynamic"}],
            "status": "snapshot_only/review_required",
        }

        result = compute_bidirectional_dependency_meetings(disconnected, ["src/core.py"])

        self.assertEqual(result["connected_candidate_count"], 0)
        self.assertEqual(result["boundary_candidate_count"], 1)
        self.assertEqual(result["candidate_tests"][0]["classification"], "no_static_meeting_with_boundary")

    def test_unrelated_issue_does_not_contaminate_a_static_meeting(self):
        graph = {
            **self.graph,
            "issues": [{"kind": "parse_error", "path": "unrelated/broken.py"}],
        }

        result = compute_bidirectional_dependency_meetings(graph, ["src/core.py"])

        self.assertEqual(result["candidate_tests"][0]["classification"], "connected_static_path")
        self.assertEqual(result["candidate_tests"][0]["boundary_evidence"], [])

    def test_incomplete_snapshot_marks_candidates_as_uncertain(self):
        graph = {**self.graph, "status": "incomplete/review", "issues": []}

        result = compute_bidirectional_dependency_meetings(graph, ["src/core.py"])

        candidate = result["candidate_tests"][0]
        self.assertEqual(candidate["classification"], "connected_with_boundary_evidence")
        self.assertIn("snapshot_incomplete", {item["kind"] for item in candidate["boundary_evidence"]})

    def test_depth_limit_abstention_is_local_to_the_affected_test_path(self):
        graph = {
            **self.graph,
            "nodes": [
                *self.graph["nodes"],
                {"id": "file:tests/test_other.py", "type": "file", "path": "tests/test_other.py"},
                {"id": "test:tests/test_other.py:test_run", "type": "test", "path": "tests/test_other.py"},
            ],
            "edges": [
                *self.graph["edges"],
                {"source": "file:tests/test_other.py", "target": "test:tests/test_other.py:test_run", "type": "contains"},
                {"source": "test:tests/test_other.py:test_run", "target": "function:src/core.py:run", "type": "calls", "confidence": 1.0},
            ],
            "scope": {
                "files": ["src/core.py"],
                "linked_test_files": ["tests/test_core.py", "tests/test_other.py"],
            },
            "issues": [{
                "kind": "test_call_depth_limit_reached",
                "path": "tests/test_core.py",
                "limit": 8,
                "terminal_node": "function:tests/support.py:deep_helper",
                "untraversed_target": "function:pkg/next.py:next_helper",
            }, {
                "kind": "unresolved_simple_call_summary",
                "count": 12,
                "examples": [{"path": "pkg/unrelated.py", "line": 4, "name": "unknown"}],
            }],
            "incomplete_causes": {
                "global": [],
                "scope": [{
                    "kind": "test_call_depth_limit_reached",
                    "path": "tests/test_core.py",
                    "limit": 8,
                }],
            },
            "status": "incomplete/review",
        }

        result = compute_bidirectional_dependency_meetings(graph, ["src/core.py"])

        candidates = {item["test_file"]: item for item in result["candidate_tests"]}
        self.assertFalse(result["global_incomplete"])
        self.assertEqual(candidates["tests/test_core.py"]["classification"], "connected_with_boundary_evidence")
        self.assertIn("test_call_depth_limit_reached", {
            item["kind"] for item in candidates["tests/test_core.py"]["boundary_evidence"]
        })
        self.assertEqual(candidates["tests/test_other.py"]["classification"], "connected_static_path")
        self.assertEqual(candidates["tests/test_other.py"]["boundary_evidence"], [])

    def test_bidirectional_waves_report_missing_test_endpoint(self):
        result = compute_bidirectional_dependency_meetings(
            self.graph,
            ["src/core.py"],
            test_files=["tests/missing.py"],
        )

        self.assertEqual(result["candidate_tests"][0]["classification"], "test_file_missing")


if __name__ == "__main__":
    unittest.main()