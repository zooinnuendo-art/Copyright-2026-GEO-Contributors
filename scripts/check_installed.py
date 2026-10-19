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
    print("Installed wheel: CLI, demo fixtures, graph/tension JSON, license and dependency checks passed.")


if __name__ == "__main__":
    main()
