"""Validate wheel-installed commands from outside the source checkout."""

import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def run(command, *arguments):
    executable = shutil.which(command)
    if executable is None:
        raise RuntimeError(f"installed command not found: {command}")
    return subprocess.run(
        [executable, *arguments], check=True, capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    ).stdout


def check_change_review(root):
    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(root), *arguments], check=True, capture_output=True,
            text=True, encoding="utf-8", timeout=30,
        ).stdout.strip()

    def review(*arguments):
        return json.loads(subprocess.run(
            [sys.executable, "-I", "-m", "tools.geo_change_review", str(root),
             "--source-root", "src", "--format", "json", *arguments],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=60,
        ).stdout)

    def check_report(report):
        assert report["changed_production_files"] == ["src/pkg/core.py"], report
        candidate = report["review"]["candidate_tests"][0]
        assert candidate["test_file"] == "tests/test_core.py", candidate
        assert candidate["changed_symbol_alignment"]["direct_reference_kinds"] == ["changed_function_call"], candidate
        assert not report["source"]["working_tree_used"], report
        assert not report["source"]["target_code_executed"], report

    git("init", "-q")
    git("config", "user.name", "GEO Wheel Check")
    git("config", "user.email", "geo-wheel-check@example.invalid")
    # Repository-local settings keep fixture commits independent of user signing/hooks.
    git("config", "commit.gpgsign", "false")
    hooks = root / "empty-hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", str(hooks))
    (root / "src/pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    provider = root / "src/pkg/core.py"
    provider.write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "tests/test_core.py").write_text(
        "from pkg.core import run\ndef test_run():\n    assert run() == 1\n",
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-q", "-m", "initial fixture")
    provider.write_text("def run():\n    return 2\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "change provider")
    commit = git("rev-parse", "HEAD")
    committed = review("--revision", commit)
    check_report(committed)
    provider.write_text("def run():\n    return 3\n", encoding="utf-8")
    git("add", "src/pkg/core.py")
    provider.write_text("def broken(:\n", encoding="utf-8")
    untracked = root / "src/pkg/untracked.py"
    untracked.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    status = git("status", "--porcelain")
    index = Path(git("rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = root / index
    index_bytes = index.read_bytes()
    worktree_bytes = provider.read_bytes()
    object_counts = git("count-objects", "-v")
    staged = review("--staged")
    check_report(staged)
    assert staged["analysis_kind"] == "experimental_staged_change_review", staged
    assert staged["source"]["comparison_parent_id"] == commit, staged
    assert not staged["ast_delta"]["uncertain_files"], staged
    assert index.read_bytes() == index_bytes
    assert provider.read_bytes() == worktree_bytes
    assert untracked.read_text(encoding="utf-8") == "raise RuntimeError('must not execute')\n"
    assert git("count-objects", "-v") == object_counts
    assert git("status", "--porcelain") == status
    assert git("rev-parse", "HEAD") == commit
    assert review("--revision", commit) == committed


def main():
    if Path.cwd().resolve().is_relative_to(ROOT):
        raise RuntimeError("run this check outside the source checkout")
    # Isolated Python cannot resolve the checkout through cwd or PYTHONPATH.
    output = subprocess.run(
        [sys.executable, "-I", "-c", "import tools; print(tools.__file__)"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    module_path = Path(output.strip()).resolve()
    if module_path.is_relative_to(ROOT):
        raise RuntimeError(f"expected a wheel install, found source: {module_path}")
    distribution = importlib.metadata.distribution("geo-code-analysis")
    if distribution.requires:
        raise RuntimeError(f"unexpected runtime dependencies: {distribution.requires}")
    if distribution.metadata["License-Expression"] != "Apache-2.0":
        raise RuntimeError("missing Apache-2.0 package metadata")
    files = distribution.files or []
    for name in ("LICENSE", "NOTICE"):
        if not any(str(path).replace("\\", "/").endswith(f"/licenses/{name}") for path in files):
            raise RuntimeError(f"{name} is missing from the installed distribution")
    for command in ("geo-graph", "geo-history", "geo-tension"):
        if "usage:" not in run(command, "--help"):
            raise RuntimeError(f"missing help output: {command}")
    help_output = subprocess.run(
        [sys.executable, "-I", "-m", "tools.geo_change_review", "--help"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    assert "--staged" in help_output and "--revision" in help_output, help_output
    demo = json.loads(run("geo-demo"))
    assert demo["snapshot"]["candidate_tests"] == ["tests/test_checkout.py"], demo
    assert demo["detected_renames"][0]["similarity_percent"] == 100, demo
    pair = demo["history_tension"]["pairs"][0]
    assert (pair["left_path"], pair["right_path"], pair["tension"]) == (
        "shop/policy.py", "shop/rates.py", 1.0,
    ), pair
    with tempfile.TemporaryDirectory(prefix="geo-installed-") as directory:
        root = Path(directory)
        (root / "provider.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (root / "test_provider.py").write_text(
            "from provider import value\ndef test_value():\n    assert value() == 1\n",
            encoding="utf-8",
        )
        graph = json.loads(run("geo-graph", directory, "--changed", "provider.py"))
        assert graph["scope"]["linked_test_files"] == ["test_provider.py"], graph
        graph_path, history_path = root / "graph.json", root / "history.json"
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        history_path.write_text(json.dumps({"records": [{
            "revision": {"id": "r1", "comparison_parent_id": "r0"},
            "file_changes": [
                {"status": "M", "path": "provider.py"},
                {"status": "M", "path": "test_provider.py"},
            ],
        }]}), encoding="utf-8")
        report = json.loads(run(
            "geo-tension", "--graph", str(graph_path), "--history", str(history_path),
            "--format", "json",
        ))
        assert report["pair_count"] == 1, report
        assert report["pairs"][0]["structural_evidence"] == "known_static_path", report
    with tempfile.TemporaryDirectory(prefix="geo-installed-review-") as directory:
        check_change_review(Path(directory))
    print("Installed wheel: CLI, demo, graph/tension JSON, committed/staged review, read-only, license and dependency checks passed.")


if __name__ == "__main__":
    main()
