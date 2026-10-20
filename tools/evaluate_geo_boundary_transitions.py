"""Score boundary-transition accounting on synthetic, developer-authored labels."""

import argparse
import json
from pathlib import Path
import tempfile

from tools.code_dependency_graph import build_dependency_graph
from tools.dependency_impact_benchmark import fallback_import_form_cases
from tools.geo_boundary_transition_probe import analyze_boundary_transitions


def boundary_transition_cases():
    cases = list(fallback_import_form_cases())
    for count, fallback in ((7, False), (8, False), (8, True)):
        helpers = "from pkg.core import run\n"
        for index in range(count):
            target = f"helper_{index + 1}()" if index + 1 < count else "run()"
            helpers += f"def helper_{index}():\n    return {target}\n"
        cases.append({
            "case_id": f"chain_{count + 1}_calls_fallback_{fallback}",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": ["tests/test_chain.py"],
            "sources": {
                "pkg/core.py": "def run():\n    return 1\n",
                "pkg/helpers.py": helpers,
                "tests/test_chain.py": (
                    "from pkg.helpers import helper_0\n"
                    + ("from pkg.core import run\n" if fallback else "")
                    + "def test_chain():\n    helper_0()\n"
                    + ("    callback = run\n" if fallback else "")
                ),
            },
        })
    cases.extend((
        {
            "case_id": "metadata_only_name_reference_noise",
            "changed_files": ["pkg/core.py"],
            # This contract concerns value/behavior changes to run, not its name.
            # The test only checks function metadata; fallback still recommends it.
            "expected_test_files": [],
            "sources": {
                "pkg/core.py": "def run():\n    return 1\n",
                "tests/test_name.py": (
                    "from pkg.core import run\n"
                    "def test_name():\n    assert run.__name__ == 'run'\n"
                ),
            },
        },
        {
            "case_id": "dynamic_inside_known_provider",
            "changed_files": ["pkg/core.py"],
            "expected_test_files": ["tests/test_core.py"],
            "sources": {
                "pkg/core.py": "def run(plugin):\n    return plugin.dispatch()\n",
                "tests/test_core.py": (
                    "from pkg.core import run\n"
                    "def test_run(plugin):\n    return run(plugin)\n"
                ),
            },
        },
    ))
    return tuple(cases)


def _selection_score(selected, expected):
    return {
        "selected": sorted(selected),
        "recalled": sorted(selected & expected),
        "missed": sorted(expected - selected),
        "unnecessary": sorted(selected - expected),
    }


