import json
import unittest
from unittest.mock import patch

from tools.evaluate_geo_boundary_transitions import run_boundary_transition_evaluation
from tools.geo_boundary_transition_probe import analyze_boundary_transitions


class BoundaryTransitionEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_boundary_transition_evaluation()
        cls.cases = {case["case_id"]: case for case in cls.report["cases"]}

    def test_evaluation_is_explicitly_synthetic_and_json_serializable(self):
        self.assertFalse(self.report["labels_independent"])
        self.assertFalse(self.report["fixture_code_executed"])
        self.assertEqual(len(self.cases), self.report["case_count"])
        json.dumps(self.report, allow_nan=False)

    def test_eight_and_nine_call_threshold_and_recovered_candidate(self):
        eight = self.cases["chain_8_calls_fallback_False"]
        nine = self.cases["chain_9_calls_fallback_False"]
        recovered = self.cases["chain_9_calls_fallback_True"]
        self.assertEqual(eight["static"]["recalled"], ["tests/test_chain.py"])
        self.assertEqual(nine["fallback"]["missed"], ["tests/test_chain.py"])
        self.assertEqual(recovered["added_expected_candidates"], ["tests/test_chain.py"])
        candidate = recovered["probe"]["candidate_tests"][0]
        self.assertTrue(candidate["boundary_preserved"])
        self.assertEqual(candidate["evidence_status"], "review_required")

    def test_noise_is_counted_not_hidden_by_label_or_boundary(self):
        noise = self.cases["metadata_only_name_reference_noise"]
        self.assertEqual(noise["static"]["selected"], [])
        self.assertEqual(noise["added_unnecessary_candidates"], ["tests/test_name.py"])
        self.assertEqual(noise["fallback"]["unnecessary"], ["tests/test_name.py"])

    def test_boundary_and_promotion_invariants_are_measured(self):
        self.assertEqual(self.report["invariants"], {
            "lost_depth_boundaries": 0,
            "cleared_depth_boundary_candidates": 0,
            "unsafe_evidence_promotions": 0,
        })
        self.assertTrue(self.cases["dynamic_inside_known_provider"]["probe"]["candidate_tests"][0]["boundary_preserved"])

    def test_aggregate_metrics_match_case_rows(self):
        for mode in ("static", "fallback"):
            totals = self.report["methods"][mode]
            for key in ("selected", "recalled", "missed", "unnecessary"):
                self.assertEqual(totals[key], sum(len(row[mode][key]) for row in self.cases.values()))
            self.assertEqual(totals["expected"], totals["recalled"] + totals["missed"])
            self.assertEqual(totals["selected"], totals["recalled"] + totals["unnecessary"])
            self.assertEqual(totals["precision"], totals["recalled"] / totals["selected"])
            self.assertEqual(totals["recall"], totals["recalled"] / totals["expected"])

    def test_deterministic_and_no_temporary_absolute_paths(self):
        self.assertEqual(self.report, run_boundary_transition_evaluation())
        self.assertNotIn("geo-boundary-eval-", json.dumps(self.report))

    def test_invariant_checks_detect_dropped_boundaries_and_unsafe_promotion(self):
        def damaged_probe(*graphs, **options):
            result = analyze_boundary_transitions(*graphs, **options)
            result["boundary_entries"] = []
            for row in result["candidate_tests"]:
                row["boundary_preserved"] = False
                row["evidence_status"] = "known_static_route_no_reported_boundary"
            return result

        with patch(
            "tools.evaluate_geo_boundary_transitions.analyze_boundary_transitions",
            side_effect=damaged_probe,
        ):
            report = run_boundary_transition_evaluation()
        self.assertGreater(report["invariants"]["lost_depth_boundaries"], 0)
        self.assertGreater(report["invariants"]["cleared_depth_boundary_candidates"], 0)
        self.assertGreater(report["invariants"]["unsafe_evidence_promotions"], 0)

    def test_one_extra_hop_recovers_remaining_positive_without_removing_noise(self):
        report = run_boundary_transition_evaluation(extra_call_hops=1)
        self.assertEqual(report["continuation_added_expected_candidates"], 1)
        self.assertEqual(report["continuation_added_unnecessary_candidates"], 0)
        self.assertEqual(report["methods"]["continuation"]["recalled"], 7)
        self.assertEqual(report["methods"]["continuation"]["unnecessary"], 1)
        self.assertGreater(report["continuation_visits"], 0)
        self.assertTrue(all(value == 0 for value in report["invariants"].values()))


if __name__ == "__main__":
    unittest.main()
