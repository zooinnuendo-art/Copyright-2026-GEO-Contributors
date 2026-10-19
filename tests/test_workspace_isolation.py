import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceIsolationTests(unittest.TestCase):
    def test_project_sources_and_experiments_are_not_symlinks(self):
        for directory in ("tools", "tests", "experiments"):
            root = ROOT / directory
            self.assertFalse(root.is_symlink(), str(root))
            for path in root.rglob("*"):
                self.assertFalse(path.is_symlink(), str(path))

    def test_workspace_targets_its_own_directory(self):
        workspace = json.loads((ROOT / "GEO.code-workspace").read_text(encoding="utf-8"))
        self.assertEqual(workspace["folders"], [{"name": "GEO", "path": "."}])

    def test_tools_import_without_ain_or_external_python_paths(self):
        script = """
import importlib
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
for path in sorted((root / "tools").glob("*.py")):
    module = importlib.import_module(f"tools.{path.stem}")
    assert Path(module.__file__).resolve().is_relative_to(root), module.__file__
assert not any(name == "ain" or name.startswith("ain.") for name in sys.modules)
"""
        with tempfile.TemporaryDirectory(prefix="geo-isolation-") as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-c", script, str(ROOT)],
                cwd=directory,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
