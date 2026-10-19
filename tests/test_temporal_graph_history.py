import unittest
import tempfile
from pathlib import Path

from tools.code_dependency_graph import build_dependency_graph
from tools.temporal_graph_history import build_temporal_snapshot_record


class TemporalGraphHistoryTests(unittest.TestCase):
    def setUp(self):
        self.parent = {
            "status": "snapshot_only/review_required",
            "nodes": [
                {"id": "file:pkg/core.py", "type": "file", "path": "pkg/core.py"},
                {"id": "function:pkg/core.py:run", "type": "function", "path": "pkg/core.py", "line": 1},
                {"id": "test:tests/test_core.py:test_run", "type": "test", "path": "tests/test_core.py"},
                {"id": "function:pkg/core.py:old", "type": "function", "path": "pkg/core.py", "line": 8},
            ],
            "edges": [
                {"source": "file:pkg/core.py", "target": "function:pkg/core.py:run", "type": "contains", "line": 1},
                {"source": "test:tests/test_core.py:test_run", "target": "function:pkg/core.py:run", "type": "tests", "line": 5},
                {"source": "function:pkg/core.py:run", "target": "function:pkg/core.py:old", "type": "calls", "line": 2},
            ],
            "issues": [{"kind": "unresolved_call", "path": "pkg/core.py", "line": 3}],
            "cache": {"status": "miss"},
        }
        self.current = {
            "status": "incomplete/review",
            "nodes": [
                {"id": "file:pkg/core.py", "type": "file", "path": "pkg/core.py"},
                {"id": "function:pkg/core.py:run", "type": "function", "path": "pkg/core.py", "line": 1, "signature": "run(value)"},
                {"id": "test:tests/test_core.py:test_run", "type": "test", "path": "tests/test_core.py"},
                {"id": "function:pkg/core.py:new", "type": "function", "path": "pkg/core.py", "line": 9},
            ],
            "edges": [
                {"source": "file:pkg/core.py", "target": "function:pkg/core.py:run", "type": "contains", "line": 1},
                {"source": "test:tests/test_core.py:test_run", "target": "function:pkg/core.py:run", "type": "tests", "line": 5},
                {"source": "function:pkg/core.py:run", "target": "function:pkg/core.py:new", "type": "calls", "line": 2},
            ],
            "issues": [
                {"kind": "unresolved_call", "path": "pkg/core.py", "line": 3},
                {"kind": "parse_error", "path": "pkg/other.py"},
            ],
            "cache": {"status": "hit"},
        }

    def test_revision_record_tracks_graph_delta_tests_and_extractor_uncertainty(self):
        record = build_temporal_snapshot_record(
            self.current,
            revision_id="rev-2",
            sequence_index=1,
            parent_revision_ids=("rev-1",),
            parent_snapshot=self.parent,
            comparison_parent_revision_id="rev-1",
            comparison_parent_sequence_index=0,
        )

        self.assertEqual(record["revision"]["sequence_index"], 1)
        self.assertEqual(record["delta"]["nodes"]["added"], [self.current["nodes"][3]])
        self.assertEqual(record["delta"]["nodes"]["removed"], [self.parent["nodes"][3]])
        self.assertEqual(record["delta"]["nodes"]["changed"][0]["after"], self.current["nodes"][1])
        self.assertEqual([edge["type"] for edge in record["delta"]["edges"]["added"]], ["calls"])
        self.assertEqual([edge["type"] for edge in record["delta"]["edges"]["removed"]], ["calls"])
        self.assertEqual(record["delta"]["directly_linked_tests"], ["test:tests/test_core.py:test_run"])
        self.assertEqual(record["extractor_uncertainty"]["status_after"], "incomplete/review")
        self.assertEqual(record["extractor_uncertainty"]["introduced_issues"], [{"kind": "parse_error", "path": "pkg/other.py"}])
        self.assertIsNone(record["outcome_annotation"])

    def test_snapshot_hash_ignores_operational_cache_metadata(self):
        first = build_temporal_snapshot_record(
            self.current,
            revision_id="rev-2",
            sequence_index=1,
            parent_revision_ids=("rev-1",),
            parent_snapshot=self.parent,
            comparison_parent_revision_id="rev-1",
            comparison_parent_sequence_index=0,
        )
        second_snapshot = {**self.current, "cache": {"status": "miss", "hits": 0}}
        second = build_temporal_snapshot_record(
            second_snapshot,
            revision_id="rev-2",
            sequence_index=1,
            parent_revision_ids=("rev-1",),
            parent_snapshot=self.parent,
            comparison_parent_revision_id="rev-1",
            comparison_parent_sequence_index=0,
        )

        self.assertEqual(first["graph_sha256"], second["graph_sha256"])

    def test_root_revision_starts_from_empty_graph_and_has_no_outcome_label(self):
        record = build_temporal_snapshot_record(
            self.parent,
            revision_id="rev-1",
            sequence_index=0,
        )

        self.assertEqual(len(record["delta"]["nodes"]["added"]), len(self.parent["nodes"]))
        self.assertIsNone(record["parent_graph_sha256"])
        self.assertIsNone(record["outcome_annotation"])

    def test_non_root_revision_requires_explicit_comparison_parent(self):
        with self.assertRaisesRegex(ValueError, "comparison parent"):
            build_temporal_snapshot_record(
                self.current,
                revision_id="rev-2",
                sequence_index=1,
                parent_revision_ids=("rev-1",),
                parent_snapshot=self.parent,
            )

    def test_comparison_parent_must_precede_revision(self):
        with self.assertRaisesRegex(ValueError, "earlier comparison parent"):
            build_temporal_snapshot_record(
                self.current,
                revision_id="rev-2",
                sequence_index=1,
                parent_revision_ids=("rev-1",),
                parent_snapshot=self.parent,
                comparison_parent_revision_id="rev-1",
                comparison_parent_sequence_index=1,
            )

    def test_actual_dependency_graph_snapshots_produce_a_test_link_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "tests").mkdir()
            (root / "pkg" / "core.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            (root / "tests" / "test_core.py").write_text(
                "from pkg.core import run\n"
                "def test_run():\n    run()\n",
                encoding="utf-8",
            )
            parent = build_dependency_graph(root, changed_files=["pkg/core.py"], max_hops=0)
            (root / "pkg" / "core.py").write_text("def run_new():\n    return 1\n", encoding="utf-8")
            (root / "tests" / "test_core.py").write_text(
                "from pkg.core import run_new\n"
                "def test_run():\n    run_new()\n",
                encoding="utf-8",
            )
            current = build_dependency_graph(root, changed_files=["pkg/core.py"], max_hops=0)

        record = build_temporal_snapshot_record(
            current,
            revision_id="rev-2",
            sequence_index=1,
            parent_revision_ids=("rev-1",),
            parent_snapshot=parent,
            comparison_parent_revision_id="rev-1",
            comparison_parent_sequence_index=0,
        )

        test_ids = {node["id"] for node in current["nodes"] if node["type"] == "test"}
        self.assertEqual(record["delta"]["directly_linked_tests"], sorted(test_ids))


if __name__ == "__main__":
    unittest.main()