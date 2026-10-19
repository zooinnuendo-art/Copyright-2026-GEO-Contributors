import unittest

from tools.evaluate_geo_structural_depth_stress import (
    STRUCTURAL_SEEDS,
    build_structural_snapshot,
)


class GeoStructuralDepthStressFixtureTests(unittest.TestCase):
    def test_seeded_topology_recipes_are_reproducible_and_cover_structural_cases(self):
        sources, labels = build_structural_snapshot(STRUCTURAL_SEEDS[0])
        self.assertEqual((sources, labels), build_structural_snapshot(STRUCTURAL_SEEDS[0]))
        cases = {label["case_id"]: label for label in labels}

        self.assertEqual(set(cases), {
            "direct_positive",
            "diamond_two_branch",
            "cycle_with_exit",
            "class_method_path",
            "computed_dispatch_boundary",
            "beyond_depth_boundary",
            "unrelated_production_helper_negative",
            "unused_import_negative",
        })
        self.assertGreater(cases["beyond_depth_boundary"]["known_call_hops"], 8)
        self.assertFalse(cases["unrelated_production_helper_negative"]["expected_affected"])
        self.assertFalse(cases["unused_import_negative"]["expected_affected"])

    def test_seed_schedule_is_fixed_and_distinct(self):
        self.assertEqual(len(STRUCTURAL_SEEDS), 8)
        self.assertEqual(len(set(STRUCTURAL_SEEDS)), 8)
        self.assertEqual(STRUCTURAL_SEEDS[0], 20261040)
        self.assertEqual(STRUCTURAL_SEEDS[-1], 20261047)


if __name__ == "__main__":
    unittest.main()