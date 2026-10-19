"""Evaluate GEO against externally authored fixture and oracle-label JSON.

The tool parses source with Python AST and does not execute fixture code. The
fixture input and expected-impact labels are separate, hash-bound documents.
This validates the data boundary; it cannot authenticate who authored labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Mapping

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_geodesic_probe import compute_bidirectional_dependency_meetings


FIXTURE_SCHEMA = "ain.geo-impact-fixtures.v1"
LABEL_SCHEMA = "ain.geo-impact-labels.v1"
CHAT_HASH_PLACEHOLDER = "ASSISTANT_WILL_COMPUTE_FROM_FIXTURE_BLOCK"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_CASES = 100
MAX_FILES_PER_CASE = 1000
MAX_TOTAL_SOURCE_BYTES_PER_CASE = 2 * 1024 * 1024
MAX_CASE_ID_LENGTH = 120
_GLOBAL_INCOMPLETE_KINDS = {
    "edge_budget_exceeded",
    "file_budget_exceeded",
    "node_budget_exceeded",
    "parse_error",
    "snapshot_changed_before_cache_return",
    "snapshot_changed_before_parse",
    "snapshot_changed_during_read",
    "snapshot_read_error",
}
_LOCAL_ISSUE_KINDS = {
    "test_call_depth_limit_reached",
    "unresolved_local_import",
}
_SUMMARY_ISSUE_KINDS = {
    "unresolved_attribute_call_summary",
    "unresolved_simple_call_summary",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _read_document(path: str | Path, *, max_bytes: int = MAX_INPUT_BYTES) -> tuple[Any, bytes]:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read input: {type(error).__name__}") from error
    if len(raw) > max_bytes:
        raise ValueError("input exceeds configured byte limit")
    value = _parse_document_bytes(raw)
    return value, raw


def _parse_document_bytes(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("input must be strict UTF-8 JSON") from error


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        first_newline = value.find("\n")
        if first_newline < 0 or not value.endswith("```"):
            raise ValueError("fenced chat JSON must have an opening and closing code fence")
        value = value[first_newline + 1:-3].strip()
    return value


def evaluate_external_chat_blocks(fixture_text: str, label_text: str) -> dict[str, Any]:
    """Evaluate the two JSON blocks pasted in chat without requiring input files."""
    if not isinstance(fixture_text, str) or not isinstance(label_text, str):
        raise ValueError("chat fixture and label blocks must be strings")
    fixture_bytes = _strip_json_fence(fixture_text).encode("utf-8")
    label_bytes = _strip_json_fence(label_text).encode("utf-8")
    if len(fixture_bytes) > MAX_INPUT_BYTES or len(label_bytes) > MAX_INPUT_BYTES:
        raise ValueError("chat JSON block exceeds configured byte limit")
    fixture_document = _parse_document_bytes(fixture_bytes)
    label_document = _parse_document_bytes(label_bytes)
    fixture_hash = hashlib.sha256(fixture_bytes).hexdigest()
    if (
        isinstance(label_document, Mapping)
        and label_document.get("fixtures_sha256") == CHAT_HASH_PLACEHOLDER
    ):
        label_document = {**label_document, "fixtures_sha256": fixture_hash}
    return evaluate_external_documents(fixture_document, fixture_bytes, label_document)


def _safe_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError(f"{field} must be a non-empty POSIX-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must not escape the fixture root")
    normalized = path.as_posix()
    if not normalized.endswith(".py"):
        raise ValueError(f"{field} must name a Python .py file")
    return normalized


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CASE_ID_LENGTH:
        raise ValueError(f"{field} must be a non-empty string of at most {MAX_CASE_ID_LENGTH} characters")
    return value


def _validate_fixture_document(document: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(document, Mapping) or set(document) != {"schema_version", "cohort_id", "cases"}:
        raise ValueError("fixture document must contain exactly schema_version, cohort_id, and cases")
    if document["schema_version"] != FIXTURE_SCHEMA:
        raise ValueError("unsupported GEO fixture schema")
    cohort_id = _identifier(document["cohort_id"], "cohort_id")
    cases = document["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise ValueError(f"cases must contain 1..{MAX_CASES} entries")
    validated = []
    seen_ids = set()
    for case in cases:
        if not isinstance(case, Mapping) or set(case) - {"case_id", "changed_files", "sources", "max_hops"}:
            raise ValueError("each fixture case contains unsupported fields")
        if not {"case_id", "changed_files", "sources"} <= set(case):
            raise ValueError("each fixture case requires case_id, changed_files, and sources")
        case_id = _identifier(case["case_id"], "case_id")
        if case_id in seen_ids:
            raise ValueError("fixture case ids must be unique")
        seen_ids.add(case_id)
        changed_files = case["changed_files"]
        if not isinstance(changed_files, list) or not changed_files:
            raise ValueError("changed_files must be a non-empty list")
        changed_files = [_safe_relative_path(path, "changed file") for path in changed_files]
        if len(set(changed_files)) != len(changed_files):
            raise ValueError("changed_files must be unique")
        sources = case["sources"]
        if not isinstance(sources, Mapping) or not sources or len(sources) > MAX_FILES_PER_CASE:
            raise ValueError("sources must be a non-empty mapping within the file limit")
        normalized_sources = {}
        total_bytes = 0
        for path, source in sources.items():
            relative = _safe_relative_path(path, "source path")
            if not isinstance(source, str):
                raise ValueError("source contents must be strings")
            total_bytes += len(source.encode("utf-8"))
            if total_bytes > MAX_TOTAL_SOURCE_BYTES_PER_CASE:
                raise ValueError("fixture source bytes exceed the per-case limit")
            normalized_sources[relative] = source
        max_hops = case.get("max_hops", 0)
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not 0 <= max_hops <= 2:
            raise ValueError("max_hops must be an integer from 0 to 2")
        validated.append({
            "case_id": case_id,
            "changed_files": changed_files,
            "sources": normalized_sources,
            "max_hops": max_hops,
        })
    return cohort_id, validated


def _validate_label_document(document: Any, *, cohort_id: str, fixture_sha256: str, case_ids: set[str]) -> dict[str, list[str]]:
    required = {"schema_version", "cohort_id", "fixtures_sha256", "labels"}
    if not isinstance(document, Mapping) or set(document) != required:
        raise ValueError("label document must contain exactly schema_version, cohort_id, fixtures_sha256, and labels")
    if document["schema_version"] != LABEL_SCHEMA:
        raise ValueError("unsupported GEO oracle-label schema")
    if document["cohort_id"] != cohort_id:
        raise ValueError("fixture and label cohort_id values differ")
    if document["fixtures_sha256"] != fixture_sha256:
        raise ValueError("label document does not bind the supplied fixture bytes")
    labels = document["labels"]
    if not isinstance(labels, list) or len(labels) != len(case_ids):
        raise ValueError("labels must contain exactly one row per fixture case")
    expected = {}
    for row in labels:
        if not isinstance(row, Mapping) or set(row) != {"case_id", "expected_test_files"}:
            raise ValueError("each label row must contain exactly case_id and expected_test_files")
        case_id = _identifier(row["case_id"], "label case_id")
        if case_id not in case_ids or case_id in expected:
            raise ValueError("label case ids must uniquely match fixture case ids")
        paths = row["expected_test_files"]
        if not isinstance(paths, list):
            raise ValueError("expected_test_files must be a list")
        normalized = [_safe_relative_path(path, "expected test file") for path in paths]
        if len(set(normalized)) != len(normalized):
            raise ValueError("expected_test_files must be unique within a label row")
        expected[case_id] = sorted(normalized)
    if set(expected) != case_ids:
        raise ValueError("labels must exactly cover the fixture case ids")
    return expected


def _case_uncertainty(
    graph: Mapping[str, Any],
) -> tuple[set[str], bool, dict[str, list[str]], set[str]]:
    causes = graph.get("incomplete_causes", {})
    global_causes = causes.get("global", []) if isinstance(causes, Mapping) else []
    scope_causes = causes.get("scope", []) if isinstance(causes, Mapping) else []
    global_uncertain = bool(global_causes)
    path_reasons: dict[str, list[str]] = {}
    for issue in scope_causes:
        if not isinstance(issue, Mapping):
            global_uncertain = True
            continue
        kind = issue.get("kind", "unknown_scope_incomplete")
        path = issue.get("path")
        if kind == "test_call_depth_limit_reached" and isinstance(path, str):
            path_reasons.setdefault(path, []).append(kind)
        else:
            global_uncertain = True

    all_selected_uncertain = False
    scope_uncertainty_paths = set()
    scope_paths = set(graph.get("scope", {}).get("files", []))
    for issue in graph.get("issues", []):
        if not isinstance(issue, Mapping):
            global_uncertain = True
            continue
        kind = issue.get("kind")
        if kind in _GLOBAL_INCOMPLETE_KINDS:
            global_uncertain = True
        elif kind == "unresolved_local_import":
            path = issue.get("path")
            if isinstance(path, str):
                path_reasons.setdefault(path, []).append(kind)
                if path in scope_paths:
                    scope_uncertainty_paths.add(path)
            else:
                all_selected_uncertain = True
        elif kind in _SUMMARY_ISSUE_KINDS:
            examples = issue.get("examples", [])
            if isinstance(examples, list):
                for example in examples:
                    if isinstance(example, Mapping) and isinstance(example.get("path"), str):
                        path_reasons.setdefault(example["path"], []).append(kind)
                        if example["path"] in scope_paths:
                            scope_uncertainty_paths.add(example["path"])
            if not isinstance(examples, list) or issue.get("count", 0) > len(examples):
                all_selected_uncertain = True
    if graph.get("status") == "incomplete/review" and not global_causes and not scope_causes:
        global_uncertain = True
    return (
        {path for path in path_reasons},
        global_uncertain or all_selected_uncertain,
        path_reasons,
        scope_uncertainty_paths,
    )


def _known_call_closure(graph: Mapping[str, Any], test_file: str) -> set[str]:
    """Return test/function symbols reachable through known call edges only."""
    nodes = {
        node.get("id"): node
        for node in graph.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    }
    calls_by_source: dict[str, list[str]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, Mapping) or edge.get("type") != "calls":
            continue
        source, target = edge.get("source"), edge.get("target")
        if source in nodes and target in nodes:
            calls_by_source.setdefault(source, []).append(target)
    pending = [
        node_id for node_id, node in nodes.items()
        if node.get("type") == "test" and node.get("path") == test_file
    ]
    reachable = set()
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(calls_by_source.get(current, []))
    return reachable


def _evaluate_case(case: Mapping[str, Any], fallback_enabled: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ain-geo-external-case-") as directory:
        root = Path(directory)
        for relative, source in case["sources"].items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        graph = build_dependency_graph(
            root,
            changed_files=list(case["changed_files"]),
            max_hops=case["max_hops"],
            include_import_use_test_fallback=fallback_enabled,
        )
    local_uncertain, global_uncertain, path_reasons, _scope_uncertainty_paths = _case_uncertainty(graph)
    selected = sorted(graph["scope"]["linked_test_files"])
    wave = compute_bidirectional_dependency_meetings(
        graph,
        list(case["changed_files"]),
        selected,
    )
    nodes = {
        node["id"]: node
        for node in graph["nodes"]
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    }
    wave_by_test = {item["test_file"]: item for item in wave["candidate_tests"]}
    graph_issues = graph.get("issues", [])
    candidate_records = []
    for path in selected:
        wave_result = wave_by_test.get(path, {})
        route_node_ids = set()
        for meeting in wave_result.get("meeting_points", []):
            route_node_ids.update(meeting.get("forward_path", []))
            route_node_ids.update(meeting.get("backward_path_from_test", []))
        route_paths = {
            nodes[node_id].get("path")
            for node_id in route_node_ids
            if node_id in nodes and isinstance(nodes[node_id].get("path"), str)
        }
        route_paths.update(case["changed_files"])
        route_paths.add(path)

        reasons = set(path_reasons.get(path, []))
        call_closure = _known_call_closure(graph, path)
        for issue in graph_issues:
            if not isinstance(issue, Mapping) or issue.get("kind") not in _SUMMARY_ISSUE_KINDS:
                continue
            examples = issue.get("examples", [])
            matching_examples = [
                example for example in examples
                if isinstance(example, Mapping)
                and isinstance(example.get("source"), str)
                and example["source"] in call_closure
            ] if isinstance(examples, list) else []
            if matching_examples or not isinstance(examples, list) or issue.get("count", 0) > len(examples):
                reasons.add(issue["kind"])
        for boundary in wave_result.get("boundary_evidence", []):
            kind = boundary.get("kind")
            if kind in _SUMMARY_ISSUE_KINDS:
                continue
            if kind == "test_call_depth_limit_reached":
                if boundary.get("path") == path:
                    reasons.add(kind)
                continue
            if kind in _GLOBAL_INCOMPLETE_KINDS:
                reasons.add(kind)
                continue
            if kind == "unresolved_local_import":
                if boundary.get("path") in route_paths:
                    reasons.add(kind)
                continue
            examples = boundary.get("examples_in_scope", [])
            matching_examples = []
            for example in examples:
                source_id = example.get("source") if isinstance(example, Mapping) else None
                example_path = example.get("path") if isinstance(example, Mapping) else None
                if source_id in route_node_ids or (source_id is None and example_path in route_paths):
                    matching_examples.append(example)
            if matching_examples:
                reasons.add(kind)
            elif boundary.get("example_count_is_lower_bound"):
                reasons.add(kind)

        if global_uncertain:
            reasons.add("global_analysis_incomplete")
        is_uncertain = global_uncertain or bool(reasons)
        candidate_records.append({
            "test_file": path,
            "recommendation": "run_candidate",
            "evidence_status": "review_required_boundary_present" if is_uncertain else "no_known_boundary",
            "uncertain": is_uncertain,
            "uncertainty_reasons": sorted(reasons),
            "meeting_node_ids": sorted({
                meeting.get("node_id")
                for meeting in wave_result.get("meeting_points", [])
                if isinstance(meeting.get("node_id"), str)
            }),
        })
    return {
        "analysis_status": graph["status"],
        "global_uncertain": global_uncertain,
        "candidate_test_files": selected,
        "recommended_test_files": selected,
        "review_required_test_files": [
            row["test_file"] for row in candidate_records if row["uncertain"]
        ],
        "no_known_boundary_test_files": [
            row["test_file"] for row in candidate_records if not row["uncertain"]
        ],
        "candidate_uncertainty": candidate_records,
        "wave_classifications": [
            {"test_file": item["test_file"], "classification": item["classification"]}
            for item in wave["candidate_tests"]
        ],
        "incomplete_causes": graph.get("incomplete_causes", {}),
    }


def evaluate_external_documents(fixture_document: Any, fixture_bytes: bytes, label_document: Any) -> dict[str, Any]:
    """Score separate external fixture/label documents without executing source."""
    if _parse_document_bytes(fixture_bytes) != fixture_document:
        raise ValueError("fixture mapping does not match the supplied fixture bytes")
    cohort_id, cases = _validate_fixture_document(fixture_document)
    fixtures_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    case_ids = {case["case_id"] for case in cases}
    labels = _validate_label_document(
        label_document,
        cohort_id=cohort_id,
        fixture_sha256=fixtures_sha256,
        case_ids=case_ids,
    )

    modes = {}
    for fallback_enabled in (False, True):
        selected_total = expected_total = recalled_total = actionable_total = 0
        unnecessary_total = abstained_expected = 0
        per_case = []
        for case in cases:
            graph_result = _evaluate_case(case, fallback_enabled)
            expected = set(labels[case["case_id"]])
            selected = set(graph_result["candidate_test_files"])
            recommended = set(graph_result["recommended_test_files"])
            uncertain = {
                row["test_file"] for row in graph_result["candidate_uncertainty"]
                if row["uncertain"]
            }
            recalled = selected & expected
            actionable = recalled - uncertain
            missed = expected - selected
            unnecessary = selected - expected
            depth_paths = {
                issue.get("path")
                for issue in graph_result["incomplete_causes"].get("scope", [])
                if issue.get("kind") == "test_call_depth_limit_reached"
            }
            expected_abstained = (
                expected
                if graph_result["global_uncertain"]
                else (expected & uncertain) | (expected & depth_paths)
            )
            selected_total += len(selected)
            expected_total += len(expected)
            recalled_total += len(recalled)
            actionable_total += len(actionable)
            unnecessary_total += len(unnecessary)
            abstained_expected += len(expected_abstained)
            per_case.append({
                "case_id": case["case_id"],
                "expected_test_files": sorted(expected),
                **graph_result,
                "recommended_recalled_test_files": sorted(recommended & expected),
                "recalled_test_files": sorted(recalled),
                "actionable_recalled_test_files": sorted(actionable),
                "missed_test_files": sorted(missed),
                "unnecessary_test_files": sorted(unnecessary),
                "abstained_expected_test_files": sorted(expected_abstained),
            })
        modes["fallback_enabled" if fallback_enabled else "fallback_disabled"] = {
            "metrics": {
                "expected_test_files": expected_total,
                "selected_test_files": selected_total,
                "recalled_test_files": recalled_total,
                "actionable_recalled_test_files": actionable_total,
                "missed_test_files": expected_total - recalled_total,
                "unnecessary_test_files": unnecessary_total,
                "expected_test_abstentions": abstained_expected,
                "affected_test_recall": recalled_total / expected_total if expected_total else None,
                "recommended_affected_recall": recalled_total / expected_total if expected_total else None,
                "actionable_affected_recall": actionable_total / expected_total if expected_total else None,
                "verified_under_static_evidence_recall": actionable_total / expected_total if expected_total else None,
                "selected_test_precision": recalled_total / selected_total if selected_total else None,
                "unnecessary_selection_rate": unnecessary_total / selected_total if selected_total else None,
                "expected_test_abstention_rate": abstained_expected / expected_total if expected_total else None,
            },
            "cases": per_case,
        }

    fallback_delta_cases = []
    delta_counts = {
        "added_recommended_candidates": 0,
        "added_expected_candidates": 0,
        "added_actionable_expected_candidates": 0,
        "added_abstained_expected_candidates": 0,
        "added_unnecessary_candidates": 0,
        "removed_recommendations": 0,
    }
    baseline_cases = {case["case_id"]: case for case in modes["fallback_disabled"]["cases"]}
    enabled_cases = {case["case_id"]: case for case in modes["fallback_enabled"]["cases"]}
    for case_id in sorted(baseline_cases):
        before = baseline_cases[case_id]
        after = enabled_cases[case_id]
        before_recommended = set(before["recommended_test_files"])
        after_recommended = set(after["recommended_test_files"])
        expected = set(after["expected_test_files"])
        added = after_recommended - before_recommended
        removed = before_recommended - after_recommended
        added_expected = added & expected
        actionable = set(after["actionable_recalled_test_files"])
        added_actionable = added_expected & actionable
        added_abstained = added_expected - added_actionable
        added_unnecessary = added - expected
        delta_counts["added_recommended_candidates"] += len(added)
        delta_counts["added_expected_candidates"] += len(added_expected)
        delta_counts["added_actionable_expected_candidates"] += len(added_actionable)
        delta_counts["added_abstained_expected_candidates"] += len(added_abstained)
        delta_counts["added_unnecessary_candidates"] += len(added_unnecessary)
        delta_counts["removed_recommendations"] += len(removed)
        fallback_delta_cases.append({
            "case_id": case_id,
            "added_recommended_candidates": sorted(added),
            "added_expected_candidates": sorted(added_expected),
            "added_actionable_expected_candidates": sorted(added_actionable),
            "added_abstained_expected_candidates": sorted(added_abstained),
            "added_unnecessary_candidates": sorted(added_unnecessary),
            "removed_recommendations": sorted(removed),
        })
    fallback_delta = {
        "counts": delta_counts,
        "cases": fallback_delta_cases,
        "interpretation": (
            "Fallback-only recommendations are reported separately from actionable coverage; "
            "a boundary-marked recommendation remains a run candidate requiring review."
        ),
    }

    return {
        "schema_version": 1,
        "analysis_kind": "geo_external_labeled_impact_evaluation",
        "cohort_kind": "externally_supplied_synthetic_or_repository_fixtures",
        "cohort_id": cohort_id,
        "fixtures_sha256": fixtures_sha256,
        "labels_sha256": hashlib.sha256(json.dumps(label_document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "case_count": len(cases),
        "label_producer_authenticated": False,
        "fixture_author_independence_verified": False,
        "fixture_sources_executed": False,
        "one_shot_claim_enforced": False,
        "fallback_comparison": modes,
        "fallback_delta": fallback_delta,
        "limitations": [
            "The tool cannot authenticate fixture or oracle-label authorship/independence.",
            "A holdout label is meaningful only if frozen before code is exposed to its author and before results are inspected.",
            "Python source fixtures are parsed by GEO but never executed by this tool.",
            "The report is not proof of semantic impact or correctness.",
        ],
    }


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def create_label_template(fixture_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Create a new empty oracle-label file bound to exact fixture bytes."""
    fixture_document, fixture_bytes = _read_document(fixture_path)
    cohort_id, cases = _validate_fixture_document(fixture_document)
    output = Path(output_path)
    if output.resolve() == Path(fixture_path).resolve():
        raise ValueError("label output path must differ from fixture input")
    template = {
        "schema_version": LABEL_SCHEMA,
        "cohort_id": cohort_id,
        "fixtures_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "labels": [
            {"case_id": case["case_id"], "expected_test_files": []}
            for case in cases
        ],
    }
    _write_new_json(output, template)
    return {
        "output": str(output),
        "cohort_id": cohort_id,
        "case_count": len(cases),
        "fixtures_sha256": template["fixtures_sha256"],
        "labels_filled": False,
        "oracle_independent_authenticated": False,
    }


def evaluate_external_files(fixture_path: str | Path, label_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    fixture_file = Path(fixture_path)
    label_file = Path(label_path)
    output_file = Path(output_path)
    input_paths = {fixture_file.resolve(), label_file.resolve()}
    if output_file.resolve() in input_paths:
        raise ValueError("output path must differ from both inputs")
    fixture_document, fixture_bytes = _read_document(fixture_file)
    label_document, _label_bytes = _read_document(label_file)
    report = evaluate_external_documents(fixture_document, fixture_bytes, label_document)
    _write_new_json(output_file, report)
    return {"output": str(output_file), "report": report}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--prepare-labels",
        action="store_true",
        help="create an empty oracle-label template bound to the exact fixture bytes",
    )
    args = parser.parse_args()
    try:
        if args.prepare_labels:
            if args.labels is not None:
                parser.error("--labels cannot be used with --prepare-labels")
            result = create_label_template(args.fixtures, args.output)
        else:
            if args.labels is None:
                parser.error("--labels is required unless --prepare-labels is used")
            result = evaluate_external_files(args.fixtures, args.labels, args.output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()