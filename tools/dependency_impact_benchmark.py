"""Measure GEO test-scope selection on a small, labeled Python cohort."""

from __future__ import annotations

from pathlib import Path
import math
import tempfile
from typing import Any

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_geodesic_probe import compute_bidirectional_dependency_meetings


def _write_snapshot(root: Path, sources: dict[str, str]) -> None:
    for relative_path, source in sources.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def _abstention_reasons(graph: dict[str, Any]) -> list[str]:
    reasons = set()
    for issue in graph["issues"]:
        kind = issue["kind"]
        if kind == "parse_error":
            reasons.add("syntax_error")
        elif kind in {"file_budget_exceeded", "node_budget_exceeded", "edge_budget_exceeded"}:
            reasons.add("budget_exceeded")
        elif kind in {"changed_file_not_found", "snapshot_read_error"}:
            reasons.add("snapshot_incomplete")
        elif kind == "unresolved_local_import":
            reasons.add("unresolved_import")
        elif kind == "unresolved_simple_call_summary":
            reasons.add("unresolved_call")
        elif kind == "unresolved_attribute_call_summary":
            reasons.add("unresolved_attribute")
        elif kind == "test_call_depth_limit_reached":
            reasons.add("test_call_depth_limit")
    if graph["status"] == "incomplete/review" and not reasons:
        reasons.add("other_incomplete")
    return sorted(reasons)


def _cases() -> tuple[dict[str, Any], ...]:
    return (
        {
            "case_id": "direct_call",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_api.py": (
                    "from app.api import run\n"
                    "def test_run():\n    assert run(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "helper_mediated_call",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/helpers.py": (
                    "from app.api import run\n"
                    "def exercise():\n    return run(3)\n"
                ),
                "tests/test_api.py": (
                    "from tests.helpers import exercise\n"
                    "def test_run():\n    assert exercise() == 3\n"
                ),
            },
        },
        {
            "case_id": "three_stage_production_helper_chain",
            "changed_files": ["service/core.py"],
            "expected_test_files": ["tests/test_pipeline.py"],
            "sources": {
                "service/core.py": "def transform(value):\n    return value\n",
                "service/adapter.py": (
                    "from service.core import transform\n"
                    "def adapt(value):\n    return transform(value)\n"
                ),
                "tests/support.py": (
                    "from service.adapter import adapt\n"
                    "def exercise(value):\n    return adapt(value)\n"
                ),
                "tests/test_pipeline.py": (
                    "from tests.support import exercise\n"
                    "def test_transform():\n    assert exercise(5) == 5\n"
                ),
            },
        },
        {
            "case_id": "neighbor_scope_extra_test",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "max_hops": 1,
            "sources": {
                "app/api.py": (
                    "from app.util import normalize\n"
                    "def run(value):\n    return normalize(value)\n"
                ),
                "app/util.py": "def normalize(value):\n    return value\n",
                "tests/test_api.py": (
                    "from app.api import run\n"
                    "def test_run():\n    assert run(3) == 3\n"
                ),
                "tests/test_util.py": (
                    "from app.util import normalize\n"
                    "def test_normalize():\n    assert normalize(4) == 4\n"
                ),
            },
        },
        {
            "case_id": "provider_symbol_removed",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "app/api.py": "def replacement(value):\n    return value\n",
                "tests/test_api.py": (
                    "from app.api import run\n"
                    "def test_run():\n    assert run(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "provider_module_removed",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "tests/test_api.py": (
                    "from app.api import run\n"
                    "def test_run():\n    assert run(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "submodule_import_removed",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "tests/test_api.py": (
                    "from app import api\n"
                    "def test_run():\n    assert api.run(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "relative_import_removed",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["app/tests/test_api.py"],
            "sources": {
                "app/__init__.py": "",
                "app/tests/__init__.py": "",
                "app/tests/test_api.py": (
                    "from ..api import run\n"
                    "def test_run():\n    assert run(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "literal_getattr_call",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_api.py": (
                    "import app.api as api\n"
                    "def test_run():\n    assert getattr(api, 'run')(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "computed_getattr_name",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_api.py": (
                    "import app.api as api\n"
                    "def test_run(attribute):\n"
                    "    assert getattr(api, attribute)(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "shadowed_getattr_negative",
            "changed_files": ["app/api.py"],
            "expected_test_files": [],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_api.py": (
                    "import app.api as api\n"
                    "def test_shadowed_getattr():\n"
                    "    def getattr(obj, name):\n        return lambda: 1\n"
                    "    assert getattr(api, 'run')() == 1\n"
                ),
            },
        },
        {
            "case_id": "unused_import_negative",
            "changed_files": ["app/api.py"],
            "expected_test_files": [],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_unrelated.py": (
                    "from app.api import run\n"
                    "def test_constant():\n    assert 1 + 1 == 2\n"
                ),
            },
        },
        {
            "case_id": "same_name_shadow_negative",
            "changed_files": ["app/api.py"],
            "expected_test_files": [],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_shadow.py": (
                    "from app.api import run\n"
                    "def test_local_run():\n"
                    "    def run(value):\n        return value + 1\n"
                    "    assert run(2) == 3\n"
                ),
            },
        },
        {
            "case_id": "syntax_error_in_test",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_api.py": (
                    "from app.api import run\n"
                    "def test_run(:\n    assert run(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "file_budget_exceeded",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "build_options": {"max_files": 1},
            "sources": {
                "app/api.py": "def run(value):\n    return value\n",
                "tests/test_api.py": (
                    "from app.api import run\n"
                    "def test_run():\n    assert run(3) == 3\n"
                ),
            },
        },
        {
            "case_id": "ambiguous_provider_symbol",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["tests/test_api.py"],
            "sources": {
                "app/api.py": (
                    "def run(value):\n    return value\n"
                    "def run(value):\n    return value + 1\n"
                ),
                "tests/test_api.py": (
                    "from app.api import run\n"
                    "def test_run():\n    assert run(3) == 3\n"
                ),
            },
        },
    )


