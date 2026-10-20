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

    def test_duplicate_definition_records_partial_file_uncertainty(self):
        delta = build_python_ast_delta(
            {},
            {"pkg/core.py": b"def run():\n    return 1\ndef run():\n    return 2\n"},
        )

        self.assertEqual(delta["symbols"]["added"], [])
        self.assertEqual(delta["uncertain_files"], ["pkg/core.py"])
        self.assertEqual(delta["partially_uncertain_files"], ["pkg/core.py"])
        self.assertEqual(delta["uncertain_symbols"], ["function:pkg/core.py:run"])
        self.assertEqual(delta["issues"][0]["kind"], "duplicate_symbol_id")

    def test_duplicate_symbols_do_not_discard_unique_changes(self):
        before = b"def duplicate():\n    pass\ndef duplicate():\n    pass\ndef keep():\n    return 1\ndef removed():\n    pass\n"
        after = b"def duplicate():\n    return 2\ndef duplicate():\n    return 3\ndef keep():\n    return 2\ndef added():\n    pass\n"
        delta = build_python_ast_delta({"core.py": before}, {"core.py": after})
        self.assertEqual([row["symbol_id"] for row in delta["symbols"]["changed"]], ["function:core.py:keep"])
        self.assertEqual([row["symbol_id"] for row in delta["symbols"]["added"]], ["function:core.py:added"])
        self.assertEqual([row["symbol_id"] for row in delta["symbols"]["removed"]], ["function:core.py:removed"])
        self.assertEqual(delta["uncertain_symbols"], ["function:core.py:duplicate"])
        self.assertEqual(delta, build_python_ast_delta({"core.py": before}, {"core.py": after}))

    def test_one_sided_duplicates_do_not_create_false_addition_or_removal(self):
        unique = b"def run():\n    return 1\n"
        duplicate = unique + b"def run():\n    return 2\n"
        for before, after in ((unique, duplicate), (duplicate, unique), (b"", duplicate), (duplicate, b"")):
            with self.subTest(before=before, after=after):
                delta = build_python_ast_delta({"core.py": before}, {"core.py": after})
                self.assertEqual(delta["symbols"], {"added": [], "changed": [], "removed": []})

    def test_duplicate_parent_suppresses_descendants_but_not_siblings(self):
        unique = b"class Group:\n    def child(self):\n        return 1\nclass Other:\n    def keep(self):\n        return 1\n"
        duplicate = unique.replace(b"return 1", b"return 2") + b"class Group:\n    pass\n"
        for before, after in ((unique, duplicate), (duplicate, unique)):
            delta = build_python_ast_delta({"core.py": before}, {"core.py": after})
            changes = {row["symbol_id"] for row in delta["symbols"]["changed"]}
            self.assertEqual(changes, {"class:core.py:Other", "function:core.py:Other.keep"})
            self.assertEqual(delta["symbols"]["added"], [])
            self.assertEqual(delta["symbols"]["removed"], [])
            self.assertIn("function:core.py:Group.child", delta["uncertain_symbols"])

    def test_parse_error_on_other_side_keeps_full_file_abstention(self):
        delta = build_python_ast_delta(
            {"core.py": b"def run():\n    pass\ndef run():\n    pass\n"},
            {"core.py": b"def broken(:\n"},
        )
        self.assertEqual(delta["symbols"], {"added": [], "changed": [], "removed": []})
        self.assertEqual(delta["uncertain_files"], ["core.py"])
        self.assertEqual(delta["partially_uncertain_files"], [])

    def test_property_overloads_and_nested_duplicates_keep_unrelated_symbols(self):
        before = b"class Box:\n    @property\n    def value(self):\n        return 1\n    @value.setter\n    def value(self, value):\n        pass\n    def keep(self):\n        return 1\n"
        after = before.replace(b"def keep(self):\n        return 1", b"def keep(self):\n        return 2")
        delta = build_python_ast_delta({"core.py": before}, {"core.py": after})
        self.assertEqual(
            {row["symbol_id"] for row in delta["symbols"]["changed"]},
            {"class:core.py:Box", "function:core.py:Box.keep"},
        )
        self.assertEqual(delta["uncertain_symbols"], ["function:core.py:Box.value"])

    def test_ast_budgets_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "symbol budget"):
            build_python_ast_delta(
                {},
                {"pkg/core.py": b"def one():\n    return 1\ndef two():\n    return 2\n"},
                max_symbols_per_file=1,
            )


if __name__ == "__main__":
    unittest.main()