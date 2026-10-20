import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import test_geo_change_review as fixtures
from tools.geo_change_review import _index_entries, _text_report, review_git_change
from tools.git_temporal_history import GitHistoryError


class StagedReviewTests(unittest.TestCase):
    setUp = fixtures.ChangeReviewTests.setUp
    git = fixtures.ChangeReviewTests.git
    write = fixtures.ChangeReviewTests.write
    commit = fixtures.ChangeReviewTests.commit

    def staged(self, **kwargs):
        return review_git_change(self.root, staged=True, source_roots=["src"], **kwargs)

    def test_index_only_partial_staging_and_no_mutation(self):
        self.write("src/pkg/core.py", "def run():\n    return 3\n")
        self.git("add", "src/pkg/core.py")
        self.write("src/pkg/core.py", "def unstaged(:\n")
        self.write("src/pkg/untracked.py", "raise RuntimeError('must not execute')\n")
        index_path = Path(self.git("rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = self.root / index_path
        # Record index bytes after status, which can refresh its stat cache.
        before_status = self.git("status", "--porcelain")
        index_bytes = index_path.read_bytes()
        worktree = (self.root / "src/pkg/core.py").read_bytes()
        report = self.staged()
        self.assertEqual(index_path.read_bytes(), index_bytes)
        self.assertEqual((self.root / "src/pkg/core.py").read_bytes(), worktree)
        self.assertEqual(self.git("status", "--porcelain"), before_status)
        self.assertEqual(report["source"]["comparison_parent_id"], self.second)
        self.assertIsNone(report["source"]["commit_id"])
        self.assertFalse(report["source"]["working_tree_used"])
        self.assertEqual(report["changed_production_files"], ["src/pkg/core.py"])
        self.assertEqual(report["ast_delta"]["uncertain_files"], [])
        self.assertEqual(report["review"]["candidate_tests"][0]["changed_symbol_alignment"]["direct_reference_kinds"], ["changed_function_call"])
        self.assertIn("index versus HEAD", _text_report(report))

    def test_empty_and_test_only_index(self):
        self.write("src/pkg/core.py", "def run():\n    return 9\n")
        self.assertEqual(self.staged()["selection_status"], "no_production_python_changes")
        self.write("tests/test_core.py", "# staged test only\n")
        self.git("add", "tests/test_core.py")
        report = self.staged()
        self.assertEqual(report["changed_test_files"], ["tests/test_core.py"])
        self.assertIsNone(report["review"])

    def test_staged_rename_and_deletion(self):
        padding = "# retained lines for rename detection\n" * 5
        self.write("src/pkg/core.py", padding + "def run():\n    return 2\n")
        self.commit()
        self.git("mv", "src/pkg/core.py", "src/pkg/moved.py")
        self.write("src/pkg/moved.py", padding + "def run():\n    return 3\n")
        self.write("tests/test_core.py", "from pkg.moved import run\ndef test_run():\n    run()\n")
        self.git("add", ".")
        report = self.staged()
        self.assertEqual(report["changed_production_files"], ["src/pkg/moved.py"])
        self.assertEqual(report["ast_delta"]["symbols"]["changed"][0]["symbol_id"], "function:src/pkg/moved.py:run")
        self.git("reset", "-q", "HEAD", "--", "src/pkg/core.py", "src/pkg/moved.py", "tests/test_core.py")
        self.git("rm", "--cached", "src/pkg/core.py")
        self.write("src/pkg/other.py", "pass\n")
        self.git("add", "src/pkg/other.py")
        report = self.staged()
        self.assertTrue(report["review"]["candidate_tests"][0]["fallback_added"])

    def test_detects_concurrent_index_change(self):
        calls = 0

        def changing(*args):
            nonlocal calls
            raw, entries = _index_entries(*args)
            calls += 1
            return (raw + b"changed", entries) if calls == 2 else (raw, entries)

        with patch("tools.geo_change_review._index_entries", side_effect=changing):
            with self.assertRaisesRegex(GitHistoryError, "changed during"):
                self.staged()

    def test_unmerged_index_rejected(self):
        current_branch = self.git("branch", "--show-current")
        self.git("branch", "conflict-side")
        self.write("src/pkg/core.py", "def run():\n    return 3\n")
        self.commit()
        self.git("checkout", "conflict-side")
        self.write("src/pkg/core.py", "def run():\n    return 4\n")
        self.commit()
        self.git("checkout", current_branch)
        conflict = subprocess.run(
            ["git", "-C", str(self.root), "merge", "--no-edit", "conflict-side"],
            text=True, capture_output=True,
        )
        self.assertNotEqual(conflict.returncode, 0, conflict.stdout + conflict.stderr)
        with self.assertRaisesRegex(GitHistoryError, "unmerged"):
            self.staged()

    def test_cli_staged_and_mutual_exclusion(self):
        self.write("src/pkg/core.py", "def run():\n    return 3\n")
        self.git("add", ".")
        command = [sys.executable, "-m", "tools.geo_change_review", str(self.root), "--staged", "--source-root", "src"]
        result = subprocess.run(command + ["--format", "json"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), self.staged())
        failed = subprocess.run(command + ["--revision", self.second], capture_output=True, text=True)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        with self.assertRaises(ValueError):
            review_git_change(self.root, self.second, staged=True)

    def test_subdirectory_uses_whole_repository_index(self):
        self.write("src/pkg/core.py", "def run():\n    return 3\n")
        self.git("add", ".")
        report = review_git_change(self.root / "tests", staged=True, source_roots=["src"])
        self.assertEqual(report, self.staged())

    def test_unborn_head_rejected_and_root_head_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            with self.assertRaises(GitHistoryError):
                review_git_change(directory, staged=True)
        # HEAD can be the root commit in staged mode (unlike --revision mode).
        subprocess.run(
            ["git", "-C", str(self.root), "update-ref", "HEAD", self.first],
            check=True, capture_output=True,
        )
        report = self.staged()
        self.assertEqual(report["source"]["comparison_parent_id"], self.first)

    def test_added_file_and_budget_failure_do_not_write_objects(self):
        self.write("src/pkg/new.py", "def added():\n    return 1\n")
        self.git("add", "src/pkg/new.py")
        objects = self.git("count-objects", "-v")
        report = self.staged()
        self.assertEqual(report["changed_production_files"], ["src/pkg/new.py"])
        self.assertEqual(self.git("count-objects", "-v"), objects)
        with self.assertRaisesRegex(GitHistoryError, "file count"):
            self.staged(max_files=1)
        with self.assertRaisesRegex(GitHistoryError, "blob bytes"):
            self.staged(max_total_bytes=1)