def _wave_holdout_cases() -> tuple[dict[str, Any], ...]:
    """Hand-labeled cases reserved for a wave-priority holdout comparison."""
    return (
        {
            "case_id": "holdout_three_stage_helper_chain",
            "split": "evaluation",
            "changed_files": ["service/api.py"],
            "expected_test_files": ["tests/test_service.py"],
            "sources": {
                "service/api.py": "def fetch(item):\n    return item\n",
                "service/adapter.py": (
                    "from service.api import fetch\n"
                    "def adapt(item):\n    return fetch(item)\n"
                ),
                "tests/support.py": (
                    "from service.adapter import adapt\n"
                    "def exercise(item):\n    return adapt(item)\n"
                ),
                "tests/test_service.py": (
                    "from tests.support import exercise\n"
                    "def test_fetch():\n    assert exercise(5) == 5\n"
                ),
            },
        },
        {
            "case_id": "holdout_computed_dispatch",
            "split": "evaluation",
            "changed_files": ["service/api.py"],
            "expected_test_files": ["tests/test_dispatch.py"],
            "sources": {
                "service/api.py": "def fetch(item):\n    return item\n",
                "tests/test_dispatch.py": (
                    "import service.api as api\n"
                    "def test_dispatch(name):\n"
                    "    return getattr(api, name)(5)\n"
                ),
            },
        },
        {
            "case_id": "holdout_deleted_provider",
            "split": "evaluation",
            "changed_files": ["service/api.py"],
            "expected_test_files": ["tests/test_deleted_api.py"],
            "sources": {
                "tests/test_deleted_api.py": (
                    "from service.api import fetch\n"
                    "def test_fetch():\n    return fetch(5)\n"
                ),
            },
        },
        {
            "case_id": "holdout_neighbor_overselection",
            "split": "evaluation",
            "changed_files": ["service/api.py"],
            "expected_test_files": ["tests/test_service.py"],
            "max_hops": 1,
            "sources": {
                "service/api.py": (
                    "from service.normalize import clean\n"
                    "def fetch(item):\n    return clean(item)\n"
                ),
                "service/normalize.py": "def clean(item):\n    return item\n",
                "tests/test_service.py": (
                    "from service.api import fetch\n"
                    "def test_fetch():\n    return fetch(5)\n"
                ),
                "tests/test_normalize.py": (
                    "from service.normalize import clean\n"
                    "def test_clean():\n    return clean(5)\n"
                ),
            },
        },
        {
            "case_id": "holdout_unrelated_import_consumer",
            "split": "evaluation",
            "changed_files": ["service/api.py"],
            "expected_test_files": [],
            "sources": {
                "service/api.py": "def fetch(item):\n    return item\n",
                "tests/test_formatting.py": (
                    "from service.api import fetch\n"
                    "def test_formatting():\n    return str(5)\n"
                ),
                "tests/test_unrelated.py": (
                    "def test_unrelated():\n    assert 3 + 4 == 7\n"
                ),
            },
        },
    )


