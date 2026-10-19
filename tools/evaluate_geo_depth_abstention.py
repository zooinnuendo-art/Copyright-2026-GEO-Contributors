"""One-shot synthetic diagnostic for GEO's bounded test-call traversal.

The generator and evaluator are maintained together, so this is exploratory
evidence only, not an independent holdout or real-repository estimate.
"""

from __future__ import annotations

import argparse
import json
import random
from typing import Any

from tools.code_dependency_graph import build_dependency_graph


SEED = 20261016
CALL_HOP_LIMIT = 8
STRESS_SEEDS = tuple(range(20261017, 20261037))


def build_depth_abstention_snapshot(seed: int = SEED) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Generate depths 1..12 plus independent positive, boundary, and negative controls."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    generator = random.Random(seed)
    depth_order = list(range(1, 13))
    generator.shuffle(depth_order)
    chain_keys = {depth: f"case_{position:02d}" for position, depth in enumerate(depth_order)}

    sources = {
        "src/__init__.py": "",
        "src/core.py": "def changed():\n    return 1\n",
        "tests/__init__.py": "",
    }
    labels = []
    for depth in range(1, 13):
        chain_key = chain_keys[depth]
        test_path = f"tests/test_chain_{chain_key}.py"
        if depth == 1:
            sources[test_path] = (
                "from src.core import changed\n"
                "def test_chain():\n    return changed()\n"
            )
        else:
            helper_count = depth - 1
            for step in range(helper_count):
                module_path = f"src/chains/{chain_key}/step_{step}.py"
                module_name = f"src.chains.{chain_key}.step_{step}"
                if step + 1 < helper_count:
                    next_name = f"src.chains.{chain_key}.step_{step + 1}"
                    function_name = f"step_{step + 1}"
                else:
                    next_name = "src.core"
                    function_name = "changed"
                local_function = f"step_{step}"
                sources[module_path] = (
                    f"from {next_name} import {function_name}\n"
                    f"def {local_function}():\n    return {function_name}()\n"
                )
            sources[test_path] = (
                f"from src.chains.{chain_key}.step_0 import step_0\n"
                "def test_chain():\n    return step_0()\n"
            )
        labels.append({
            "case_id": f"depth_{depth:02d}",
            "test_file": test_path,
            "expected_affected": True,
            "known_call_hops": depth,
        })

    dynamic_path = "tests/test_dynamic_boundary.py"
    sources[dynamic_path] = (
        "import src.core as core\n"
        "def test_dynamic(name):\n    return getattr(core, name)()\n"
    )
    labels.append({
        "case_id": "computed_dispatch_positive_control",
        "test_file": dynamic_path,
        "expected_affected": True,
        "known_call_hops": None,
    })

    unrelated_path = "tests/test_unrelated.py"
    sources[unrelated_path] = "def test_unrelated():\n    return 2 + 3\n"
    labels.append({
        "case_id": "unrelated_negative_control",
        "test_file": unrelated_path,
        "expected_affected": False,
        "known_call_hops": None,
    })
    return sources, labels


def run_depth_abstention_diagnostic(seed: int = SEED) -> dict[str, Any]:
    """Evaluate local selection/abstention against the generated labels once."""
    sources, labels = build_depth_abstention_snapshot(seed)
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ain-geo-depth-abstention-") as directory:
        root = Path(directory)
        for relative, content in sources.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        graph = build_dependency_graph(
            root,
            changed_files=["src/core.py"],
            max_hops=0,
            include_import_use_test_fallback=True,
        )

    selected = set(graph["scope"]["linked_test_files"])
    depth_limited_paths = {
        issue.get("path")
        for issue in graph.get("incomplete_causes", {}).get("scope", [])
        if issue.get("kind") == "test_call_depth_limit_reached"
    }
    unresolved_paths = {
        example.get("path")
        for issue in graph["issues"]
        if issue.get("kind") == "unresolved_attribute_call_summary"
        for example in issue.get("examples", [])
    }

    case_results = []
    expected_count = selected_expected = actionable_expected = depth_abstentions = 0
    unrelated_selected = False
    for label in labels:
        path = label["test_file"]
        is_selected = path in selected
        is_expected = label["expected_affected"]
        depth_abstain = path in depth_limited_paths
        unresolved_boundary = path in unresolved_paths
        if is_expected:
            expected_count += 1
            selected_expected += int(is_selected)
            actionable_expected += int(is_selected and not depth_abstain and not unresolved_boundary)
            depth_abstentions += int(depth_abstain)
        else:
            unrelated_selected = is_selected
        case_results.append({
            **label,
            "selected": is_selected,
            "depth_limit_abstention": depth_abstain,
            "unresolved_boundary": unresolved_boundary,
            "actionable": is_selected and not depth_abstain and not unresolved_boundary,
        })

    selected_count = len(selected)
    expected_selected_count = sum(item["expected_affected"] and item["selected"] for item in case_results)
    return {
        "schema_version": 1,
        "analysis_kind": "geo_depth_abstention_mixed_snapshot_diagnostic",
        "cohort_kind": "seeded_synthetic_same_session_diagnostic",
        "cohort_status": "consumed_after_this_run",
        "seed": seed,
        "call_hop_limit": CALL_HOP_LIMIT,
        "snapshot_python_files": len(sources),
        "graph_status": graph["status"],
        "incomplete_causes": graph.get("incomplete_causes", {}),
        "metrics": {
            "expected_affected_tests": expected_count,
            "selected_tests": selected_count,
            "selected_affected_tests": expected_selected_count,
            "affected_test_recall": expected_selected_count / expected_count if expected_count else None,
            "actionable_affected_recall": actionable_expected / expected_count if expected_count else None,
            "depth_limit_abstentions": depth_abstentions,
            "unrelated_negative_selected": unrelated_selected,
            "unnecessary_selection_rate": (
                (selected_count - expected_selected_count) / selected_count if selected_count else None
            ),
            "selected_static_tests_not_contaminated_by_other_depth_limits": sum(
                item["selected"] and item["expected_affected"] and item["known_call_hops"] is not None
                and item["known_call_hops"] <= CALL_HOP_LIMIT and not item["depth_limit_abstention"]
                for item in case_results
            ),
        },
        "cases": case_results,
        "limitations": [
            "Generator and evaluator are maintained in this repository and run by the same agent; this is not independent evaluation.",
            "The synthetic result is not a real-repository estimate or proof of semantic impact.",
            "This seeded cohort is consumed and must not be used to tune or confirm later changes.",
        ],
    }


