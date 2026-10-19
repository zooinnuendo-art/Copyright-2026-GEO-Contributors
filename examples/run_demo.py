"""Analyze a toy snapshot and temporary Git history; leave source files untouched."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_history_tension_probe import analyze_history_structural_tension
from tools.git_temporal_history import extract_git_temporal_history


FIXTURE = Path(__file__).resolve().parent / "shop"


def run_demo():
    graph = build_dependency_graph(FIXTURE, changed_files=["shop/pricing.py"])
    with tempfile.TemporaryDirectory(prefix="geo-demo-") as directory:
        repository = Path(directory) / "shop"
        shutil.copytree(FIXTURE, repository, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(repository), *arguments],
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout.strip()

        def commit(message):
            git("add", ".")
            git("commit", "-q", "-m", message)
            return git("rev-parse", "HEAD")

        git("init", "-q")
        git("config", "user.name", "GEO Demo")
        git("config", "user.email", "geo-demo@example.invalid")
        git("config", "commit.gpgsign", "false")
        commit("baseline")
        (repository / "shop/pricing.py").write_text(
            "def total(price, quantity):\n    return round(price * quantity, 2)\n",
            encoding="utf-8",
        )
        (repository / "shop/policy.py").write_text("MAX_QUANTITY = 20\n", encoding="utf-8")
        edited = commit("cochange pricing and policy")
        git("mv", "shop/pricing.py", "shop/rates.py")
        (repository / "shop/checkout.py").write_text(
            "from shop.rates import total\n\n\ndef checkout(price, quantity):\n"
            "    return total(price, quantity)\n",
            encoding="utf-8",
        )
        moved = commit("move pricing module and update importer")
        history = extract_git_temporal_history(
            repository, [edited, moved], include_final_snapshot=True,
        )
        final_graph = history.pop("final_snapshot")
        tension = analyze_history_structural_tension(final_graph, history)
        renames = [
            change for record in history["records"] for change in record["file_changes"]
            if change["status"] == "R"
        ]
    return {
        "example_kind": "synthetic_demonstration_not_effectiveness_evidence",
        "snapshot": {
            "status": graph["status"],
            "changed_file": "shop/pricing.py",
            "candidate_tests": graph["scope"]["linked_test_files"],
        },
        "detected_renames": renames,
        "history_tension": tension,
    }


def main():
    try:
        result = run_demo()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"GEO demo failed: {error}") from error
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