def _call_chain_evaluation_cases() -> tuple[dict[str, Any], ...]:
    """Fresh synthetic cases for the frozen bounded test-call traversal."""
    def helper_chain(case_id: str, helper_count: int, *, expected: bool = True) -> dict[str, Any]:
        sources = {
            "pkg/core.py": "def changed():\n    return 1\n",
            "tests/test_chain.py": (
                "from pkg.chain import helper_0\n"
                "def test_chain():\n    return helper_0()\n"
            ),
        }
        helper_source = "from pkg.core import changed\n"
        for index in range(helper_count):
            target = f"helper_{index + 1}()" if index + 1 < helper_count else "changed()"
            helper_source += f"def helper_{index}():\n    return {target}\n"
        sources["pkg/chain.py"] = helper_source
        return {
            "case_id": case_id,
            "changed_files": ["pkg/core.py"],
            "expected_test_files": ["tests/test_chain.py"] if expected else [],
            "sources": sources,
        }

    return (
        helper_chain("chain_exactly_eight_hops", 7),
        helper_chain("chain_nine_hops_truncated", 8),
        {
            "case_id": "cycle_with_exit_to_changed_code",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": ["tests/test_cycle.py"],
            "sources": {
                "pkg/core.py": "def changed():\n    return 1\n",
                "pkg/cycle.py": (
                    "from pkg.core import changed\n"
                    "def first():\n    second()\n    return changed()\n"
                    "def second():\n    return first()\n"
                ),
                "tests/test_cycle.py": (
                    "from pkg.cycle import first\n"
                    "def test_cycle():\n    return first()\n"
                ),
            },
        },
        {
            "case_id": "cycle_without_changed_exit",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": [],
            "sources": {
                "pkg/core.py": "def changed():\n    return 1\n",
                "pkg/cycle.py": (
                    "def first():\n    return second()\n"
                    "def second():\n    return first()\n"
                ),
                "tests/test_cycle.py": (
                    "from pkg.cycle import first\n"
                    "def test_cycle():\n    return first()\n"
                ),
            },
        },
        {
            "case_id": "imported_class_method_chain",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": ["tests/test_class.py"],
            "sources": {
                "pkg/core.py": "def changed():\n    return 1\n",
                "pkg/runner.py": (
                    "from pkg.core import changed\n"
                    "class Runner:\n"
                    "    def execute(self):\n        return changed()\n"
                ),
                "tests/test_class.py": (
                    "from pkg.runner import Runner\n"
                    "def test_class():\n    return Runner.execute(None)\n"
                ),
            },
        },
        {
            "case_id": "unrelated_imported_production_helper",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": [],
            "sources": {
                "pkg/core.py": "def changed():\n    return 1\n",
                "pkg/other.py": "def unrelated():\n    return 7\n",
                "tests/test_other.py": (
                    "from pkg.other import unrelated\n"
                    "def test_other():\n    return unrelated()\n"
                ),
            },
        },
        {
            "case_id": "computed_dispatch_at_boundary",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": ["tests/test_dispatch.py"],
            "sources": {
                "pkg/core.py": "def changed():\n    return 1\n",
                "tests/test_dispatch.py": (
                    "import pkg.core as core\n"
                    "def test_dispatch(name):\n    return getattr(core, name)()\n"
                ),
            },
        },
        {
            "case_id": "test_imports_changed_module_but_never_calls_it",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": [],
            "sources": {
                "pkg/core.py": "def changed():\n    return 1\n",
                "tests/test_constant.py": (
                    "from pkg.core import changed\n"
                    "def test_constant():\n    return 5\n"
                ),
            },
        },
    )