def run_depth_abstention_metamorphic_check(
    seeds: tuple[int, ...] = STRESS_SEEDS,
) -> dict[str, Any]:
    """Check frozen depth/locality invariants over deterministic path renamings."""
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be a non-empty sequence of unique integers")
    runs = []
    invariant_names = (
        "in_bound_static_tests_selected_actionably",
        "over_bound_tests_unselected_and_locally_abstained",
        "computed_dispatch_selected_with_boundary",
        "unrelated_negative_not_selected",
        "depth_causes_do_not_become_global_causes",
    )
    for seed in seeds:
        result = run_depth_abstention_diagnostic(seed)
        cases = {item["case_id"]: item for item in result["cases"]}
        causes = result["incomplete_causes"]
        expected_over_bound_paths = {
            item["test_file"] for item in result["cases"]
            if item["known_call_hops"] is not None and item["known_call_hops"] > CALL_HOP_LIMIT
        }
        actual_depth_paths = {
            issue.get("path") for issue in causes.get("scope", [])
            if issue.get("kind") == "test_call_depth_limit_reached"
        }
        invariants = {
            "in_bound_static_tests_selected_actionably": all(
                cases[f"depth_{depth:02d}"]["selected"]
                and cases[f"depth_{depth:02d}"]["actionable"]
                and not cases[f"depth_{depth:02d}"]["depth_limit_abstention"]
                for depth in range(1, CALL_HOP_LIMIT + 1)
            ),
            "over_bound_tests_unselected_and_locally_abstained": all(
                not cases[f"depth_{depth:02d}"]["selected"]
                and cases[f"depth_{depth:02d}"]["depth_limit_abstention"]
                for depth in range(CALL_HOP_LIMIT + 1, 13)
            ),
            "computed_dispatch_selected_with_boundary": (
                cases["computed_dispatch_positive_control"]["selected"]
                and cases["computed_dispatch_positive_control"]["unresolved_boundary"]
                and not cases["computed_dispatch_positive_control"]["depth_limit_abstention"]
            ),
            "unrelated_negative_not_selected": not cases["unrelated_negative_control"]["selected"],
            "depth_causes_do_not_become_global_causes": (
                not causes.get("global")
                and actual_depth_paths == expected_over_bound_paths
            ),
        }
        runs.append({
            "seed": seed,
            "invariants": invariants,
            "passed_invariants": sum(invariants.values()),
            "total_invariants": len(invariants),
            "all_passed": all(invariants.values()),
            "expected_over_bound_paths": sorted(expected_over_bound_paths),
            "actual_depth_issue_paths": sorted(actual_depth_paths),
        })

    passed_runs = sum(run["all_passed"] for run in runs)
    invariant_pass_counts = {
        name: sum(run["invariants"][name] for run in runs)
        for name in invariant_names
    }
    return {
        "schema_version": 1,
        "analysis_kind": "geo_depth_abstention_metamorphic_check",
        "cohort_kind": "same_generator_path_renaming_metamorphic_diagnostic",
        "cohort_status": "consumed_after_this_run",
        "call_hop_limit": CALL_HOP_LIMIT,
        "seed_count": len(seeds),
        "seeds": list(seeds),
        "passed_runs": passed_runs,
        "failed_runs": len(runs) - passed_runs,
        "invariant_pass_counts": invariant_pass_counts,
        "all_runs_passed": passed_runs == len(runs),
        "runs": runs,
        "limitations": [
            "Each seed renames/reorders one fixed synthetic topology; runs are not independent samples.",
            "Generator and evaluator are maintained in the same repository and run by the same agent.",
            "This checks path/name-order robustness only; it is not a performance estimate or independent evaluation.",
            "The seed set is consumed after this run and must not be used to tune later changes.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--metamorphic-check", action="store_true")
    arguments = parser.parse_args()
    result = (
        run_depth_abstention_metamorphic_check()
        if arguments.metamorphic_check
        else run_depth_abstention_diagnostic(arguments.seed)
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()