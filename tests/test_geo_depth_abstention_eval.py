import unittest

from tools.evaluate_geo_depth_abstention import (
    SEED,
    STRESS_SEEDS,
    build_depth_abstention_snapshot,
)


class GeoDepthAbstentionEvaluationFixtureTests(unittest.TestCase):
    def test_seeded_mixed_snapshot_fixture_is_reproducible_and_covers_depths(self):
        sources, labels = build_depth_abstention_snapshot()
        sources_again, labels_again = build_depth_abstention_snapshot(SEED)

        self.assertEqual((sources, labels), (sources_again, labels_again))
        self.assertEqual({label["known_call_hops"] for label in labels if label["known_call_hops"] is not None}, set(range(1, 13)))
        self.assertEqual(len(labels), 14)
        self.assertEqual(sum(label["expected_affected"] for label in labels), 13)
        self.assertEqual(len(sources), len(set(sources)))

    def test_fixture_labels_include_boundary_positive_and_unrelated_negative_controls(self):
        _sources, labels = build_depth_abstention_snapshot()
        by_id = {label["case_id"]: label for label in labels}

        self.assertTrue(by_id["computed_dispatch_positive_control"]["expected_affected"])
        self.assertFalse(by_id["unrelated_negative_control"]["expected_affected"])

    def test_metamorphic_seed_list_is_fixed_and_excludes_consumed_seed(self):
        self.assertEqual(len(STRESS_SEEDS), 20)
        self.assertEqual(STRESS_SEEDS, tuple(range(20261017, 20261037)))
        self.assertNotIn(SEED, STRESS_SEEDS)


if __name__ == "__main__":
    unittest.main()