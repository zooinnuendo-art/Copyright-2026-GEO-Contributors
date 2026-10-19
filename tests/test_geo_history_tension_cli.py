import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.geo_history_tension_probe import analyze_history_structural_tension


ROOT = Path(__file__).resolve().parents[1]


class GeoHistoryTensionCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.graph = {
            "nodes": [], "edges": [], "status": "incomplete/review",
            "incomplete_causes": {"global": [{"kind": "node_budget_exceeded"}]},
            "issues": [{
                "kind": "unresolved_simple_call_summary", "count": 8,
                "examples": [{"path": "settings.py", "name": "injected", "line": 2}],
            }],
        }
        self.history = {"records": [{
            "revision": {"id": "r1", "comparison_parent_id": "r0"},
            "file_changes": [
                {"status": "M", "path": path} for path in ("a.py", "b.py", "settings.py")
            ],
        }]}
        self.graph_path = self.directory / "graph.json"
        self.history_path = self.directory / "history.json"
        self.graph_path.write_text(json.dumps(self.graph), encoding="utf-8")
        self.history_path.write_text(json.dumps(self.history), encoding="utf-8")

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "tools.geo_history_tension_probe",
             "--graph", str(self.graph_path), "--history", str(self.history_path), *arguments],
            cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
        )

    def run_repository_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "tools.geo_history_tension_probe", *arguments],
            cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
        )

    def test_rejects_missing_and_mixed_input_modes(self):
        for arguments in (
            (), ("--graph", str(self.graph_path)),
            ("--repository", str(self.directory)),
            ("--revisions", "HEAD"),
            ("--repository", str(self.directory), "--revisions", "HEAD", "--graph", str(self.graph_path)),
            ("--graph", str(self.graph_path), "--history", str(self.history_path), "--revisions", "HEAD"),
            ("--repository", str(self.directory), "--revisions", "HEAD"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_repository_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_json_matches_api_and_is_not_truncated_by_top(self):
        result = self.run_cli("--format", "json", "--top", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            analyze_history_structural_tension(self.graph, self.history),
        )
        self.assertEqual(len(json.loads(result.stdout)["pairs"]), 3)
        self.assertEqual(result.stderr, "")

    def test_text_shows_ranking_context_filters_and_uncertainty(self):
        result = self.run_cli("--exclude", "settings.py", "--exclude", "absent/*")
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in (
            "Pairs: 1; hotspots: 1", '[hotspot] "a.py" <-> "b.py"',
            "tension=1.000000", "evidence=static_path_not_found",
            "change frequency=1.000000/1.000000", "static degree=0/0",
            'Excluded: "settings.py"', "file/revision occurrences: 1",
            "incomplete/review", "Unresolved call sites reported: 8",
            "node_budget_exceeded", "injected",
            "not attributed to pairs", "does not mean no runtime dependency",
        ):
            self.assertIn(text, result.stdout)

    def test_text_top_limits_only_display_and_thresholds_are_wired(self):
        result = self.run_cli("--top", "1", "--minimum-cochange-commits", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("showing 1 of 3", result.stdout)
        self.assertIn("hotspots: 0", result.stdout)
        self.assertEqual(result.stdout.count("[candidate]"), 1)

    def test_all_excluded_is_success_but_not_a_safety_verdict(self):
        result = self.run_cli("--exclude", "*")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No candidate pairs", result.stdout)
        self.assertIn("not a safety verdict", result.stdout)
        self.assertIn("Excluded paths: 3", result.stdout)

    def test_invalid_inputs_fail_without_success_output(self):
        for arguments in (
            ("--graph", str(self.directory / "missing.json")),
            ("--top", "0"), ("--max-input-bytes", "1"), ("--max-pairs", "1"),
            ("--max-history-records", "0"), ("--max-paths-per-revision", "1"),
            ("--max-unique-paths", "1"), ("--hotspot-threshold", "nan"),
            ("--minimum-cochange-commits", "0"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_json_documents_are_strict_and_must_be_objects(self):
        for raw in (b"{bad", b"[]", b'{"x":NaN}', b'{"nodes":[],"nodes":[]}', b"\xff"):
            with self.subTest(raw=raw):
                self.graph_path.write_bytes(raw)
                result = self.run_cli()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_extractor_cli_pipeline_uses_a_real_git_history_and_final_snapshot(self):
        repository = self.directory / "repository"
        repository.mkdir()

        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(repository), *arguments],
                capture_output=True, text=True, check=True, timeout=30,
            ).stdout.strip()

        def commit(message):
            git("add", ".")
            git("commit", "-q", "-m", message)
            return git("rev-parse", "HEAD")

        git("init", "-q")
        git("config", "user.name", "GEO CLI Test")
        git("config", "user.email", "geo-test@example.invalid")
        (repository / "old.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        (repository / "peer.py").write_text("VALUE = 1\n", encoding="utf-8")
        commit("baseline")
        (repository / "old.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
        (repository / "peer.py").write_text("VALUE = 2\n", encoding="utf-8")
        edited = commit("cochange")
        git("mv", "old.py", "new.py")
        moved = commit("move")
        before = git("status", "--porcelain")
        for module, arguments, output in (
            ("tools.git_temporal_history", [str(repository), edited, moved], self.history_path),
            ("tools.code_dependency_graph", [str(repository)], self.graph_path),
        ):
            result = subprocess.run(
                [sys.executable, "-m", module, *arguments], cwd=ROOT,
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output.write_text(result.stdout, encoding="utf-8")
        result = self.run_cli("--format", "json", "--hotspot-threshold", "0.75")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["pair_count"], 1)
        pair = report["pairs"][0]
        self.assertEqual((pair["left_path"], pair["right_path"]), ("new.py", "peer.py"))
        self.assertEqual(pair["cochange_commits"], 1)
        self.assertTrue(pair["hotspot"])
        self.assertEqual(report["graph_uncertainty"]["unresolved_calls"]["reported_count"], 0)
        self.assertEqual(git("rev-parse", "HEAD"), moved)
        self.assertEqual(git("status", "--porcelain"), before)
        tree = git("rev-parse", moved + "^{tree}")
        (repository / "peer.py").write_text("from new import helper\nVALUE = helper()\n", encoding="utf-8")
        newer = commit("later dependency must not affect old snapshot")
        (repository / "new.py").write_text("def broken(:\n", encoding="utf-8")
        (repository / "untracked.py").write_text("def broken(:\n", encoding="utf-8")
        # Include both staged and unstaged edits when checking read-only behavior.
        (repository / "peer.py").write_text("STAGED = 1\n", encoding="utf-8")
        git("add", "peer.py")
        (repository / "peer.py").write_text("UNSTAGED = 1\n", encoding="utf-8")
        dirty_status = git("status", "--porcelain")
        index_diff = git("diff", "--cached")
        working_diff = git("diff")
        mode = ("--repository", str(repository), "--revisions", edited, moved)
        combined = self.run_repository_cli(*mode, "--format", "json")
        self.assertEqual(combined.returncode, 0, combined.stderr)
        combined_report = json.loads(combined.stdout)
        source = combined_report.pop("source")
        self.assertEqual(combined_report, report)
        self.assertEqual(source["final_commit_id"], moved)
        self.assertEqual(source["final_tree_id"], tree)
        self.assertEqual(source["graph_root"], f"git-tree://{tree}")
        self.assertEqual(source["revision_ids"], [edited, moved])
        self.assertEqual(source["snapshot_alignment"], "same_extraction")
        self.assertFalse(source["working_tree_used"])
        text = self.run_repository_cli(*mode, "--exclude", "peer.py")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Git source:", text.stdout)
        self.assertIn(moved, text.stdout)
        self.assertIn("Pairs: 0; hotspots: 0", text.stdout)
        self.assertEqual(git("rev-parse", "HEAD"), newer)
        self.assertEqual(git("status", "--porcelain"), dirty_status)
        self.assertEqual(git("diff", "--cached"), index_diff)
        self.assertEqual(git("diff"), working_diff)
        self.assertEqual((repository / "new.py").read_text(), "def broken(:\n")
        self.assertTrue((repository / "untracked.py").exists())
        for arguments in (
            ("--max-revisions", "1"), ("--max-history-records", "1"),
            ("--max-files", "1"), ("--timeout-seconds", "nan"),
            ("--timeout-seconds", "0"), ("--max-total-bytes", "1"),
            ("--max-metadata-bytes", "1"),
        ):
            with self.subTest(arguments=arguments):
                failed = self.run_repository_cli(*mode, *arguments)
                self.assertEqual(failed.returncode, 2)
                self.assertEqual(failed.stdout, "")
                self.assertIn("error:", failed.stderr)
                self.assertNotIn("Traceback", failed.stderr)
        noncontiguous = self.run_repository_cli(
            "--repository", str(repository), "--revisions", edited, newer,
        )
        self.assertEqual(noncontiguous.returncode, 2)
        self.assertIn("contiguous first-parent", noncontiguous.stderr)


if __name__ == "__main__":
    unittest.main()