def run_dependency_impact_benchmark(
    *,
    max_hops: int = 0,
    include_import_use_test_fallback: bool = True,
    cases: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Score selected test files against explicit fixture-level impact labels.

    These hand-authored fixtures measure extractor/scope behavior only; they
    are not an estimate of recall on real repositories.
    """
    if not 0 <= max_hops <= 2:
        raise ValueError("max_hops must be between 0 and 2")
    benchmark_cases = _cases() if cases is None else cases
    if not benchmark_cases:
        raise ValueError("cases must not be empty")

    case_results = []
    total_expected = 0
    total_selected = 0
    total_recalled = 0
    total_actionable_recalled = 0
    total_complete_expected = 0
    total_complete_recalled = 0
    abstained_cases = 0
    abstention_reason_counts: dict[str, int] = {}
    for case in benchmark_cases:
        with tempfile.TemporaryDirectory(prefix="ain-geo-impact-") as directory:
            root = Path(directory)
            _write_snapshot(root, case["sources"])
            graph = build_dependency_graph(
                root,
                changed_files=case["changed_files"],
                max_hops=case.get("max_hops", max_hops),
                include_import_use_test_fallback=include_import_use_test_fallback,
                **case.get("build_options", {}),
            )
        selected = sorted(graph["scope"]["linked_test_files"])
        expected = sorted(case["expected_test_files"])
        recalled = sorted(set(selected) & set(expected))
        misses = sorted(set(expected) - set(selected))
        extras = sorted(set(selected) - set(expected))
        changed_paths = set(case["changed_files"])
        nodes_by_id = {node["id"]: node for node in graph["nodes"]}
        resolved_calls_to_changed_scope = [
            edge
            for edge in graph["edges"]
            if edge["type"] == "calls"
            and nodes_by_id.get(edge["target"], {}).get("path") in changed_paths
        ]
        unresolved = [
            issue for issue in graph["issues"]
            if issue["kind"] in {
                "unresolved_attribute_call_summary",
                "unresolved_simple_call_summary",
                "test_call_depth_limit_reached",
                "unresolved_local_import",
            }
        ]
        reasons = _abstention_reasons(graph)
        incomplete = graph["status"] == "incomplete/review"
        abstained = incomplete or bool(unresolved) or bool(reasons)
        actionable_recalled = [] if abstained else recalled
        case_results.append({
            "case_id": case["case_id"],
            "expected_test_files": expected,
            "selected_test_files": selected,
            "recalled_test_files": recalled,
            "actionable_recalled_test_files": actionable_recalled,
            "missed_test_files": misses,
            "unnecessary_test_files": extras,
            "resolved_calls_to_changed_scope": len(resolved_calls_to_changed_scope),
            "unresolved_issue_count": len(unresolved),
            "abstention_reasons": reasons,
            "analysis_incomplete": incomplete,
            "abstained": abstained,
        })
        total_expected += len(expected)
        total_selected += len(selected)
        total_recalled += len(recalled)
        total_actionable_recalled += len(actionable_recalled)
        abstained_cases += int(abstained)
        if abstained:
            for reason in reasons:
                abstention_reason_counts[reason] = abstention_reason_counts.get(reason, 0) + 1
        else:
            total_complete_expected += len(expected)
            total_complete_recalled += len(recalled)

    return {
        "schema_version": 1,
        "analysis_kind": "geo_synthetic_dependency_impact_benchmark",
        "scope_max_hops": max_hops,
        "import_use_test_fallback": include_import_use_test_fallback,
        "cohort_kind": "hand_authored_synthetic_fixtures",
        "case_count": len(case_results),
        "counts": {
            "expected_test_files": total_expected,
            "selected_test_files": total_selected,
            "recalled_test_files": total_recalled,
            "actionable_recalled_test_files": total_actionable_recalled,
            "missed_test_files": total_expected - total_recalled,
            "unnecessary_test_files": sum(len(case["unnecessary_test_files"]) for case in case_results),
            "abstained_cases": abstained_cases,
            "complete_cases": len(case_results) - abstained_cases,
            "abstention_cases_by_reason": dict(sorted(abstention_reason_counts.items())),
        },
        "metrics": {
            "affected_test_recall": total_recalled / total_expected if total_expected else None,
            "actionable_affected_test_recall": (
                total_actionable_recalled / total_expected if total_expected else None
            ),
            "complete_case_recall": (
                total_complete_recalled / total_complete_expected if total_complete_expected else None
            ),
            "selected_test_precision": total_recalled / total_selected if total_selected else None,
            "unnecessary_selection_rate": (
                (total_selected - total_recalled) / total_selected if total_selected else None
            ),
            "abstention_rate": abstained_cases / len(case_results) if case_results else None,
            "analysis_coverage": (len(case_results) - abstained_cases) / len(case_results) if case_results else None,
        },
        "cases": case_results,
        "limitations": [
            "Synthetic fixture results are not real-repository recall estimates.",
            "A miss with an unresolved issue is reported as an abstention, not a successful selection.",
            "This benchmark evaluates test-file selection only; it does not prove semantic impact or correctness.",
        ],
    }


def summarize_import_use_fallback_delta(
    baseline: dict[str, Any],
    fallback_enabled: dict[str, Any],
) -> dict[str, Any]:
    """Separate candidate-generation lift from actionable coverage and noise."""
    baseline_cases = {case["case_id"]: case for case in baseline.get("cases", [])}
    enabled_cases = {case["case_id"]: case for case in fallback_enabled.get("cases", [])}
    if set(baseline_cases) != set(enabled_cases):
        raise ValueError("fallback reports must contain matching case ids")

    case_deltas = []
    totals = {
        "added_candidates": 0,
        "added_expected_candidates": 0,
        "added_actionable_expected_candidates": 0,
        "added_abstained_expected_candidates": 0,
        "added_unnecessary_candidates": 0,
        "removed_candidates": 0,
    }
    for case_id in sorted(enabled_cases):
        before = baseline_cases[case_id]
        after = enabled_cases[case_id]
        before_selected = set(before.get("selected_test_files", []))
        after_selected = set(after.get("selected_test_files", []))
        expected = set(after.get("expected_test_files", []))
        added = after_selected - before_selected
        removed = before_selected - after_selected
        case_abstained = bool(after.get("abstained"))
        added_expected = added & expected
        actionable_expected = set(after.get("actionable_recalled_test_files", []))
        added_actionable = added_expected & actionable_expected
        added_abstained = added_expected - added_actionable
        added_unnecessary = added - expected

        totals["added_candidates"] += len(added)
        totals["added_expected_candidates"] += len(added_expected)
        totals["added_actionable_expected_candidates"] += len(added_actionable)
        totals["added_abstained_expected_candidates"] += len(added_abstained)
        totals["added_unnecessary_candidates"] += len(added_unnecessary)
        totals["removed_candidates"] += len(removed)
        case_deltas.append({
            "case_id": case_id,
            "added_candidates": sorted(added),
            "added_expected_candidates": sorted(added_expected),
            "added_actionable_expected_candidates": sorted(added_actionable),
            "added_abstained_expected_candidates": sorted(added_abstained),
            "added_unnecessary_candidates": sorted(added_unnecessary),
            "removed_candidates": sorted(removed),
            "fallback_case_abstained": case_abstained,
        })

    return {
        "cases": case_deltas,
        "counts": totals,
        "metrics": {
            "actionable_share_of_added_expected": (
                totals["added_actionable_expected_candidates"] / totals["added_expected_candidates"]
                if totals["added_expected_candidates"] else None
            ),
            "abstained_share_of_added_expected": (
                totals["added_abstained_expected_candidates"] / totals["added_expected_candidates"]
                if totals["added_expected_candidates"] else None
            ),
            "unnecessary_share_of_added": (
                totals["added_unnecessary_candidates"] / totals["added_candidates"]
                if totals["added_candidates"] else None
            ),
        },
        "interpretation": (
            "Fallback candidate additions are not synonymous with actionable coverage; "
            "inspect added_abstained_expected_candidates separately."
        ),
    }


def compare_import_use_fallback(
    *,
    max_hops: int = 0,
    cases: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Run the same fixture cohort with the conservative fallback on and off."""
    baseline = run_dependency_impact_benchmark(
        max_hops=max_hops,
        include_import_use_test_fallback=False,
        cases=cases,
    )
    fallback_enabled = run_dependency_impact_benchmark(
        max_hops=max_hops,
        include_import_use_test_fallback=True,
        cases=cases,
    )
    return {
        "analysis_kind": "geo_import_use_fallback_comparison",
        "baseline": baseline,
        "fallback_enabled": fallback_enabled,
        "fallback_delta": summarize_import_use_fallback_delta(baseline, fallback_enabled),
    }


def fallback_import_form_cases() -> tuple[dict[str, Any], ...]:
    """Return a paired used/unused cohort for unresolved provider import forms."""
    return (
        {
            "case_id": "deleted_symbol_alias_used",
            "changed_files": ["src/core/api.py"],
            "expected_test_files": ["tests/test_deleted_symbol.py"],
            "sources": {
                "src/__init__.py": "",
                "src/core/__init__.py": "",
                "src/core/api.py": "def replacement():\n    return 1\n",
                "tests/test_deleted_symbol.py": (
                    "from src.core.api import removed as old_api\n"
                    "def test_old_api():\n    return old_api()\n"
                ),
            },
        },
        {
            "case_id": "deleted_symbol_alias_unused",
            "changed_files": ["src/core/api.py"],
            "expected_test_files": [],
            "sources": {
                "src/__init__.py": "",
                "src/core/__init__.py": "",
                "src/core/api.py": "def replacement():\n    return 1\n",
                "tests/test_unused_symbol.py": (
                    "from src.core.api import removed as old_api\n"
                    "def test_constant():\n    return 4\n"
                ),
            },
        },
        {
            "case_id": "deleted_provider_module_used",
            "changed_files": ["src/core/api.py"],
            "expected_test_files": ["tests/test_deleted_module.py"],
            "sources": {
                "src/__init__.py": "",
                "src/core/__init__.py": "",
                "tests/test_deleted_module.py": (
                    "from src.core.api import run\n"
                    "def test_run():\n    return run()\n"
                ),
            },
        },
        {
            "case_id": "deleted_provider_module_unused",
            "changed_files": ["src/core/api.py"],
            "expected_test_files": [],
            "sources": {
                "src/__init__.py": "",
                "src/core/__init__.py": "",
                "tests/test_unused_module.py": (
                    "from src.core.api import run\n"
                    "def test_constant():\n    return 5\n"
                ),
            },
        },
        {
            "case_id": "relative_provider_removed_used",
            "changed_files": ["app/api.py"],
            "expected_test_files": ["app/tests/test_relative.py"],
            "sources": {
                "app/__init__.py": "",
                "app/tests/__init__.py": "",
                "app/tests/test_relative.py": (
                    "from ..api import removed as old_run\n"
                    "def test_old():\n    return old_run()\n"
                ),
            },
        },
    )


def compare_fallback_import_forms() -> dict[str, Any]:
    """Run fallback off/on on the paired deleted-provider import fixtures."""
    return compare_import_use_fallback(cases=fallback_import_form_cases())


def compare_bidirectional_wave_selection(
    *,
    max_hops: int = 0,
    cases: tuple[dict[str, Any], ...] | None = None,
    split: str = "development",
) -> dict[str, Any]:
    """Compare existing scope with raw and boundary-free wave intersections."""
    if not 0 <= max_hops <= 2:
        raise ValueError("max_hops must be between 0 and 2")
    if not isinstance(split, str) or not split:
        raise ValueError("split must be a non-empty string")
    cohort_cases = _cases() if cases is None else cases
    if not cohort_cases:
        raise ValueError("cases must not be empty")

    methods = {
        "existing_scope": {"selected": 0, "recalled": 0, "expected": 0},
        "wave_meeting": {"selected": 0, "recalled": 0, "expected": 0},
        "actionable_wave_meeting": {"selected": 0, "recalled": 0, "expected": 0},
    }
    case_results = []
    baseline_candidates = []
    wave_candidates = []
    classification_counts: dict[str, int] = {}
    abstained_cases = 0
    for case in cohort_cases:
        with tempfile.TemporaryDirectory(prefix="ain-geo-wave-impact-") as directory:
            root = Path(directory)
            _write_snapshot(root, case["sources"])
            graph = build_dependency_graph(
                root,
                changed_files=case["changed_files"],
                max_hops=case.get("max_hops", max_hops),
                include_import_use_test_fallback=True,
                **case.get("build_options", {}),
            )
        baseline = set(graph["scope"]["linked_test_files"])
        wave = compute_bidirectional_dependency_meetings(
            graph,
            case["changed_files"],
            sorted(baseline),
        )
        wave_by_test = {item["test_file"]: item for item in wave["candidate_tests"]}
        meeting = {
            path for path, result in wave_by_test.items()
            if result["meeting_points"]
        }
        actionable = {
            path for path, result in wave_by_test.items()
            if result["classification"] == "connected_static_path"
        }
        expected = set(case["expected_test_files"])
        methods["existing_scope"]["expected"] += len(expected)
        methods["wave_meeting"]["expected"] += len(expected)
        methods["actionable_wave_meeting"]["expected"] += len(expected)
        for name, selected in (
            ("existing_scope", baseline),
            ("wave_meeting", meeting),
            ("actionable_wave_meeting", actionable),
        ):
            methods[name]["selected"] += len(selected)
            methods[name]["recalled"] += len(selected & expected)
        case_classifications = {
            path: result["classification"]
            for path, result in sorted(wave_by_test.items())
        }
        for classification in case_classifications.values():
            classification_counts[classification] = classification_counts.get(classification, 0) + 1
        case_uncertain = (
            graph["status"] == "incomplete/review"
            or any(
                case_classifications.get(path) in {
                    "connected_with_boundary_evidence",
                    "no_static_meeting_with_boundary",
                    "test_file_missing",
                }
                for path in expected
            )
        )
        abstained_cases += int(case_uncertain)
        case_results.append({
            "case_id": case["case_id"],
            "expected_test_files": sorted(expected),
            "existing_scope_test_files": sorted(baseline),
            "wave_meeting_test_files": sorted(meeting),
            "actionable_wave_test_files": sorted(actionable),
            "candidate_classifications": case_classifications,
            "abstained": case_uncertain,
        })
        for path in sorted(baseline):
            classification = case_classifications.get(path, "no_static_meeting")
            record = {
                "candidate_id": f"{case['case_id']}::{path}",
                "test_file": path,
                "case_id": case["case_id"],
                "affected": path in expected,
                "classification": classification,
            }
            baseline_candidates.append(record)
            wave_candidates.append(record)

    metrics = {}
    for name, counts in methods.items():
        selected = counts["selected"]
        recalled = counts["recalled"]
        expected = counts["expected"]
        metrics[name] = {
            "expected_test_files": expected,
            "selected_test_files": selected,
            "recalled_test_files": recalled,
            "missed_test_files": expected - recalled,
            "unnecessary_test_files": selected - recalled,
            "affected_test_recall": recalled / expected if expected else None,
            "selected_test_precision": recalled / selected if selected else None,
            "unnecessary_selection_rate": (selected - recalled) / selected if selected else None,
        }

    classification_priority = {
        "connected_static_path": 0,
        "connected_with_boundary_evidence": 1,
        "no_static_meeting": 2,
        "no_static_meeting_with_boundary": 3,
        "test_file_missing": 4,
    }
    baseline_ranked = sorted(baseline_candidates, key=lambda item: (item["test_file"], item["case_id"]))
    wave_ranked = sorted(
        wave_candidates,
        key=lambda item: (
            classification_priority.get(item["classification"], 5),
            item["test_file"],
            item["case_id"],
        ),
    )
    random_expected_precision = metrics["existing_scope"]["selected_test_precision"]
    ranking_budgets = []
    for fraction in (0.25, 0.5, 0.75, 1.0):
        budget = min(len(baseline_ranked), math.ceil(len(baseline_ranked) * fraction))
        baseline_top = baseline_ranked[:budget]
        wave_top = wave_ranked[:budget]
        ranking_budgets.append({
            "fraction": fraction,
            "candidate_budget": budget,
            "baseline_path_order": {
                "recalled_affected_tests": sum(item["affected"] for item in baseline_top),
                "recall_over_all_expected": (
                    sum(item["affected"] for item in baseline_top)
                    / methods["existing_scope"]["expected"]
                    if methods["existing_scope"]["expected"] else None
                ),
                "precision_within_budget": (
                    sum(item["affected"] for item in baseline_top) / budget if budget else None
                ),
                "selected_candidates": [item["candidate_id"] for item in baseline_top],
            },
            "wave_priority_order": {
                "recalled_affected_tests": sum(item["affected"] for item in wave_top),
                "recall_over_all_expected": (
                    sum(item["affected"] for item in wave_top)
                    / methods["existing_scope"]["expected"]
                    if methods["existing_scope"]["expected"] else None
                ),
                "precision_within_budget": (
                    sum(item["affected"] for item in wave_top) / budget if budget else None
                ),
                "selected_candidates": [item["candidate_id"] for item in wave_top],
            },
            "random_order_expectation": {
                "expected_recalled_affected_tests": budget * random_expected_precision,
                "expected_recall_over_all_expected": (
                    budget * random_expected_precision / methods["existing_scope"]["expected"]
                    if methods["existing_scope"]["expected"] else None
                ),
                "expected_precision_within_budget": random_expected_precision,
            },
        })
    return {
        "schema_version": 1,
        "analysis_kind": "geo_bidirectional_wave_selection_comparison",
        "cohort_kind": "hand_authored_synthetic_fixtures",
        "split": split,
        "scope_max_hops": max_hops,
        "case_count": len(case_results),
        "metrics": metrics,
        "ranking": {
            "candidate_count": len(baseline_ranked),
            "wave_priority_order": [
                {**item, "rank": rank}
                for rank, item in enumerate(wave_ranked, start=1)
            ],
            "budgets": ranking_budgets,
            "ordering_policy": "connected_static_path, connected_with_boundary_evidence, no_static_meeting, no_static_meeting_with_boundary; ties by test path",
        },
        "counts": {
            "abstained_cases": abstained_cases,
            "abstention_rate": abstained_cases / len(case_results) if case_results else None,
            "candidate_classifications": dict(sorted(classification_counts.items())),
        },
        "cases": case_results,
        "limitations": [
            "This synthetic cohort is not a real-repository estimate.",
            "A static meeting is evidence of a modeled route, not proof of semantic impact.",
            "Boundary-free wave candidates are not independently verified safe or correct.",
            "The comparison measures the precision/recall cost of filtering, not runtime savings.",
            "Priority ranking is exploratory and does not establish review-time savings or general ranking quality.",
        ],
    }


def compare_bidirectional_wave_priority_splits(*, max_hops: int = 0) -> dict[str, Any]:
    """Evaluate the frozen wave ordering separately on development and holdout cases."""
    return {
        "schema_version": 1,
        "analysis_kind": "geo_bidirectional_wave_priority_split_comparison",
        "ranking_policy_frozen_across_splits": True,
        "development": compare_bidirectional_wave_selection(
            max_hops=max_hops,
            cases=_cases(),
            split="development",
        ),
        "evaluation": compare_bidirectional_wave_selection(
            max_hops=max_hops,
            cases=_wave_holdout_cases(),
            split="evaluation",
        ),
        "limitations": [
            "Development and holdout cases are hand-authored synthetic fixtures, not real-repository samples.",
            "The ordering policy is fixed in code and is not fitted to the holdout labels.",
            "Small holdout counts cannot establish general ranking performance or statistical significance.",
        ],
    }


def evaluate_call_chain_boundary(*, max_hops: int = 0) -> dict[str, Any]:
    """Measure the frozen eight-call-hop traversal on a fresh synthetic cohort.

    This is a one-shot exploratory evaluation, not an independently authored
    or statistically powered holdout. Do not tune the implementation against
    these outcomes and continue to label the cohort as consumed afterward.
    """
    if not 0 <= max_hops <= 2:
        raise ValueError("max_hops must be between 0 and 2")
    cases = _call_chain_evaluation_cases()
    case_results = []
    expected_total = selected_total = recalled_total = unnecessary_total = 0
    uncertain_selected = 0
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="ain-geo-call-chain-eval-") as directory:
            root = Path(directory)
            _write_snapshot(root, case["sources"])
            graph = build_dependency_graph(
                root,
                changed_files=case["changed_files"],
                max_hops=case.get("max_hops", max_hops),
                include_import_use_test_fallback=True,
                **case.get("build_options", {}),
            )
        selected = set(graph["scope"]["linked_test_files"])
        expected = set(case["expected_test_files"])
        recalled = selected & expected
        unnecessary = selected - expected
        uncertain = graph["status"] == "incomplete/review" or any(
            issue["kind"] in {
                "unresolved_attribute_call_summary",
                "unresolved_simple_call_summary",
                "test_call_depth_limit_reached",
                "unresolved_local_import",
            }
            for issue in graph["issues"]
        )
        expected_total += len(expected)
        selected_total += len(selected)
        recalled_total += len(recalled)
        unnecessary_total += len(unnecessary)
        uncertain_selected += int(bool(selected & expected) and uncertain)
        case_results.append({
            "case_id": case["case_id"],
            "expected_test_files": sorted(expected),
            "selected_test_files": sorted(selected),
            "recalled_test_files": sorted(recalled),
            "missed_test_files": sorted(expected - selected),
            "unnecessary_test_files": sorted(unnecessary),
            "analysis_status": graph["status"],
            "uncertain": uncertain,
            "uncertainty_issue_kinds": sorted({
                issue["kind"]
                for issue in graph["issues"]
                if issue["kind"] in {
                    "unresolved_attribute_call_summary",
                    "unresolved_simple_call_summary",
                    "unresolved_local_import",
                    "test_call_depth_limit_reached",
                    "parse_error",
                    "file_budget_exceeded",
                    "node_budget_exceeded",
                    "edge_budget_exceeded",
                }
            }),
        })
    return {
        "schema_version": 1,
        "analysis_kind": "geo_call_chain_boundary_evaluation",
        "cohort_kind": "fresh_hand_authored_synthetic_one_shot",
        "cohort_status": "consumed_after_this_run",
        "case_count": len(case_results),
        "call_hop_limit": 8,
        "metrics": {
            "expected_test_files": expected_total,
            "selected_test_files": selected_total,
            "recalled_test_files": recalled_total,
            "missed_test_files": expected_total - recalled_total,
            "unnecessary_test_files": unnecessary_total,
            "affected_test_recall": recalled_total / expected_total if expected_total else None,
            "selected_test_precision": recalled_total / selected_total if selected_total else None,
            "unnecessary_selection_rate": unnecessary_total / selected_total if selected_total else None,
            "selected_expected_cases_with_uncertainty": uncertain_selected,
        },
        "cases": case_results,
        "limitations": [
            "This cohort was authored and evaluated in the same development session; it is not independent evaluation.",
            "The cohort is synthetic and too small to estimate real-repository performance.",
            "Results are exploratory and must not be used to claim generalization or tune another holdout.",
        ],
    }


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-hops", type=int, default=0)
    parser.add_argument("--compare-import-use-fallback", action="store_true")
    parser.add_argument("--compare-fallback-import-forms", action="store_true")
    parser.add_argument("--compare-bidirectional-waves", action="store_true")
    parser.add_argument("--compare-wave-priority-splits", action="store_true")
    parser.add_argument("--evaluate-call-chain-boundary", action="store_true")
    arguments = parser.parse_args()
    selected_modes = sum((
        arguments.compare_import_use_fallback,
        arguments.compare_fallback_import_forms,
        arguments.compare_bidirectional_waves,
        arguments.compare_wave_priority_splits,
        arguments.evaluate_call_chain_boundary,
    ))
    if selected_modes > 1:
        parser.error("choose only one comparison mode")
    if arguments.evaluate_call_chain_boundary:
        result = evaluate_call_chain_boundary(max_hops=arguments.max_hops)
    elif arguments.compare_wave_priority_splits:
        result = compare_bidirectional_wave_priority_splits(max_hops=arguments.max_hops)
    elif arguments.compare_bidirectional_waves:
        result = compare_bidirectional_wave_selection(max_hops=arguments.max_hops)
    elif arguments.compare_fallback_import_forms:
        result = compare_fallback_import_forms()
    elif arguments.compare_import_use_fallback:
        result = compare_import_use_fallback(max_hops=arguments.max_hops)
    else:
        result = run_dependency_impact_benchmark(max_hops=arguments.max_hops)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()