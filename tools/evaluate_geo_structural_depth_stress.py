"""One-shot synthetic stress diagnostic over varied call-graph topologies.

This generator and evaluator are maintained together and are not independent
evaluation. The cohort is intended to test structural/path contamination,
not estimate repository performance.
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
from pathlib import Path
from typing import Any

from tools.code_dependency_graph import build_dependency_graph


STRUCTURAL_SEEDS = tuple(range(20261040, 20261048))
CALL_HOP_LIMIT = 8


def _add_helper_chain(
    sources: dict[str, str],
    *,
    prefix: str,
    helper_count: int,
) -> str:
    if helper_count <= 0:
        raise ValueError("helper_count must be positive")
    for index in range(helper_count):
        module = f"app/{prefix}/node_{index}.py"
        module_name = f"app.{prefix}.node_{index}"
        if index + 1 < helper_count:
            imported_module = f"app.{prefix}.node_{index + 1}"
            imported_name = f"relay_{index + 1}"
        else:
            imported_module = "app.core"
            imported_name = "changed"
        function_name = f"relay_{index}"
        sources[module] = (
            f"from {imported_module} import {imported_name}\n"
            f"def {function_name}():\n    return {imported_name}()\n"
        )
    return f"app.{prefix}.node_0"


def build_structural_snapshot(seed: int) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Generate one mixed-topology snapshot with known affected-test labels."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    rng = random.Random(seed)
    sources = {
        "app/__init__.py": "",
        "app/core.py": "def changed():\n    return 1\n",
        "tests/__init__.py": "",
    }
    labels = []

    def add_test(case_id: str, path: str, source: str, *, affected: bool, hops: int | None) -> None:
        sources[path] = source
        labels.append({
            "case_id": case_id,
            "test_file": path,
            "expected_affected": affected,
            "known_call_hops": hops,
        })

    add_test(
        "direct_positive",
        "tests/test_direct.py",
        "from app.core import changed\ndef test_direct():\n    return changed()\n",
        affected=True,
        hops=1,
    )

    branch_width = 2 + seed % 2
    branch_entrypoints = []
    branch_hops = []
    for branch in range(branch_width):
        chain_length = 1 + ((seed + branch * 3) % 4)
        entry_module = _add_helper_chain(
            sources,
            prefix=f"branch_{branch}",
            helper_count=chain_length,
        )
        branch_entrypoints.append((entry_module, "relay_0"))
        branch_hops.append(chain_length + 1)

    if branch_width == 3:
        branch_imports = "".join(
            f"from {module} import {name} as branch_{index}\n"
            for index, (module, name) in enumerate(branch_entrypoints)
        )
        branch_calls = " and ".join(f"branch_{index}()" for index in range(branch_width))
        branch_source = f"{branch_imports}def test_branch():\n    return {branch_calls}\n"
        branch_id = "branching_fanout"
    else:
        branch_imports = (
            f"from {branch_entrypoints[0][0]} import relay_0 as left_branch\n"
            f"from {branch_entrypoints[1][0]} import relay_0 as right_branch\n"
        )
        branch_source = (
            f"{branch_imports}def test_diamond():\n"
            "    left_branch()\n"
            "    return right_branch()\n"
        )
        branch_id = "diamond_two_branch"
    add_test(
        branch_id,
        f"tests/test_{branch_id}.py",
        branch_source,
        affected=True,
        hops=max(branch_hops),
    )

    sources["app/cycle.py"] = (
        "from app.core import changed\n"
        "def alpha():\n    return beta()\n"
        "def beta():\n    alpha()\n    return changed()\n"
    )
    add_test(
        "cycle_with_exit",
        "tests/test_cycle.py",
        "from app.cycle import alpha\ndef test_cycle():\n    return alpha()\n",
        affected=True,
        hops=3,
    )

    sources["app/runner.py"] = (
        "from app.core import changed\n"
        "class Runner:\n"
        "    def execute(self):\n        return changed()\n"
    )
    add_test(
        "class_method_path",
        "tests/test_class.py",
        "from app.runner import Runner\ndef test_class():\n    return Runner.execute(None)\n",
        affected=True,
        hops=3,
    )

    dynamic_path = f"tests/test_dynamic_{seed % 97:02d}.py"
    add_test(
        "computed_dispatch_boundary",
        dynamic_path,
        "import app.core as core\n"
        "def test_dynamic(name):\n    return getattr(core, name)()\n",
        affected=True,
        hops=None,
    )

    deep_hops = 9 + seed % 4
    deep_helper_count = deep_hops - 1
    deep_module = _add_helper_chain(
        sources,
        prefix=f"deep_{seed % 97:02d}",
        helper_count=deep_helper_count,
    )
    add_test(
        "beyond_depth_boundary",
        f"tests/test_deep_{seed % 97:02d}.py",
        f"from {deep_module} import relay_0\n"
        "def test_deep():\n    return relay_0()\n",
        affected=True,
        hops=deep_hops,
    )

    unrelated_name = f"unrelated_{seed % 97:02d}"
    sources[f"app/{unrelated_name}.py"] = (
        f"def {unrelated_name}():\n    return 5\n"
    )
    add_test(
        "unrelated_production_helper_negative",
        f"tests/test_{unrelated_name}.py",
        f"from app.{unrelated_name} import {unrelated_name}\n"
        f"def test_unrelated():\n    return {unrelated_name}()\n",
        affected=False,
        hops=None,
    )

    negative_import = f"tests/test_unused_{seed % 97:02d}.py"
    add_test(
        "unused_import_negative",
        negative_import,
        "from app.core import changed\ndef test_unused():\n    return 11\n",
        affected=False,
        hops=None,
    )

    rng.shuffle(labels)
    return sources, labels


def run_structural_depth_stress(seeds: tuple[int, ...] = STRUCTURAL_SEEDS) -> dict[str, Any]:
    """Evaluate the frozen selector against the generated structural recipes once."""
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be a non-empty sequence of unique integers")
    runs = []
    aggregate = {
        "expected_affected": 0,
        "selected_affected": 0,
        "actionable_affected": 0,
        "selected_total": 0,
        "unnecessary_selected": 0,
        "over_bound_expected": 0,
        "over_bound_unselected": 0,
        "over_bound_locally_abstained": 0,
        "in_bound_static_expected": 0,
        "in_bound_static_actionable": 0,
        "negative_controls": 0,
        "negative_controls_selected": 0,
        "global_incomplete_runs": 0,
    }
    for seed in seeds:
        sources, labels = build_structural_snapshot(seed)
        with tempfile.TemporaryDirectory(prefix="ain-geo-structural-stress-") as directory:
            root = Path(directory)
            for relative, content in sources.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            graph = build_dependency_graph(
                root,
                changed_files=["app/core.py"],
                max_hops=0,
                include_import_use_test_fallback=True,
            )

        selected_paths = set(graph["scope"]["linked_test_files"])
        local_depth_paths = {
            issue.get("path")
            for issue in graph.get("incomplete_causes", {}).get("scope", [])
            if issue.get("kind") == "test_call_depth_limit_reached"
        }
        unresolved_attribute_paths = {
            example.get("path")
            for issue in graph["issues"]
            if issue.get("kind") == "unresolved_attribute_call_summary"
            for example in issue.get("examples", [])
        }
        global_causes = graph.get("incomplete_causes", {}).get("global", [])
        case_rows = []
        for label in labels:
            path = label["test_file"]
            selected = path in selected_paths
            local_abstention = path in local_depth_paths
            dynamic_boundary = path in unresolved_attribute_paths
            actionable = selected and not local_abstention and not dynamic_boundary
            affected = label["expected_affected"]
            hops = label["known_call_hops"]
            if affected:
                aggregate["expected_affected"] += 1
                aggregate["selected_affected"] += int(selected)
                aggregate["actionable_affected"] += int(actionable)
            else:
                aggregate["negative_controls"] += 1
                aggregate["negative_controls_selected"] += int(selected)
            aggregate["selected_total"] += int(selected)
            aggregate["unnecessary_selected"] += int(selected and not affected)
            if hops is not None and hops > CALL_HOP_LIMIT:
                aggregate["over_bound_expected"] += 1
                aggregate["over_bound_unselected"] += int(not selected)
                aggregate["over_bound_locally_abstained"] += int(local_abstention)
            if affected and hops is not None and hops <= CALL_HOP_LIMIT:
                aggregate["in_bound_static_expected"] += 1
                aggregate["in_bound_static_actionable"] += int(actionable)
            case_rows.append({
                **label,
                "selected": selected,
                "actionable": actionable,
                "depth_limit_abstention": local_abstention,
                "dynamic_boundary": dynamic_boundary,
            })
        aggregate["global_incomplete_runs"] += int(bool(global_causes))
        runs.append({
            "seed": seed,
            "snapshot_files": len(sources),
            "graph_status": graph["status"],
            "global_incomplete_causes": global_causes,
            "scope_incomplete_causes": graph.get("incomplete_causes", {}).get("scope", []),
            "cases": case_rows,
        })

    expected = aggregate["expected_affected"]
    selected = aggregate["selected_total"]
    aggregate_metrics = {
        **aggregate,
        "affected_test_recall": aggregate["selected_affected"] / expected if expected else None,
        "actionable_affected_recall": aggregate["actionable_affected"] / expected if expected else None,
        "in_bound_static_actionable_rate": (
            aggregate["in_bound_static_actionable"] / aggregate["in_bound_static_expected"]
            if aggregate["in_bound_static_expected"] else None
        ),
        "over_bound_local_abstention_rate": (
            aggregate["over_bound_locally_abstained"] / aggregate["over_bound_expected"]
            if aggregate["over_bound_expected"] else None
        ),
        "negative_control_false_selection_rate": (
            aggregate["negative_controls_selected"] / aggregate["negative_controls"]
            if aggregate["negative_controls"] else None
        ),
        "unnecessary_selection_rate": (
            aggregate["unnecessary_selected"] / selected if selected else None
        ),
    }
    return {
        "schema_version": 1,
        "analysis_kind": "geo_structural_depth_abstention_stress",
        "cohort_kind": "seeded_synthetic_varied_topology_same_session",
        "cohort_status": "consumed_after_this_run",
        "call_hop_limit": CALL_HOP_LIMIT,
        "seeds": list(seeds),
        "seed_count": len(seeds),
        "topology_recipes": ["direct", "branch_or_diamond", "cycle_with_exit", "class_method", "computed_dispatch", "deep_chain", "unrelated_helper", "unused_import"],
        "metrics": aggregate_metrics,
        "runs": runs,
        "limitations": [
            "The generator, labels, and evaluator are maintained and run in the same session; this is not independent evaluation.",
            "Seeds vary synthetic topology parameters but do not represent real-repository distributions.",
            "The cohort is consumed and must not be tuned or reused as an untouched holdout.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    arguments = parser.parse_args()
    print(json.dumps(run_structural_depth_stress(), ensure_ascii=True, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()