from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from tools.git_temporal_history import GitHistoryError, extract_git_temporal_history
from tools.geo_history_tension_probe import analyze_history_structural_tension
from tools.code_dependency_graph import build_dependency_graph


class GitTemporalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name)
        self._git("init", "-q")
        self._git("config", "user.name", "GEO Test")
        self._git("config", "user.email", "geo-test@example.invalid")
        self._write("pkg/core.py", "def run():\n    return 1\n")
        self._write("tests/test_core.py", "from pkg.core import run\ndef test_run():\n    run()\n")
        self.commit_one = self._commit("initial")
        self._write("pkg/core.py", "def run_new():\n    return 1\n")
        self._write("tests/test_core.py", "from pkg.core import run_new\ndef test_run():\n    run_new()\n")
        self.commit_two = self._commit("rename function")
        self._write("pkg/helpers.py", "def helper():\n    return 2\n")
        self.commit_three = self._commit("add helper")
        self._write("pkg/core.py", "def broken(:\n    return 3\n")
        self.commit_four = self._commit("introduce syntax error")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _write(self, relative_path, content):
        path = self.repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._git("add", relative_path)

    def _commit(self, message):
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def test_reads_contiguous_history_and_tracks_graph_delta_without_mutating_repo(self):
        initial_head = self._git("rev-parse", "HEAD")
        initial_status = self._git("status", "--porcelain")

        history = extract_git_temporal_history(
            self.repository,
            (self.commit_two, self.commit_three),
        )

        self.assertEqual(history["selection"]["baseline_revision_id"], self.commit_one)
        self.assertEqual([record["revision"]["id"] for record in history["records"]], [self.commit_two, self.commit_three])
        first, second = history["records"]
        self.assertEqual(first["revision"]["sequence_index"], 1)
        self.assertEqual(first["revision"]["comparison_parent_id"], self.commit_one)
        self.assertEqual(first["delta"]["directly_linked_tests"], ["test:tests/test_core.py:test_run"])
        self.assertEqual(
            [item["symbol_id"] for item in first["ast_delta"]["symbols"]["added"]],
            ["function:pkg/core.py:run_new"],
        )
        self.assertEqual(
            [item["symbol_id"] for item in first["ast_delta"]["symbols"]["removed"]],
            ["function:pkg/core.py:run"],
        )
        self.assertEqual(
            [item["symbol_id"] for item in first["ast_delta"]["symbols"]["changed"]],
            ["test:tests/test_core.py:test_run"],
        )
        self.assertEqual(second["revision"]["sequence_index"], 2)
        self.assertIn("pkg/helpers.py", {change.get("after", change).get("path") for change in second["delta"]["nodes"]["added"]})
        self.assertEqual(
            [item["symbol_id"] for item in second["ast_delta"]["symbols"]["added"]],
            ["function:pkg/helpers.py:helper"],
        )
        self.assertEqual(self._git("rev-parse", "HEAD"), initial_head)
        self.assertEqual(self._git("status", "--porcelain"), initial_status)

    def test_syntax_error_revision_does_not_report_false_symbol_removals(self):
        history = extract_git_temporal_history(self.repository, (self.commit_three, self.commit_four))
        delta = history["records"][1]["ast_delta"]

        core_symbol_changes = [
            symbol
            for category in ("added", "removed", "changed")
            for symbol in delta["symbols"][category]
            if symbol.get("symbol_id", "").startswith(("function:pkg/core.py:", "class:pkg/core.py:", "global:pkg/core.py:"))
        ]
        self.assertEqual(core_symbol_changes, [])
        self.assertIn("pkg/core.py", delta["uncertain_files"])

    def test_replay_is_deterministic(self):
        first = extract_git_temporal_history(self.repository, (self.commit_two, self.commit_three))
        second = extract_git_temporal_history(self.repository, (self.commit_two, self.commit_three))

        self.assertEqual(first["records"], second["records"])

    def test_optional_final_snapshot_matches_selected_commit_not_head(self):
        original = extract_git_temporal_history(self.repository, (self.commit_two, self.commit_three))
        bundle = extract_git_temporal_history(
            self.repository, (self.commit_two, self.commit_three), include_final_snapshot=True,
        )
        snapshot = bundle.pop("final_snapshot")
        self.assertEqual(bundle, original)
        self.assertEqual(snapshot["root"], f'git-tree://{self._git("rev-parse", self.commit_three + "^{tree}")}')
        self.assertEqual(snapshot["status"], "snapshot_only/review_required")
        self.assertIn("function:pkg/core.py:run_new", {node["id"] for node in snapshot["nodes"]})
        self.assertNotIn("parse_error", {issue["kind"] for issue in snapshot["issues"]})

    def test_rejects_noncontiguous_series_and_budget_overruns(self):
        with self.assertRaisesRegex(GitHistoryError, "contiguous first-parent"):
            extract_git_temporal_history(self.repository, (self.commit_one, self.commit_three))
        with self.assertRaisesRegex(GitHistoryError, "file count"):
            extract_git_temporal_history(self.repository, (self.commit_one,), max_files=1)

    def test_rejects_unknown_revision(self):
        with self.assertRaises(GitHistoryError):
            extract_git_temporal_history(self.repository, ("not-a-revision",))

    def test_tracks_pure_and_edited_renames_without_replacing_raw_deltas(self):
        source = "".join(f"def helper_{index}():\n    return {index}\n\n" for index in range(20))
        self._write("pkg/core.py", "def run_new():\n    return 1\n")
        self._write("pkg/helpers.py", source)
        baseline = self._commit("prepare rename")
        self._git("mv", "pkg/helpers.py", "pkg/moved.py")
        moved = self._commit("move helper")
        self._git("mv", "pkg/moved.py", "pkg/final.py")
        self._write("pkg/final.py", source.replace("return 19", "return 99"))
        edited = self._commit("move and edit helper")
        before_head = self._git("rev-parse", "HEAD")
        before_status = self._git("status", "--porcelain")

        history = extract_git_temporal_history(self.repository, (moved, edited))
        first, second = history["records"]
        self.assertEqual(history["selection"]["baseline_revision_id"], baseline)
        self.assertEqual(first["file_changes"], [{
            "status": "R", "old_path": "pkg/helpers.py", "path": "pkg/moved.py",
            "similarity_percent": 100, "content_changed": False,
        }])
        self.assertEqual(first["ast_delta"]["rename_aware"]["symbols"], {
            "added": [], "removed": [], "changed": [],
        })
        self.assertEqual(len(first["ast_delta"]["symbols"]["added"]), 20)
        self.assertEqual(len(first["ast_delta"]["symbols"]["removed"]), 20)
        self.assertIn({
            "before_id": "function:pkg/helpers.py:helper_0",
            "after_id": "function:pkg/moved.py:helper_0",
        }, first["delta"]["renamed_nodes"])
        self.assertIn({
            "before_id": "file:pkg/helpers.py", "after_id": "file:pkg/moved.py",
        }, first["delta"]["renamed_nodes"])
        self.assertTrue(second["file_changes"][0]["content_changed"])
        self.assertGreaterEqual(second["file_changes"][0]["similarity_percent"], 50)
        symbols = second["ast_delta"]["rename_aware"]["symbols"]
        self.assertEqual(symbols["added"], [])
        self.assertEqual(symbols["removed"], [])
        self.assertEqual([item["symbol_id"] for item in symbols["changed"]], [
            "function:pkg/final.py:helper_19",
        ])
        self.assertEqual(self._git("rev-parse", "HEAD"), before_head)
        self.assertEqual(self._git("status", "--porcelain"), before_status)
        self.assertEqual(
            history["records"],
            extract_git_temporal_history(self.repository, (moved, edited))["records"],
        )

    @unittest.skipIf(os.name == "nt", "Windows filenames cannot contain newline characters")
    def test_rename_with_newline_path_is_nul_delimited(self):
        self._git("mv", "pkg/helpers.py", "pkg/new\nname.py")
        revision = self._commit("move to unusual filename")
        record = extract_git_temporal_history(self.repository, (revision,))["records"][0]
        self.assertEqual(record["file_changes"][0]["path"], "pkg/new\nname.py")
        self.assertEqual(record["file_changes"][0]["old_path"], "pkg/helpers.py")

    def test_python_extension_transition_is_addition_or_deletion(self):
        self._git("mv", "pkg/helpers.py", "pkg/helpers.txt")
        removed = self._commit("remove Python extension")
        self._git("mv", "pkg/helpers.txt", "pkg/returned.py")
        added = self._commit("restore Python extension")
        records = extract_git_temporal_history(self.repository, (removed, added))["records"]
        self.assertEqual(records[0]["file_changes"], [{"status": "D", "path": "pkg/helpers.py"}])
        self.assertEqual(records[1]["file_changes"], [{"status": "A", "path": "pkg/returned.py"}])

    def test_git_to_tension_follows_consecutive_moves_and_uses_final_graph(self):
        self._write("pkg/core.py", "def run_new():\n    return 1\n")
        self._commit("repair syntax")
        self._write("pkg/helpers.py", "def helper():\n    return 10\n")
        self._write("pkg/peer.py", "from pkg.helpers import helper\nVALUE = helper()\n")
        first_edit = self._commit("cochange before move")
        self._git("mv", "pkg/helpers.py", "pkg/middle.py")
        first_move = self._commit("first move")
        self._git("mv", "pkg/middle.py", "pkg/final.py")
        second_move = self._commit("second move")
        self._write("pkg/final.py", "def helper():\n    return 20\n")
        self._write("pkg/peer.py", "from pkg.final import helper\nVALUE = helper()\n")
        last_edit = self._commit("cochange after move")
        history = extract_git_temporal_history(
            self.repository, (first_edit, first_move, second_move, last_edit),
        )
        graph = build_dependency_graph(self.repository)
        result = analyze_history_structural_tension(graph, history)
        self.assertEqual(result["pair_count"], 1)
        pair = result["pairs"][0]
        self.assertEqual((pair["left_path"], pair["right_path"]), ("pkg/final.py", "pkg/peer.py"))
        self.assertEqual(pair["cochange_commits"], 2)
        self.assertEqual(pair["historical_jaccard_proximity"], 1.0)
        self.assertEqual(pair["structural_distance_hops"], 1)
        self.assertEqual(pair["tension"], 0.5)

    def test_empty_commit_and_root_have_explicit_file_change_metadata(self):
        root = extract_git_temporal_history(self.repository, (self.commit_one,))["records"][0]
        self.assertEqual(root["file_changes"], [
            {"status": "A", "path": "pkg/core.py"},
            {"status": "A", "path": "tests/test_core.py"},
        ])
        self._git("commit", "--allow-empty", "-q", "-m", "empty")
        empty = self._git("rev-parse", "HEAD")
        record = extract_git_temporal_history(self.repository, (empty,))["records"][0]
        self.assertEqual(record["file_changes"], [])
        self.assertNotIn("rename_aware", record["ast_delta"])

if __name__ == "__main__":
    unittest.main()