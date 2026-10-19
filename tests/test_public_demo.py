import unittest

from examples.run_demo import FIXTURE, run_demo


class PublicDemoTests(unittest.TestCase):
    def test_demo_reports_test_candidate_rename_and_disconnected_cochange_without_mutation(self):
        before = {
            path.relative_to(FIXTURE): path.read_bytes()
            for path in FIXTURE.rglob("*") if path.is_file() and "__pycache__" not in path.parts
        }
        result = run_demo()
        self.assertIn("tests/test_checkout.py", result["snapshot"]["candidate_tests"])
        self.assertEqual(result["detected_renames"], [{
            "status": "R", "old_path": "shop/pricing.py", "path": "shop/rates.py",
            "similarity_percent": 100, "content_changed": False,
        }])
        pair = next(
            row for row in result["history_tension"]["pairs"]
            if (row["left_path"], row["right_path"]) == ("shop/policy.py", "shop/rates.py")
        )
        self.assertEqual(pair["tension"], 1.0)
        self.assertEqual(pair["structural_evidence"], "static_path_not_found")
        self.assertTrue(pair["hotspot"])
        after = {
            path.relative_to(FIXTURE): path.read_bytes()
            for path in FIXTURE.rglob("*") if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(before, after)
        self.assertFalse((FIXTURE / ".git").exists())


if __name__ == "__main__":
    unittest.main()