def run_boundary_transition_evaluation(*, extra_call_hops=0, max_extra_visits=1000):
    rows = []
    totals = {
        mode: {"selected": 0, "expected": 0, "recalled": 0, "missed": 0, "unnecessary": 0}
        for mode in ("static", "fallback", "continuation")
    }
    lost_boundaries = 0
    cleared_candidates = 0
    promoted_candidates = 0
    for case in boundary_transition_cases():
        with tempfile.TemporaryDirectory(prefix="geo-boundary-eval-") as directory:
            root = Path(directory)
            for path, source in case["sources"].items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
            graphs = [
                build_dependency_graph(
                    root, changed_files=case["changed_files"], max_hops=0,
                    include_import_use_test_fallback=enabled,
                ) for enabled in (False, True)
            ]
        probe = analyze_boundary_transitions(
            *graphs, extra_call_hops=extra_call_hops, max_extra_visits=max_extra_visits,
        )
        expected = set(case["expected_test_files"])
        selections = [set(graph["scope"]["linked_test_files"]) for graph in graphs]
        selections.append({row["test_file"] for row in probe["candidate_tests"]})
        scored = {}
        for mode, selected in zip(("static", "fallback", "continuation"), selections):
            scored[mode] = _selection_score(selected, expected)
            totals[mode]["expected"] += len(expected)
            for key in ("selected", "recalled", "missed", "unnecessary"):
                totals[mode][key] += len(scored[mode][key])
        added = selections[1] - selections[0]
        expected_boundaries = [
            issue for issue in graphs[0]["issues"]
            if issue["kind"] == "test_call_depth_limit_reached"
        ]
        # Compare the actual retained records, not the probe's declared counters.
        remaining = list(probe["boundary_entries"])
        case_lost = 0
        for boundary in expected_boundaries:
            if boundary in remaining:
                remaining.remove(boundary)
            else:
                case_lost += 1
        lost_boundaries += case_lost
        for candidate in probe["candidate_tests"]:
            local_depth = any(
                issue["path"] == candidate["test_file"] for issue in expected_boundaries
            )
            if local_depth and not candidate["boundary_preserved"]:
                cleared_candidates += 1
            if (
                candidate["fallback_added"] or candidate["boundary_encountered"]
            ) and candidate["evidence_status"] != "review_required":
                promoted_candidates += 1
        rows.append({
            "case_id": case["case_id"],
            "expected_test_files": sorted(expected),
            **scored,
            "added_expected_candidates": sorted(added & expected),
            "added_unnecessary_candidates": sorted(added - expected),
            "lost_depth_boundaries": case_lost,
            "probe": probe,
            "continuation_added_expected_candidates": sorted((selections[2] - selections[1]) & expected),
            "continuation_added_unnecessary_candidates": sorted((selections[2] - selections[1]) - expected),
        })
    for counts in totals.values():
        counts["precision"] = counts["recalled"] / counts["selected"] if counts["selected"] else None
        counts["recall"] = counts["recalled"] / counts["expected"] if counts["expected"] else None
    return {
        "schema_version": 1,
        "analysis_kind": "synthetic_boundary_transition_evaluation",
        "cohort_kind": "developer_authored_synthetic_regression_cases",
        "labels_independent": False,
        "fixture_code_executed": False,
        "case_count": len(rows),
        "methods": totals,
        "added_expected_candidates": sum(len(row["added_expected_candidates"]) for row in rows),
        "added_unnecessary_candidates": sum(len(row["added_unnecessary_candidates"]) for row in rows),
        "continuation_added_expected_candidates": sum(
            len(row["continuation_added_expected_candidates"]) for row in rows
        ),
        "continuation_added_unnecessary_candidates": sum(
            len(row["continuation_added_unnecessary_candidates"]) for row in rows
        ),
        "continuation_visits": sum(row["probe"]["continuation"]["visits"] for row in rows),
        "continuation_peak_queue_size": max(
            (row["probe"]["continuation"]["peak_queue_size"] for row in rows), default=0,
        ),
        "continuation_search_status_counts": {
            status: sum(
                search["search_status"] == status
                for row in rows for search in row["probe"]["continuation"]["boundary_searches"]
            )
            for status in (
                "known_call_witness_found", "known_calls_exhausted",
                "hop_budget_exhausted", "visit_budget_exhausted",
            )
        },
        "invariants": {
            "lost_depth_boundaries": lost_boundaries,
            "cleared_depth_boundary_candidates": cleared_candidates,
            "unsafe_evidence_promotions": promoted_candidates,
        },
        "cases": rows,
        "limitations": [
            "Labels are developer-authored scenario contracts, not independent semantic impact measurements.",
            "The metadata-only negative concerns changes to return behavior, not function names or existence.",
            "Deleted-provider positives denote review candidates for broken imports, not executable passing tests.",
            "Counts aggregate case/test occurrences, not unique test paths or independent observations.",
            "The continuation method adds budgeted known-call candidates only when explicitly enabled; defaults match existing fallback.",
            "No effectiveness, runtime probability, or real-project generalization is established.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra-call-hops", type=int, default=0)
    parser.add_argument("--max-extra-visits", type=int, default=1000)
    args = parser.parse_args()
    try:
        result = run_boundary_transition_evaluation(
            extra_call_hops=args.extra_call_hops, max_extra_visits=args.max_extra_visits,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
