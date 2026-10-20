from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.geo_change_review import _index_entries
from tools.git_temporal_history import (
    GitHistoryError,
    _dependency_snapshot,
    _tree_python_blobs,
    _validate_git_python_path,
    _validate_git_python_paths,
)


class GitPathSafetyTests(unittest.TestCase):
    def test_accepts_portable_relative_unicode_paths(self):
        self.assertEqual(_validate_git_python_path("src/パッケージ/core.py").as_posix(), "src/パッケージ/core.py")

    def test_rejects_posix_and_windows_path_escape_forms(self):
        for path in (
            "", "../outside.py", "pkg/../../outside.py", "/outside.py",
            r"..\outside.py", r"pkg\..\outside.py", r"C:\outside.py",
            "C:/outside.py", "//server/share.py", "pkg/name.py:stream",
            "pkg/CON.py", "pkg/nul.txt.py", "pkg/name.py.",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(GitHistoryError, "unsafe Python path"):
                _validate_git_python_path(path)

    def test_rejects_case_insensitive_path_collisions_and_file_directory_aliases(self):
        for paths in (
            ["src/Thing.py", "src/thing.py"],
            ["src/Thing.py", "src/thing.PY"],
            ["src/pkg.py", "src/pkg.py/child.py"],
        ):
            with self.subTest(paths=paths), self.assertRaisesRegex(GitHistoryError, "collide"):
                _validate_git_python_paths(paths)

    def test_commit_tree_parser_rejects_windows_separator_before_blob_read(self):
        oid = "a" * 40
        raw = f"100644 blob {oid} 1\t..\\outside.py\0".encode()
        with patch("tools.git_temporal_history._git", return_value=raw):
            with self.assertRaisesRegex(GitHistoryError, "unsafe Python path"):
                _tree_python_blobs(
                    Path("."), "revision", max_files=10, max_total_bytes=100,
                    timeout_seconds=1, max_metadata_bytes=1000,
                )

    def test_index_parser_rejects_windows_separator(self):
        oid = "a" * 40
        raw = f"100644 {oid} 0\t..\\outside.py\0".encode()
        with patch("tools.geo_change_review._git", return_value=raw):
            with self.assertRaisesRegex(GitHistoryError, "unsafe Python path"):
                _index_entries(Path("."), 1, 1000)

    def test_snapshot_writer_revalidates_paths_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "geo-path-safety-sentinel.py"
            outside.write_text("sentinel", encoding="utf-8")
            self.addCleanup(outside.unlink, missing_ok=True)
            with patch(
                "tools.git_temporal_history._read_blobs",
                return_value=[("../geo-path-safety-sentinel.py", b"overwritten", "100644")],
            ):
                with self.assertRaisesRegex(GitHistoryError, "unsafe Python path"):
                    _dependency_snapshot(
                        root, "revision", "tree", max_files=10,
                        max_total_bytes=100, max_nodes=100, max_edges=100,
                        timeout_seconds=1, max_metadata_bytes=1000,
                        snapshot_blobs=[("../geo-path-safety-sentinel.py", "a" * 40, 11, "100644")],
                    )
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
