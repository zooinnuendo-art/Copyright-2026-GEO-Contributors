import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.git_temporal_history import GitHistoryError, extract_git_temporal_history


class GitSourceRootTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "GEO Test")
        self.git("config", "user.email", "geo-test@example.invalid")
        self.write("src/pkg/core.py", "def run():\n    return 1\n")
        self.write("tests/test_core.py", "from pkg.core import run\ndef test_run():\n    run()\n")
        self.first = self.commit()
        self.git("mv", "src/pkg/core.py", "src/pkg/moved.py")
        self.write("tests/test_core.py", "from pkg.moved import run\ndef test_run():\n    run()\n")
        self.second = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture revision")
        return self.git("rev-parse", "HEAD")

    def test_baseline_and_renamed_snapshot_share_roots_without_worktree_use(self):
        self.write("src/pkg/moved.py", "def staged():\n    return 2\n")
        self.git("add", "src/pkg/moved.py")
        self.write("src/pkg/moved.py", "broken(:\n")
        self.write("untracked.py", "raise RuntimeError('must not run')\n")
        before = self.git("status", "--porcelain")
        plain = extract_git_temporal_history(self.root, [self.second])
        history = extract_git_temporal_history(
            self.root, [self.second], source_roots=["src", "src/"],
            include_final_snapshot=True,
        )
        snapshot = history["final_snapshot"]
        self.assertEqual(snapshot["source_roots"], ["src"])
        self.assertEqual(history["selection"]["source_roots"], ["src"])
        record = history["records"][0]
        self.assertEqual(record["delta"]["directly_linked_tests"], ["test:tests/test_core.py:test_run"])
        self.assertEqual(plain["records"][0]["delta"]["directly_linked_tests"], [])
        self.assertTrue(any(change["status"] == "R" for change in record["file_changes"]))
        self.assertTrue(record["delta"]["renamed_nodes"])
        self.assertEqual(
            record["ast_delta"]["rename_aware"]["symbols"]["added"], [],
        )
        edges = {(edge["source"], edge["target"]) for edge in snapshot["edges"] if edge["type"] == "calls"}
        self.assertIn(("test:tests/test_core.py:test_run", "function:src/pkg/moved.py:run"), edges)
        # A root revision has no parent; it must still receive the same configuration.
        initial = extract_git_temporal_history(
            self.root, [self.first], source_roots=["src"], include_final_snapshot=True,
        )
        self.assertTrue(any(
            edge["target"] == "function:src/pkg/core.py:run" and edge["type"] == "calls"
            for edge in initial["final_snapshot"]["edges"]
        ))
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.second)

    def test_missing_root_in_parent_is_explicit_error(self):
        self.git("mv", "src", "lib")
        third = self.commit()
        with self.assertRaisesRegex(GitHistoryError, f"snapshot {self.second}: source root"):
            extract_git_temporal_history(self.root, [third], source_roots=["lib"])

    def test_cli_provenance_and_invalid_usage(self):
        for module, args in (
            ("tools.git_temporal_history", [str(self.root), self.second]),
            ("tools.geo_history_tension_probe", ["--repository", str(self.root), "--revisions", self.second, "--format", "json"]),
        ):
            command = [sys.executable, "-m", module, *args, "--source-root", "src"]
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
            report = json.loads(completed.stdout)
            provenance = report["selection"] if module.endswith("git_temporal_history") else report["source"]
            self.assertEqual(provenance["source_roots"], ["src"])
            command[-1] = "missing"
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(failed.stdout, "")
            self.assertIn("snapshot", failed.stderr)
        failed = subprocess.run(
            [sys.executable, "-m", "tools.geo_history_tension_probe",
             "--graph", "graph.json", "--history", "history.json", "--source-root", "src"],
            capture_output=True, text=True,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("--source-root requires --repository", failed.stderr)
