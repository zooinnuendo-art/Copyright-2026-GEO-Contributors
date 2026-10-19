from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_history_tension_probe import analyze_history_structural_tension


def _record(revision_id, comparison_parent_id, paths, status="snapshot_only/review_required"):
    return {
        "revision": {
            "id": revision_id,
            "comparison_parent_id": comparison_parent_id,
        },
        "delta": {
            "nodes": {
                "added": [],
                "removed": [],
                "changed": [{"before": {"path": path}, "after": {"path": path}} for path in paths],
            },
            "edges": {"added": [], "removed": [], "changed": []},
        },
        "extractor_uncertainty": {"status_after": status},
        "ast_delta": {"symbols": {"added": [], "removed": [], "changed": []}},
    }


class GeoHistoryTensionProbeTests(unittest.TestCase):
    def test_frequent_cochange_with_no_static_path_is_reported_as_hotspot(self):
        graph = {
            "nodes": [
                {"id": "file:pkg/a.py", "type": "file", "path": "pkg/a.py"},
                {"id": "file:pkg/b.py", "type": "file", "path": "pkg/b.py"},
                {"id": "file:pkg/c.py", "type": "file", "path": "pkg/c.py"},
            ],
            "edges": [],
        }
        history = {"records": [
            _record("r0", None, ["pkg/a.py", "pkg/b.py", "pkg/c.py"]),
            _record("r1", "r0", ["pkg/a.py", "pkg/b.py"]),
            _record("r2", "r1", ["pkg/a.py", "pkg/b.py"]),
            _record("r3", "r2", ["pkg/a.py"]),
            _record("r4", "r3", ["pkg/b.py"]),
        ]}

        result = analyze_history_structural_tension(
            graph,
            history,
            minimum_cochange_commits=2,
            hotspot_threshold=0.5,
        )

        pair = next(row for row in result["pairs"] if row["left_path"] == "pkg/a.py" and row["right_path"] == "pkg/b.py")
        self.assertEqual(result["comparable_revisions"], 4)
        self.assertEqual(result["excluded_root_records"], 1)
        self.assertIsNone(pair["structural_distance_hops"])
        self.assertEqual(pair["historical_jaccard_proximity"], 0.5)
        self.assertEqual(pair["tension"], 0.5)
        self.assertTrue(pair["hotspot"])

    def test_structural_dependency_distance_reduces_tension_and_incomplete_history_is_excluded(self):
        graph = {
            "nodes": [
                {"id": "file:pkg/a.py", "type": "file", "path": "pkg/a.py"},
                {"id": "function:pkg/a.py:run", "type": "function", "path": "pkg/a.py"},
                {"id": "file:pkg/b.py", "type": "file", "path": "pkg/b.py"},
                {"id": "function:pkg/b.py:helper", "type": "function", "path": "pkg/b.py"},
            ],
            "edges": [
                {"source": "file:pkg/a.py", "target": "function:pkg/a.py:run", "type": "contains"},
                {"source": "function:pkg/a.py:run", "target": "function:pkg/b.py:helper", "type": "calls"},
            ],
        }
        history = {"records": [
            _record("r1", "r0", ["pkg/a.py", "pkg/b.py"]),
            _record("r2", "r1", ["pkg/a.py", "pkg/b.py"]),
            _record("r3", "r2", ["pkg/a.py", "pkg/b.py"], status="incomplete/review"),
        ]}

        result = analyze_history_structural_tension(graph, history)

        pair = result["pairs"][0]
        self.assertEqual(result["comparable_revisions"], 2)
        self.assertEqual(result["excluded_incomplete_records"], 1)
        self.assertEqual(pair["structural_distance_hops"], 1)
        self.assertEqual(pair["structural_proximity"], 0.5)
        self.assertEqual(pair["historical_jaccard_proximity"], 1.0)
        self.assertEqual(pair["tension"], 0.5)

    def test_rejects_invalid_budgets_and_thresholds(self):
        with self.assertRaisesRegex(ValueError, "positive integer"):
            analyze_history_structural_tension({"nodes": [], "edges": []}, {"records": []}, minimum_cochange_commits=0)
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            analyze_history_structural_tension({"nodes": [], "edges": []}, {"records": []}, hotspot_threshold=1.1)
        with self.assertRaisesRegex(ValueError, "record count"):
            analyze_history_structural_tension(
                {"nodes": [], "edges": []},
                {"records": [{}, {}]},
                max_history_records=1,
            )

    def test_git_renames_project_history_to_final_paths_and_ignore_pure_moves(self):
        def git_record(revision, parent, changes):
            record = _record(revision, parent, ["unmodified_dependency.py"])
            record["file_changes"] = changes
            return record

        history = {"records": [
            git_record("r1", "r0", [
                {"status": "M", "path": "old.py"}, {"status": "M", "path": "peer.py"},
            ]),
            git_record("r2", "r1", [
                {"status": "R", "old_path": "old.py", "path": "middle.py", "content_changed": False},
            ]),
            git_record("r3", "r2", [
                {"status": "R", "old_path": "middle.py", "path": "final.py", "content_changed": True},
                {"status": "M", "path": "peer.py"},
            ]),
        ]}
        result = analyze_history_structural_tension({"nodes": [], "edges": []}, history)
        self.assertEqual(result["path_identity"], "git_changes_projected_to_final_revision")
        self.assertEqual(result["pair_count"], 1)
        pair = result["pairs"][0]
        self.assertEqual((pair["left_path"], pair["right_path"]), ("final.py", "peer.py"))
        self.assertEqual(pair["cochange_commits"], 2)
        self.assertEqual(pair["left_changed_commits"], 2)
        self.assertEqual(pair["historical_jaccard_proximity"], 1.0)

    def test_reusing_old_path_does_not_merge_distinct_file_identities(self):
        changes = [
            [{"status": "M", "path": "old.py"}, {"status": "M", "path": "peer.py"}],
            [{"status": "R", "old_path": "old.py", "path": "new.py", "content_changed": False}],
            [{"status": "A", "path": "old.py"}, {"status": "M", "path": "peer.py"}],
        ]
        records = []
        for index, items in enumerate(changes, 1):
            record = _record(f"r{index}", f"r{index - 1}", [])
            record["file_changes"] = items
            records.append(record)
        result = analyze_history_structural_tension({"nodes": [], "edges": []}, {"records": records})
        pairs = {(row["left_path"], row["right_path"]): row for row in result["pairs"]}
        self.assertEqual(set(pairs), {("new.py", "peer.py"), ("old.py", "peer.py")})
        self.assertEqual(pairs[("new.py", "peer.py")]["cochange_commits"], 1)
        self.assertEqual(pairs[("old.py", "peer.py")]["cochange_commits"], 1)
        self.assertEqual(pairs[("new.py", "peer.py")]["historical_jaccard_proximity"], 0.5)

    def test_deleted_then_recreated_path_does_not_inherit_deleted_history(self):
        records = [_record(f"r{i}", f"r{i - 1}", []) for i in range(1, 4)]
        records[0]["file_changes"] = [
            {"status": "M", "path": "a.py"}, {"status": "M", "path": "b.py"},
        ]
        records[1]["file_changes"] = [{"status": "D", "path": "a.py"}]
        records[2]["file_changes"] = [{"status": "A", "path": "a.py"}]
        result = analyze_history_structural_tension({"nodes": [], "edges": []}, {"records": records})
        self.assertEqual(result["pairs"], [])

    def test_git_file_changes_reject_malformed_or_oversized_metadata(self):
        record = _record("r1", "r0", [])
        record["file_changes"] = [{"status": "R", "path": "new.py"}]
        with self.assertRaisesRegex(ValueError, "rename source"):
            analyze_history_structural_tension({"nodes": [], "edges": []}, {"records": [record]})
        record["file_changes"] = [
            {"status": "M", "path": "a.py"}, {"status": "M", "path": "b.py"},
        ]
        with self.assertRaisesRegex(ValueError, "per-revision"):
            analyze_history_structural_tension(
                {"nodes": [], "edges": []}, {"records": [record]}, max_paths_per_revision=1,
            )

    def test_git_records_require_consistent_order_and_metadata(self):
        first = _record("r1", "r0", [])
        second = _record("r2", "r1", [])
        first["file_changes"] = [{"status": "M", "path": "a.py"}]
        second["file_changes"] = []
        with self.assertRaisesRegex(ValueError, "oldest-first"):
            analyze_history_structural_tension(
                {"nodes": [], "edges": []}, {"records": [second, first]},
            )
        del second["file_changes"]
        with self.assertRaisesRegex(ValueError, "cannot mix"):
            analyze_history_structural_tension(
                {"nodes": [], "edges": []}, {"records": [first, second]},
            )

    def test_historical_path_budget_includes_content_identical_moves(self):
        record = _record("r1", "r0", [])
        record["file_changes"] = [{
            "status": "R", "old_path": "old.py", "path": "new.py", "content_changed": False,
        }]
        with self.assertRaisesRegex(ValueError, "historical paths"):
            analyze_history_structural_tension(
                {"nodes": [], "edges": []}, {"records": [record]}, max_unique_paths=1,
            )

    def test_explicit_filter_removes_utility_noise_without_changing_business_pair(self):
        history = {"records": [
            _record("r0", None, ["root_only.py"]),
            _record("r1", "r0", ["pkg/a.py", "pkg/b.py", "pkg/settings.py"]),
            _record("r2", "r1", ["pkg/a.py", "pkg/b.py", "pkg/settings.py"]),
            _record("r3", "r2", ["pkg/settings.py"]),
            _record("r4", "r3", ["incomplete_only.py"], status="incomplete/review"),
        ]}
        graph = {"nodes": [], "edges": []}
        baseline = analyze_history_structural_tension(graph, history)
        filtered = analyze_history_structural_tension(
            graph, history, excluded_path_patterns=["pkg/settings.py", "pkg/sett*.py"],
        )
        business = next(
            row for row in baseline["pairs"]
            if (row["left_path"], row["right_path"]) == ("pkg/a.py", "pkg/b.py")
        )
        self.assertEqual(baseline["pair_count"], 3)
        self.assertEqual(filtered["pairs"], [business])
        self.assertTrue(business["hotspot"])
        self.assertEqual(business["tension"], 1.0)
        self.assertEqual(business["left_change_frequency"], 0.666666666667)
        self.assertEqual(filtered["comparable_revisions"], 3)
        self.assertEqual(filtered["noise_filter"]["excluded_path_count"], 1)
        self.assertEqual(filtered["noise_filter"]["excluded_path_revision_count"], 3)
        contexts = {row["path"]: row for row in filtered["files"]}
        self.assertEqual(set(contexts), {"pkg/a.py", "pkg/b.py", "pkg/settings.py"})
        settings = contexts["pkg/settings.py"]
        self.assertTrue(settings["excluded"])
        self.assertEqual(settings["matched_exclusion_patterns"], ["pkg/sett*.py", "pkg/settings.py"])
        self.assertEqual(settings["change_frequency"], 1.0)
        self.assertEqual(contexts["pkg/a.py"]["static_degree"], 0)

    def test_excluded_hub_remains_in_shortest_paths_and_file_degree_is_deduplicated(self):
        paths = ["a.py", "b.py", "utils.py"]
        graph = {
            "nodes": [{"id": f"file:{path}", "path": path} for path in paths],
            "edges": [
                {"source": "file:a.py", "target": "file:utils.py", "type": "imports"},
                {"source": "file:a.py", "target": "file:utils.py", "type": "calls"},
                {"source": "file:utils.py", "target": "file:a.py", "type": "reads"},
                {"source": "file:b.py", "target": "file:utils.py", "type": "imports"},
                {"source": "file:a.py", "target": "file:a.py", "type": "calls"},
                {"source": "file:a.py", "target": "file:b.py", "type": "contains"},
            ],
        }
        history = {"records": [_record("r1", "r0", paths)]}
        result = analyze_history_structural_tension(
            graph, history, excluded_path_patterns=["utils.py"],
        )
        pair = result["pairs"][0]
        self.assertEqual(result["pair_count"], 1)
        self.assertEqual(pair["structural_distance_hops"], 2)
        self.assertEqual(pair["tension"], 0.666666666667)
        self.assertEqual(pair["left_static_degree"], 1)
        self.assertEqual(pair["right_static_degree"], 1)
        self.assertEqual(
            {row["path"]: row["static_degree"] for row in result["files"]},
            {"a.py": 1, "b.py": 1, "utils.py": 2},
        )
        self.assertTrue(result["noise_filter"]["static_paths_preserved"])

    def test_patterns_are_case_sensitive_whole_path_fnmatch(self):
        paths = ["settings.py", "pkg/settings.py", "pkg/SETTINGS.py", "generated/x.py",
                 "generated/deep/y.py", "pkg/generated/z.py"]
        history = {"records": [_record("r1", "r0", paths)]}
        result = analyze_history_structural_tension(
            {"nodes": [], "edges": []}, history,
            excluded_path_patterns=["generated/*", "*settings.py", "*settings.py"],
        )
        self.assertEqual(result["noise_filter"]["excluded_path_patterns"], ["*settings.py", "generated/*"])
        self.assertEqual(
            {row["path"] for row in result["files"] if row["excluded"]},
            {"settings.py", "pkg/settings.py", "generated/x.py", "generated/deep/y.py"},
        )
        self.assertEqual(result["pair_count"], 1)

    def test_exclusion_matches_projected_final_name_not_rename_source(self):
        first, second = _record("r1", "r0", []), _record("r2", "r1", [])
        first["file_changes"] = [
            {"status": "M", "path": "old.py"}, {"status": "M", "path": "peer.py"},
        ]
        second["file_changes"] = [{
            "status": "R", "old_path": "old.py", "path": "new.py", "content_changed": False,
        }]
        history = {"records": [first, second]}
        old_filter = analyze_history_structural_tension(
            {"nodes": [], "edges": []}, history, excluded_path_patterns=["old.py"],
        )
        new_filter = analyze_history_structural_tension(
            {"nodes": [], "edges": []}, history, excluded_path_patterns=["new.py"],
        )
        self.assertEqual(old_filter["pair_count"], 1)
        self.assertEqual(old_filter["noise_filter"]["excluded_path_count"], 0)
        self.assertEqual(new_filter["pair_count"], 0)
        self.assertEqual(new_filter["noise_filter"]["excluded_path_revision_count"], 1)
        self.assertEqual(new_filter["files"][0]["change_frequency"], 0.5)

    def test_empty_and_fully_filtered_results_have_explicit_audit_context(self):
        empty = analyze_history_structural_tension(
            {"nodes": [], "edges": []}, {"records": []}, excluded_path_patterns=["*"],
        )
        self.assertEqual(empty["files"], [])
        self.assertEqual(empty["noise_filter"]["excluded_path_count"], 0)
        self.assertEqual(empty["noise_filter"]["excluded_path_revision_count"], 0)
        all_filtered = analyze_history_structural_tension(
            {"nodes": [], "edges": []},
            {"records": [_record("r1", "r0", ["config.py", "constants.py"])]},
            excluded_path_patterns=["*"],
        )
        self.assertEqual(all_filtered["pairs"], [])
        self.assertEqual(all_filtered["hotspot_count"], 0)
        self.assertEqual(all_filtered["comparable_revisions"], 1)
        self.assertEqual(all_filtered["noise_filter"]["excluded_path_count"], 2)
        self.assertTrue(all(row["excluded"] for row in all_filtered["files"]))

    def test_no_matching_pattern_preserves_all_rankings_and_scores(self):
        history = {"records": [
            _record("r1", "r0", ["a.py", "b.py", "constants.py"]),
            _record("r2", "r1", ["a.py", "b.py"]),
        ]}
        baseline = analyze_history_structural_tension({"nodes": [], "edges": []}, history)
        filtered = analyze_history_structural_tension(
            {"nodes": [], "edges": []}, history, excluded_path_patterns=["absent/*"],
        )
        self.assertEqual(baseline["pairs"], filtered["pairs"])
        self.assertEqual(baseline["files"], filtered["files"])
        self.assertEqual(baseline["hotspot_count"], filtered["hotspot_count"])
        self.assertEqual(baseline["noise_filter"]["excluded_path_patterns"], [])
        self.assertEqual(baseline["noise_filter"]["excluded_path_count"], 0)

    def test_filters_do_not_bypass_path_budgets_but_apply_before_pair_budget(self):
        history = {"records": [_record("r1", "r0", ["a.py", "b.py", "config.py"])]}
        for budget in ({"max_paths_per_revision": 2}, {"max_unique_paths": 2}):
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, "budget"):
                analyze_history_structural_tension(
                    {"nodes": [], "edges": []}, history, excluded_path_patterns=["*"], **budget,
                )
        result = analyze_history_structural_tension(
            {"nodes": [], "edges": []}, history,
            excluded_path_patterns=["config.py"], max_pairs=1,
        )
        self.assertEqual(result["pair_count"], 1)

    def test_rejects_invalid_exclusion_pattern_arguments(self):
        for patterns in ("*.py", b"*.py", None, 42, [""], [None], ["a.py", 1], ["a\0.py"]):
            with self.subTest(patterns=patterns), self.assertRaisesRegex(ValueError, "excluded_path_patterns"):
                analyze_history_structural_tension(
                    {"nodes": [], "edges": []}, {"records": []}, excluded_path_patterns=patterns,
                )

    def test_missing_uncertainty_metadata_is_unknown_not_a_clean_result(self):
        result = analyze_history_structural_tension(
            {"nodes": [], "edges": []},
            {"records": [_record("r1", "r0", ["a.py", "b.py"])]},
        )
        self.assertEqual(result["pairs"][0]["structural_evidence"], "static_path_not_found")
        uncertainty = result["graph_uncertainty"]
        self.assertIsNone(uncertainty["graph_status"])
        self.assertIsNone(uncertainty["issues"])
        self.assertIsNone(uncertainty["incomplete_causes"])
        self.assertIsNone(uncertainty["unresolved_calls"]["reported_count"])
        self.assertFalse(uncertainty["issues_reported"])
        self.assertFalse(uncertainty["incomplete_causes_reported"])
        self.assertFalse(uncertainty["unresolved_calls"]["per_file_counts_available"])

    def test_uncertainty_is_global_preserves_samples_and_does_not_reweight_scores(self):
        graph = {"nodes": [], "edges": []}
        history = {"records": [_record("r1", "r0", ["a.py", "b.py"])]}
        baseline = analyze_history_structural_tension(graph, history)
        graph.update({
            "status": "incomplete/review",
            "incomplete_causes": {"global": [{"kind": "node_budget_exceeded"}], "scope": []},
            "issues": [
                {"kind": "unresolved_attribute_call_summary", "count": 50, "examples": [
                    {"path": "unrelated.py", "line": 2, "expression": "plugin.run"},
                ]},
                {"kind": "unresolved_simple_call_summary", "count": 1, "examples": [
                    {"path": "a.py", "line": 3, "name": "injected"},
                ]},
                {"kind": "unresolved_local_import", "path": "unrelated.py"},
            ],
        })
        original = deepcopy(graph)
        result = analyze_history_structural_tension(graph, history)
        self.assertEqual(result["pairs"], baseline["pairs"])
        self.assertEqual(result["hotspot_count"], baseline["hotspot_count"])
        uncertainty = result["graph_uncertainty"]
        self.assertEqual(uncertainty["scope"], "whole_graph_not_pair_attributed")
        self.assertTrue(uncertainty["incomplete_reported"])
        self.assertEqual(uncertainty["issues"], graph["issues"])
        self.assertEqual(uncertainty["incomplete_causes"], graph["incomplete_causes"])
        calls = uncertainty["unresolved_calls"]
        self.assertEqual(calls["reported_count"], 51)
        self.assertTrue(calls["summaries"][0]["examples_truncated"])
        self.assertFalse(calls["summaries"][1]["examples_truncated"])
        self.assertEqual(graph, original)

    def test_reported_zero_calls_does_not_hide_nonempty_incomplete_causes(self):
        for status, causes, expected in (
            ("snapshot_only/review_required", {"global": [], "scope": []}, False),
            ("incomplete/review", {"global": [], "scope": []}, True),
            ("snapshot_only/review_required", {"global": [], "scope": [{"kind": "depth_limit"}]}, True),
        ):
            with self.subTest(status=status, causes=causes):
                result = analyze_history_structural_tension(
                    {"nodes": [], "edges": [], "status": status, "issues": [], "incomplete_causes": causes},
                    {"records": []},
                )
                uncertainty = result["graph_uncertainty"]
                self.assertEqual(uncertainty["incomplete_reported"], expected)
                self.assertEqual(uncertainty["unresolved_calls"]["reported_count"], 0)
                self.assertTrue(uncertainty["issues_reported"])
                self.assertTrue(uncertainty["incomplete_causes_reported"])

    def test_dynamic_calls_from_real_extractor_remain_unattributed_review_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "caller.py").write_text(
                "def run(plugin):\n    plugin.dispatch()\n    injected()\n", encoding="utf-8",
            )
            (root / "provider.py").write_text("def dispatch():\n    return 1\n", encoding="utf-8")
            graph = build_dependency_graph(root)
        history = {"records": [_record("r1", "r0", ["caller.py", "provider.py"])]}
        result = analyze_history_structural_tension(graph, history)
        pair = result["pairs"][0]
        self.assertIsNone(pair["structural_distance_hops"])
        self.assertEqual(pair["structural_evidence"], "static_path_not_found")
        self.assertEqual(pair["tension"], 1.0)
        self.assertTrue(pair["hotspot"])
        uncertainty = result["graph_uncertainty"]
        self.assertEqual(uncertainty["graph_status"], "snapshot_only/review_required")
        self.assertFalse(uncertainty["incomplete_reported"])
        self.assertEqual(uncertainty["unresolved_calls"]["reported_count"], 2)
        self.assertEqual(
            {summary["kind"] for summary in uncertainty["unresolved_calls"]["summaries"]},
            {"unresolved_attribute_call_summary", "unresolved_simple_call_summary"},
        )

    def test_known_path_is_still_qualified_when_graph_is_incomplete(self):
        graph = {
            "nodes": [],
            "edges": [{"source": "file:a.py", "target": "file:b.py", "type": "imports"}],
            "status": "incomplete/review",
            "issues": [{"kind": "parse_error", "path": "other.py"}],
            "incomplete_causes": {"global": [{"kind": "parse_error", "path": "other.py"}]},
        }
        result = analyze_history_structural_tension(
            graph, {"records": [_record("r1", "r0", ["a.py", "b.py"])]},
        )
        self.assertEqual(result["pairs"][0]["structural_evidence"], "known_static_path")
        self.assertEqual(result["pairs"][0]["structural_distance_hops"], 1)
        self.assertTrue(result["graph_uncertainty"]["incomplete_reported"])

    def test_real_parse_failure_is_retained_as_incomplete_graph_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            (root / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
            graph = build_dependency_graph(root)
        result = analyze_history_structural_tension(
            graph, {"records": [_record("r1", "r0", ["broken.py", "other.py"])]},
        )
        self.assertTrue(result["graph_uncertainty"]["incomplete_reported"])
        self.assertIn(
            "parse_error",
            {cause["kind"] for cause in result["graph_uncertainty"]["incomplete_causes"]["global"]},
        )
        self.assertEqual(result["pairs"][0]["structural_evidence"], "static_path_not_found")

    def test_filtering_all_pairs_preserves_uncertainty_from_excluded_files(self):
        issue = {
            "kind": "unresolved_simple_call_summary", "count": 3,
            "examples": [{"path": "settings.py", "name": "injected", "line": 1}],
        }
        result = analyze_history_structural_tension(
            {"nodes": [], "edges": [], "issues": [issue]},
            {"records": [_record("r1", "r0", ["settings.py", "other.py"])]},
            excluded_path_patterns=["*"],
        )
        self.assertEqual(result["pairs"], [])
        self.assertEqual(result["graph_uncertainty"]["unresolved_calls"]["reported_count"], 3)
        self.assertEqual(result["graph_uncertainty"]["issues"], [issue])

    def test_rejects_malformed_uncertainty_instead_of_reporting_clean_analysis(self):
        cases = [
            {"status": 1}, {"issues": {}}, {"issues": ["bad"]},
            {"incomplete_causes": []}, {"incomplete_causes": {"global": {}}},
            {"incomplete_causes": {"scope": ["bad"]}},
        ]
        for count in (None, -1, True, 1.5, "2"):
            cases.append({"issues": [{"kind": "unresolved_simple_call_summary", "count": count}]})
        for examples in ({}, ["bad"], [{}, {}]):
            cases.append({"issues": [{
                "kind": "unresolved_attribute_call_summary", "count": 1, "examples": examples,
            }]})
        for metadata in cases:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                analyze_history_structural_tension(
                    {"nodes": [], "edges": [], **metadata}, {"records": []},
                )


if __name__ == "__main__":
    unittest.main()