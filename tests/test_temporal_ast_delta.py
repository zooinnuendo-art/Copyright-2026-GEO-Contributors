import unittest

from tools.temporal_ast_delta import build_python_ast_delta


class TemporalAstDeltaTests(unittest.TestCase):
    def test_reports_added_removed_changed_symbols_and_signatures(self):
        parent = {
            "pkg/core.py": b"VALUE = 1\ndef keep(value: int) -> int:\n    return value + VALUE\ndef remove():\n    return None\n",
        }
        current = {
            "pkg/core.py": b"VALUE = 2\ndef keep(value: str) -> int:\n    return value + VALUE\ndef added():\n    return True\n",
        }

        delta = build_python_ast_delta(parent, current)

        self.assertEqual(
            [item["symbol_id"] for item in delta["symbols"]["added"]],
            ["function:pkg/core.py:added"],
        )
        self.assertEqual(
            [item["symbol_id"] for item in delta["symbols"]["removed"]],
            ["function:pkg/core.py:remove"],
        )
        changed_ids = {item["symbol_id"] for item in delta["symbols"]["changed"]}
        self.assertEqual(changed_ids, {"function:pkg/core.py:keep", "global:pkg/core.py:VALUE"})
        changed_function = next(item for item in delta["symbols"]["changed"] if item["symbol_id"].endswith(":keep"))
        self.assertNotEqual(changed_function["before"]["signature_sha256"], changed_function["after"]["signature_sha256"])
        self.assertFalse(delta["source_text_retained"])

    def test_comment_only_change_does_not_change_symbol_ast(self):
        delta = build_python_ast_delta(
            {"pkg/core.py": b"def run():\n    return 1\n"},
            {"pkg/core.py": b"# comment\ndef run():\n    return 1\n"},
        )

        self.assertEqual(delta["symbols"], {"added": [], "removed": [], "changed": []})

    def test_parse_error_never_becomes_false_symbol_additions_or_removals(self):
        delta = build_python_ast_delta(
            {"pkg/core.py": b"def run():\n    return 1\n"},
            {"pkg/core.py": b"def run(:\n    return 2\n"},
        )

        self.assertEqual(delta["symbols"], {"added": [], "removed": [], "changed": []})
        self.assertEqual(delta["uncertain_files"], ["pkg/core.py"])
        self.assertEqual(delta["issues"][0]["kind"], "ast_parse_error")

    def test_duplicate_definition_marks_the_whole_file_uncertain(self):
        delta = build_python_ast_delta(
            {},
            {"pkg/core.py": b"def run():\n    return 1\ndef run():\n    return 2\n"},
        )

        self.assertEqual(delta["symbols"]["added"], [])
        self.assertEqual(delta["uncertain_files"], ["pkg/core.py"])
        self.assertEqual(delta["issues"][0]["kind"], "duplicate_symbol_id")

    def test_ast_budgets_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "symbol budget"):
            build_python_ast_delta(
                {},
                {"pkg/core.py": b"def one():\n    return 1\ndef two():\n    return 2\n"},
                max_symbols_per_file=1,
            )


if __name__ == "__main__":
    unittest.main()