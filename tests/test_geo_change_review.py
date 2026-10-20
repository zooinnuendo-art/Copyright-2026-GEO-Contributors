import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.geo_change_review import _text_report, review_git_change
from tools.git_temporal_history import GitHistoryError


class ChangeReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "GEO Test")
        self.git("config", "user.email", "geo-test@example.invalid")
        self.write("src/pkg/core.py", "def run():\n    return 1\n")
        self.write("tests/test_core.py", "from pkg.core import run\ndef test_run():\n    assert run() == 1\n")
        self.first = self.commit()
        self.write("src/pkg/core.py", "def run():\n    return 2\n")
        self.second = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture change")
        return self.git("rev-parse", "HEAD")

    def review(self, revision=None, **kwargs):
        return review_git_change(self.root, revision or self.second, source_roots=["src"], **kwargs)

    def test_end_to_end_fixed_snapshot_and_dirty_worktree_preserved(self):
        self.write("src/pkg/core.py", "def staged():\n    pass\n")
        self.git("add", ".")
        self.write("src/pkg/core.py", "broken(:\n")
        self.write("untracked.py", "raise RuntimeError('not executed')\n")
        before = self.git("status", "--porcelain")
        report = self.review()
        self.assertEqual(report["source"]["commit_id"], self.second)
        self.assertEqual(report["source"]["comparison_parent_id"], self.first)
        self.assertFalse(report["source"]["working_tree_used"])
        self.assertEqual(report["changed_production_files"], ["src/pkg/core.py"])
        row = report["review"]["candidate_tests"][0]
        self.assertEqual(row["test_file"], "tests/test_core.py")
        self.assertEqual(row["changed_symbol_alignment"]["direct_reference_kinds"], ["changed_function_call"])
        self.assertEqual(self.review(), report)
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.second)
        text = _text_report(report)
        self.assertIn("changed_function_call", text)
        self.assertIn("not runtime impact proof", text)

    def test_no_production_changes_is_explicit_not_empty_success_claim(self):
        for path in ("README.txt", "tests/test_core.py"):
            self.write(path, "# comment\n")
            revision = self.commit()
            report = self.review(revision)
            self.assertEqual(report["selection_status"], "no_production_python_changes")
            self.assertIsNone(report["review"])
            self.assertIn("candidate selection was not run", _text_report(report))

    def test_deleted_provider_retains_fallback_and_uncertainty(self):
        (self.root / "src/pkg/core.py").unlink()
        # Keep the configured src directory materialized in the committed Python snapshot.
        self.write("src/pkg/other.py", "pass\n")
        revision = self.commit()
        report = self.review(revision)
        self.assertEqual(report["changed_production_files"], ["src/pkg/core.py", "src/pkg/other.py"])
        self.assertEqual(report["status"], "incomplete/review")
        self.assertTrue(report["review"]["candidate_tests"][0]["fallback_added"])
        self.assertIn("changed_file_not_found", {issue["kind"] for issue in report["graph_summary"]["issues"]})

    def test_rename_uses_current_symbol_paths(self):
        padding = "# Stable content for Git's similarity-based rename detection.\n" * 5
        self.write("src/pkg/core.py", padding + "def run():\n    return 2\n")
        self.commit()
        self.git("mv", "src/pkg/core.py", "src/pkg/moved.py")
        self.write("src/pkg/moved.py", padding + "def run():\n    return 3\n")
        self.write("tests/test_core.py", "from pkg.moved import run\ndef test_run():\n    run()\n")
        revision = self.commit()
        report = self.review(revision)
        self.assertEqual(report["changed_production_files"], ["src/pkg/moved.py"])
        self.assertEqual(report["ast_delta"]["symbols"]["changed"][0]["symbol_id"], "function:src/pkg/moved.py:run")

    def test_root_invalid_revision_and_invalid_budgets_fail(self):
        with self.assertRaisesRegex(GitHistoryError, "first parent"):
            self.review(self.first)
        with self.assertRaises(GitHistoryError):
            self.review("nonexistent")
        for kwargs in (
            {"extra_call_hops": -1}, {"max_extra_visits": 0},
            {"max_files": 0}, {"timeout_seconds": float("nan")},
            {"max_edges": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.review(**kwargs)
        with self.assertRaisesRegex(GitHistoryError, "file count"):
            self.review(max_files=1)

    def test_parse_and_graph_budget_incompleteness_are_reported(self):
        self.write("src/pkg/core.py", "broken(:\n")
        revision = self.commit()
        report = self.review(revision)
        self.assertEqual(report["status"], "incomplete/review")
        self.assertIn("src/pkg/core.py", report["ast_delta"]["uncertain_files"])
        self.assertEqual(report["ast_delta"]["symbols"]["removed"], [])
        limited = self.review(max_nodes=1)
        self.assertEqual(limited["status"], "incomplete/review")
        self.assertTrue(limited["graph_summary"]["incomplete_causes"]["global"])

    def test_snapshot_mismatch_fails_explicitly(self):
        from tools.git_temporal_history import _dependency_snapshot

        def altered(*args, **kwargs):
            graph, *rest = _dependency_snapshot(*args, **kwargs)
            graph["edges"] = []
            return graph, *rest

        with patch("tools.geo_change_review._dependency_snapshot", side_effect=altered):
            with self.assertRaisesRegex(GitHistoryError, "differs"):
                self.review()

    def test_cli_json_text_and_errors(self):
        command = [
            sys.executable, "-m", "tools.geo_change_review", str(self.root),
            "--revision", self.second, "--source-root", "src",
        ]
        result = subprocess.run(command + ["--format", "json"], check=True, capture_output=True, text=True)
        report = json.loads(result.stdout)
        self.assertEqual(report, self.review())
        text = subprocess.run(command, check=True, capture_output=True, text=True)
        self.assertIn("Candidate tests (not ranked)", text.stdout)
        for options in (["--source-root", "missing"], ["--max-files", "0"], ["--timeout-seconds", "nan"]):
            failed = subprocess.run(command + options, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stdout, "")
            self.assertIn("error:", failed.stderr)
