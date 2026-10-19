import unittest

from tools.dependency_impact_benchmark import (
    compare_import_use_fallback,
    compare_bidirectional_wave_selection,
    _wave_holdout_cases,
    run_dependency_impact_benchmark,
    compare_fallback_import_forms,
    fallback_import_form_cases,
)


class DependencyImpactBenchmarkTests(unittest.TestCase):
    def test_benchmark_is_deterministic_and_reports_known_misses_as_uncertainty(self):
        first = run_dependency_impact_benchmark()
        second = run_dependency_impact_benchmark()

        self.assertEqual(first, second)
        self.assertEqual(first["case_count"], 16)
        cases = {case["case_id"]: case for case in first["cases"]}
        self.assertEqual(cases["direct_call"]["missed_test_files"], [])
        self.assertEqual(cases["helper_mediated_call"]["missed_test_files"], [])
        self.assertEqual(cases["three_stage_production_helper_chain"]["missed_test_files"], [])
        self.assertEqual(cases["three_stage_production_helper_chain"]["actionable_recalled_test_files"], ["tests/test_pipeline.py"])
        self.assertEqual(cases["neighbor_scope_extra_test"]["unnecessary_test_files"], ["tests/test_util.py"])
        self.assertEqual(cases["provider_symbol_removed"]["missed_test_files"], [])
        self.assertTrue(cases["provider_symbol_removed"]["abstained"])
        self.assertEqual(cases["provider_module_removed"]["missed_test_files"], [])
        self.assertTrue(cases["provider_module_removed"]["abstained"])
        self.assertEqual(cases["submodule_import_removed"]["missed_test_files"], [])
        self.assertTrue(cases["submodule_import_removed"]["abstained"])
        self.assertEqual(cases["relative_import_removed"]["missed_test_files"], [])
        self.assertTrue(cases["relative_import_removed"]["abstained"])
        self.assertEqual(cases["literal_getattr_call"]["missed_test_files"], [])
        self.assertFalse(cases["literal_getattr_call"]["abstained"])
        self.assertEqual(cases["literal_getattr_call"]["resolved_calls_to_changed_scope"], 1)
        self.assertEqual(cases["computed_getattr_name"]["missed_test_files"], [])
        self.assertTrue(cases["computed_getattr_name"]["abstained"])
        self.assertEqual(cases["computed_getattr_name"]["resolved_calls_to_changed_scope"], 0)
        self.assertEqual(cases["shadowed_getattr_negative"]["selected_test_files"], [])
        self.assertTrue(cases["shadowed_getattr_negative"]["abstained"])
        self.assertEqual(cases["unused_import_negative"]["selected_test_files"], [])
        self.assertEqual(cases["same_name_shadow_negative"]["selected_test_files"], [])
        self.assertTrue(cases["same_name_shadow_negative"]["abstained"])
        for case_id in ("syntax_error_in_test", "file_budget_exceeded"):
            self.assertEqual(cases[case_id]["selected_test_files"], [])
            self.assertTrue(cases[case_id]["abstained"])
        self.assertEqual(cases["syntax_error_in_test"]["abstention_reasons"], ["syntax_error"])
        self.assertEqual(cases["file_budget_exceeded"]["abstention_reasons"], ["budget_exceeded"])
        self.assertIn("unresolved_call", cases["ambiguous_provider_symbol"]["abstention_reasons"])
        self.assertIn("snapshot_incomplete", cases["provider_module_removed"]["abstention_reasons"])
        self.assertIn("unresolved_call", cases["provider_module_removed"]["abstention_reasons"])
        self.assertIn("unresolved_attribute", cases["computed_getattr_name"]["abstention_reasons"])
        self.assertEqual(cases["ambiguous_provider_symbol"]["selected_test_files"], ["tests/test_api.py"])
        self.assertEqual(cases["ambiguous_provider_symbol"]["actionable_recalled_test_files"], [])
        self.assertEqual(first["counts"]["missed_test_files"], 2)
        self.assertEqual(first["counts"]["unnecessary_test_files"], 1)
        self.assertEqual(first["counts"]["abstained_cases"], 10)
        self.assertEqual(first["counts"]["complete_cases"], 6)
        self.assertAlmostEqual(first["metrics"]["affected_test_recall"], 11 / 13)
        self.assertAlmostEqual(first["metrics"]["actionable_affected_test_recall"], 5 / 13)
        self.assertEqual(first["metrics"]["complete_case_recall"], 1.0)
        self.assertAlmostEqual(first["metrics"]["selected_test_precision"], 11 / 12)
        self.assertAlmostEqual(first["metrics"]["unnecessary_selection_rate"], 1 / 12)
        self.assertAlmostEqual(first["metrics"]["abstention_rate"], 10 / 16)
        self.assertAlmostEqual(first["metrics"]["analysis_coverage"], 6 / 16)
        self.assertEqual(first["counts"]["abstention_cases_by_reason"], {
            "budget_exceeded": 1,
            "snapshot_incomplete": 3,
            "syntax_error": 1,
            "unresolved_attribute": 3,
            "unresolved_call": 5,
            "unresolved_import": 1,
        })

    def test_fallback_comparison_measures_recall_precision_and_abstention_tradeoff(self):
        comparison = compare_import_use_fallback()
        baseline = comparison["baseline"]
        enabled = comparison["fallback_enabled"]

        self.assertFalse(baseline["import_use_test_fallback"])
        self.assertTrue(enabled["import_use_test_fallback"])
        self.assertAlmostEqual(baseline["metrics"]["affected_test_recall"], 5 / 13)
        self.assertAlmostEqual(enabled["metrics"]["affected_test_recall"], 11 / 13)
        self.assertAlmostEqual(baseline["metrics"]["actionable_affected_test_recall"], 5 / 13)
        self.assertAlmostEqual(enabled["metrics"]["actionable_affected_test_recall"], 5 / 13)
        self.assertEqual(enabled["metrics"]["complete_case_recall"], 1.0)
        self.assertAlmostEqual(baseline["metrics"]["selected_test_precision"], 5 / 6)
        self.assertAlmostEqual(enabled["metrics"]["selected_test_precision"], 11 / 12)
        self.assertAlmostEqual(baseline["metrics"]["abstention_rate"], 10 / 16)
        self.assertAlmostEqual(enabled["metrics"]["abstention_rate"], 10 / 16)
        self.assertEqual(enabled["metrics"]["analysis_coverage"], 6 / 16)
        self.assertEqual(baseline["counts"]["unnecessary_test_files"], 1)
        shadow = {case["case_id"]: case for case in enabled["cases"]}["shadowed_getattr_negative"]
        self.assertEqual(shadow["selected_test_files"], [])
        delta = comparison["fallback_delta"]
        self.assertEqual(delta["counts"]["added_candidates"], 6)
        self.assertEqual(delta["counts"]["added_expected_candidates"], 6)
        self.assertEqual(delta["counts"]["added_actionable_expected_candidates"], 0)
        self.assertEqual(delta["counts"]["added_abstained_expected_candidates"], 6)
        self.assertEqual(delta["counts"]["added_unnecessary_candidates"], 0)
        delta_cases = {case["case_id"]: case for case in delta["cases"]}
        deleted_provider = delta_cases["provider_module_removed"]
        self.assertEqual(deleted_provider["added_candidates"], ["tests/test_api.py"])
        self.assertEqual(deleted_provider["added_abstained_expected_candidates"], ["tests/test_api.py"])
        unused_import = delta_cases["unused_import_negative"]
        self.assertEqual(unused_import["added_candidates"], [])

    def test_fallback_paired_import_forms_separate_candidate_lift_from_actionability(self):
        cases = fallback_import_form_cases()
        comparison = compare_import_use_fallback(cases=cases)
        wrapper_report = compare_fallback_import_forms()
        self.assertEqual(comparison, wrapper_report)
        baseline = comparison["baseline"]
        enabled = comparison["fallback_enabled"]
        delta = comparison["fallback_delta"]

        self.assertEqual(baseline["counts"]["recalled_test_files"], 0)
        self.assertEqual(enabled["counts"]["recalled_test_files"], 3)
        self.assertEqual(enabled["counts"]["actionable_recalled_test_files"], 0)
        self.assertEqual(enabled["counts"]["unnecessary_test_files"], 0)
        self.assertEqual(delta["counts"]["added_candidates"], 3)
        self.assertEqual(delta["counts"]["added_expected_candidates"], 3)
        self.assertEqual(delta["counts"]["added_actionable_expected_candidates"], 0)
        self.assertEqual(delta["counts"]["added_abstained_expected_candidates"], 3)
        self.assertEqual(delta["counts"]["added_unnecessary_candidates"], 0)
        deltas = {case["case_id"]: case for case in delta["cases"]}
        self.assertEqual(deltas["deleted_symbol_alias_used"]["added_expected_candidates"], ["tests/test_deleted_symbol.py"])
        self.assertEqual(deltas["deleted_provider_module_used"]["added_expected_candidates"], ["tests/test_deleted_module.py"])
        self.assertEqual(deltas["relative_provider_removed_used"]["added_expected_candidates"], ["app/tests/test_relative.py"])
        self.assertEqual(deltas["deleted_symbol_alias_unused"]["added_candidates"], [])
        self.assertEqual(deltas["deleted_provider_module_unused"]["added_candidates"], [])

    def test_scope_hop_limit_is_validated(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 2"):
            run_dependency_impact_benchmark(max_hops=3)

    def test_bidirectional_wave_comparison_reports_precision_recall_and_abstention_tradeoff(self):
        report = compare_bidirectional_wave_selection()
        second = compare_bidirectional_wave_selection()

        self.assertEqual(report, second)
        self.assertEqual(report["case_count"], 16)
        baseline = report["metrics"]["existing_scope"]
        wave = report["metrics"]["wave_meeting"]
        actionable = report["metrics"]["actionable_wave_meeting"]
        self.assertEqual(baseline["selected_test_files"], 12)
        self.assertEqual(baseline["recalled_test_files"], 11)
        self.assertAlmostEqual(baseline["affected_test_recall"], 11 / 13)
        self.assertEqual(wave["selected_test_files"], 8)
        self.assertEqual(wave["recalled_test_files"], 8)
        self.assertEqual(wave["selected_test_precision"], 1.0)
        self.assertAlmostEqual(wave["affected_test_recall"], 8 / 13)
        self.assertEqual(actionable["selected_test_files"], 5)
        self.assertEqual(actionable["recalled_test_files"], 5)
        self.assertAlmostEqual(actionable["affected_test_recall"], 5 / 13)
        self.assertEqual(report["counts"]["candidate_classifications"]["connected_with_boundary_evidence"], 3)
        self.assertIn("no_static_meeting_with_boundary", report["counts"]["candidate_classifications"])
        cases = {case["case_id"]: case for case in report["cases"]}
        self.assertEqual(cases["neighbor_scope_extra_test"]["wave_meeting_test_files"], ["tests/test_api.py"])
        self.assertEqual(cases["provider_module_removed"]["candidate_classifications"]["tests/test_api.py"], "no_static_meeting_with_boundary")
        ranking = report["ranking"]
        self.assertEqual(ranking["candidate_count"], 12)
        self.assertEqual(len({item["candidate_id"] for item in ranking["wave_priority_order"]}), 12)
        budgets = {item["fraction"]: item for item in ranking["budgets"]}
        self.assertEqual(budgets[0.25]["wave_priority_order"]["recalled_affected_tests"], 3)
        self.assertEqual(budgets[0.5]["wave_priority_order"]["recalled_affected_tests"], 6)
        self.assertEqual(budgets[0.75]["wave_priority_order"]["recalled_affected_tests"], 8)
        self.assertAlmostEqual(
            budgets[0.75]["random_order_expectation"]["expected_recalled_affected_tests"],
            9 * (11 / 12),
        )
        self.assertLess(
            budgets[0.75]["wave_priority_order"]["recalled_affected_tests"],
            budgets[0.75]["random_order_expectation"]["expected_recalled_affected_tests"],
        )

    def test_evaluation_fixture_inventory_is_separate_from_development_cases(self):
        evaluation_cases = _wave_holdout_cases()
        development_ids = {case["case_id"] for case in run_dependency_impact_benchmark()["cases"]}

        self.assertEqual(len(evaluation_cases), 5)
        self.assertEqual({case["split"] for case in evaluation_cases}, {"evaluation"})
        self.assertFalse(development_ids & {case["case_id"] for case in evaluation_cases})


if __name__ == "__main__":
    unittest.main()