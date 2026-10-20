"""Experimental evidence-layer transitions, not a geometric or probability model."""

import argparse
from collections import defaultdict, deque
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

from tools.code_dependency_graph import build_dependency_graph
from tools.geo_geodesic_probe import compute_bidirectional_dependency_meetings
from tools.geo_history_tension_probe import _read_cli_document


def _changed_symbol_context(graph, ast_delta):
    if not isinstance(ast_delta, Mapping):
        raise ValueError("ast_delta must be a mapping")
    symbols = ast_delta.get("symbols")
    if not isinstance(symbols, Mapping):
        raise ValueError("ast_delta requires symbols")
    changes = set(graph["scope"]["changed_files"])
    changed_ids = set()
    for category in ("added", "changed", "removed"):
        entries = symbols.get(category)
        if not isinstance(entries, list):
            raise ValueError(f"ast_delta symbols.{category} must be a list")
        for entry in entries:
            symbol_id = entry.get("symbol_id") if isinstance(entry, Mapping) else None
            if not isinstance(symbol_id, str) or len(symbol_id.split(":", 2)) != 3:
                raise ValueError("AST symbols require a qualified symbol_id")
            if symbol_id.split(":", 2)[1] in changes:
                changed_ids.add(symbol_id)
    uncertain = ast_delta.get("uncertain_files")
    issues = ast_delta.get("issues")
    if (
        not isinstance(uncertain, list) or any(not isinstance(path, str) for path in uncertain)
        or not isinstance(issues, list) or any(not isinstance(issue, Mapping) for issue in issues)
    ):
        raise ValueError("ast_delta requires uncertain_files and issues lists")
    partial = ast_delta.get("partially_uncertain_files", [])
    ambiguous = ast_delta.get("uncertain_symbols", [])
    if (
        not isinstance(partial, list) or any(not isinstance(path, str) for path in partial)
        or not isinstance(ambiguous, list)
        or any(not isinstance(symbol, str) or len(symbol.split(":", 2)) != 3 for symbol in ambiguous)
    ):
        raise ValueError("partial AST uncertainty requires path and symbol ID lists")
    changed_ids.difference_update(ambiguous)
    descendant_changes = {
        symbol_id: sorted(
            other for other in changed_ids
            if other != symbol_id
            and other.split(":", 2)[1] == symbol_id.split(":", 2)[1]
            and other.split(":", 2)[2].startswith(symbol_id.split(":", 2)[2] + ".")
        )
        for symbol_id in changed_ids
        if symbol_id.startswith(("class:", "function:"))
    }
    # Only existing, direct symbol edges qualify; no guessed alias or dynamic resolution.
    nodes = {node["id"]: node for node in graph["nodes"]}
    direct = defaultdict(set)
    for edge in graph["edges"]:
        if edge["target"] in changed_ids and edge["type"] in {"calls", "type_depends_on", "reads", "writes"}:
            source = nodes.get(edge["source"], {})
            if source.get("type") == "test":
                direct[source["path"]].add((edge["target"], edge["type"]))
    relevant_issues = any(
        (issue.get("path") is None or issue.get("path") in changes)
        and not (
            issue.get("kind") == "duplicate_symbol_id"
            and issue.get("path") in partial
            and issue.get("symbol_id") in ambiguous
        )
        for issue in issues
    )
    return changed_ids, direct, bool(
        relevant_issues or (set(uncertain) - set(partial)) & changes
    ), bool(set(partial) & changes), descendant_changes


def _symbol_alignment(path, evidence, context):
    changed_ids, direct, uncertain, partial, descendant_changes = context
    matches = sorted(direct.get(path, set()))
    details = []
    for symbol, relation in matches:
        kind = symbol.split(":", 1)[0]
        reference_kind = (
            "changed_function_call" if relation == "calls" and kind == "function" else
            "changed_variable_read" if relation == "reads" and kind == "global" else
            "changed_variable_write" if relation == "writes" and kind == "global" else
            "changed_class_type_reference" if relation == "type_depends_on" and kind == "class" else
            "changed_class_call" if relation == "calls" and kind == "class" else
            "other_direct_symbol_reference"
        )
        descendants = descendant_changes.get(symbol, [])
        details.append({
            "symbol_id": symbol,
            "relation": relation,
            "reference_kind": reference_kind,
            "changed_descendant_symbol_ids": descendants,
            "container_ast_includes_changed_descendants": bool(descendants),
            "reference_to_changed_descendant_proven": False,
        })
    if matches:
        classification = "direct_symbol_reference"
        reason = "existing_test_symbol_edge_matches_changed_symbol_id"
    elif uncertain or not changed_ids:
        classification = "undetermined"
        reason = "uncertain_or_no_changed_symbol_summary"
    elif not evidence or evidence["truncated"]:
        classification = "undetermined"
        reason = "missing_or_truncated_import_reference_evidence"
    elif any(
        kind in {"annotation", "cast_type_argument_syntax", "dynamic_attribute_syntax"}
        for record in evidence["records"] for kind in record["reference_kinds"]
    ):
        classification = "undetermined"
        reason = "type_or_dynamic_reference_requires_further_resolution"
    else:
        classification = "file_level_relation"
        reason = "import_use_match_without_direct_changed_symbol_edge"
    return {
        "classification": classification,
        "reason": reason,
        "direct_matches": [{"symbol_id": symbol, "relation": relation} for symbol, relation in matches],
        "direct_match_details": details,
        "direct_reference_kinds": sorted({detail["reference_kind"] for detail in details}),
        "changed_symbol_count": len(changed_ids),
        "delta_uncertain": uncertain,
        "delta_partially_uncertain": partial,
        "runtime_impact_proven": False,
    }


def _continue_boundaries(
    graph: Mapping[str, Any], boundaries: list[dict[str, Any]],
    extra_hops: int, max_visits: int,
) -> dict[str, Any]:
    nodes = {node["id"]: node for node in graph["nodes"]}
    calls = defaultdict(set)
    for edge in graph["edges"]:
        if edge["type"] == "calls":
            calls[edge["source"]].add(edge["target"])
    scoped_paths = set(graph["scope"]["distances"])
    visits = 0
    peak_queue = 0
    results = []
    for boundary in boundaries:
        terminal = boundary.get("terminal_node")
        target = boundary.get("untraversed_target")
        if (
            not isinstance(boundary.get("path"), str)
            or terminal not in nodes or target not in nodes
            or target not in calls[terminal]
        ):
            raise ValueError("depth boundary must identify an existing known call edge and test path")
        start_visits = visits
        pending = deque([(target, [terminal, target], 1)]) if visits < max_visits else deque()
        search_peak_queue = len(pending)
        peak_queue = max(peak_queue, search_peak_queue)
        visited = {terminal}
        witness = None
        hop_limited = False
        visit_limited = visits >= max_visits
        while pending:
            if visits >= max_visits:
                visit_limited = True
                break
            current, route, hops = pending.popleft()
            # Count attempted visits, including duplicates, to bound cyclic and
            # high-fanout work across all boundary searches.
            visits += 1
            if current in visited:
                continue
            visited.add(current)
            node = nodes[current]
            if node.get("path") in scoped_paths and node.get("type") != "test":
                witness = {"path": route, "extra_hops": hops}
                break
            if node.get("type") not in {"function", "class"}:
                continue
            targets = sorted(calls[current] - visited)
            if hops >= extra_hops:
                hop_limited = hop_limited or bool(targets)
                continue
            # The queue itself must also fit in the shared remaining budget.
            available = max_visits - visits - len(pending)
            if len(targets) > available:
                visit_limited = True
            for neighbor in targets[:max(0, available)]:
                if neighbor not in nodes:
                    raise ValueError("known call has a missing graph endpoint")
                pending.append((neighbor, route + [neighbor], hops + 1))
            search_peak_queue = max(search_peak_queue, len(pending))
            peak_queue = max(peak_queue, len(pending))
        results.append({
            "test_file": boundary["path"],
            "boundary": deepcopy(boundary),
            "known_call_witness": witness,
            "hop_budget_exhausted": hop_limited,
            "visit_budget_exhausted": visit_limited,
            "search_status": (
                "known_call_witness_found" if witness is not None else
                "visit_budget_exhausted" if visit_limited else
                "hop_budget_exhausted" if hop_limited else
                "known_calls_exhausted"
            ),
            "visits": visits - start_visits,
            "peak_queue_size": search_peak_queue,
        })
    return {"enabled": True, "extra_hop_limit": extra_hops, "visit_limit": max_visits,
            "visits": visits, "peak_queue_size": peak_queue, "boundary_searches": results}


def analyze_boundary_transitions(
    static_graph: Mapping[str, Any], fallback_graph: Mapping[str, Any],
    *, extra_call_hops: int = 0, max_extra_visits: int = 1000,
    ast_delta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the same extraction with import-use fallback disabled/enabled."""
    for value, name, minimum in (
        (extra_call_hops, "extra_call_hops", 0), (max_extra_visits, "max_extra_visits", 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    for key in ("nodes", "edges", "issues", "status", "incomplete_causes", "source_roots"):
        if static_graph.get(key) != fallback_graph.get(key):
            raise ValueError(f"comparison requires identical extraction evidence: {key}")
    scopes = []
    for graph in (static_graph, fallback_graph):
        scope = graph.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("graphs require scope mappings")
        selected = scope.get("linked_test_files")
        if not isinstance(selected, list) or any(not isinstance(path, str) or not path for path in selected):
            raise ValueError("linked_test_files must be lists of non-empty paths")
        scopes.append(scope)
    for key in ("changed_files", "max_hops", "distances"):
        if scopes[0].get(key) != scopes[1].get(key):
            raise ValueError(f"comparison requires identical scope: {key}")
    changes = scopes[0].get("changed_files")
    if not isinstance(changes, list) or not changes:
        raise ValueError("comparison requires changed_files")
    symbol_context = _changed_symbol_context(static_graph, ast_delta) if ast_delta is not None else None
    baseline = set(scopes[0]["linked_test_files"])
    selected = set(scopes[1]["linked_test_files"])
    if not baseline <= selected:
        raise ValueError("fallback must not remove static candidates")
    issues = static_graph.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(issue, Mapping) for issue in issues):
        raise ValueError("issues must be a list of mappings")
    boundaries = [
        deepcopy(dict(issue)) for issue in issues
        if issue.get("kind") == "test_call_depth_limit_reached"
    ]
    continuation = (
        _continue_boundaries(static_graph, boundaries, extra_call_hops, max_extra_visits)
        if extra_call_hops else {
            "enabled": False, "extra_hop_limit": 0, "visit_limit": max_extra_visits,
            "visits": 0, "peak_queue_size": 0, "boundary_searches": [],
        }
    )
    recovered = {
        row["test_file"] for row in continuation["boundary_searches"]
        if row["known_call_witness"] is not None
    }
    fallback_selected = set(selected)
    selected |= recovered
    wave = compute_bidirectional_dependency_meetings(
        static_graph, changes, sorted(selected),
    )
    rows = []
    for witness in wave["candidate_tests"]:
        path = witness["test_file"]
        fallback_added = path in fallback_selected - baseline
        continuation_added = path in recovered - fallback_selected
        added = fallback_added or continuation_added
        local_boundaries = [issue for issue in boundaries if issue.get("path") == path]
        boundary_present = bool(witness["boundary_evidence"] or local_boundaries)
        meeting = witness["meeting_points"][0] if witness["meeting_points"] else None
        rows.append({
            "test_file": path,
            "recommendation": "run_candidate",
            "evidence_layers": ["static", "recommendation"] if added else ["static"],
            "layer_transition_count": 1 if added else 0,
            "transition_reason": (
                "known_call_boundary_continuation" if continuation_added else
                "import_use_fallback_added_candidate" if fallback_added else None
            ),
            "fallback_added": fallback_added,
            "import_use_fallback_evidence": deepcopy(
                scopes[1].get("import_use_fallback_evidence", {}).get(path)
            ),
            "continuation_added": continuation_added,
            "boundary_encountered": boundary_present,
            "boundary_preserved": boundary_present,
            "depth_boundaries": deepcopy(local_boundaries),
            "boundary_evidence": deepcopy(witness["boundary_evidence"]),
            "static_meeting_classification": witness["classification"],
            "best_known_static_meeting": deepcopy(meeting),
            "evidence_status": (
                "review_required"
                if boundary_present or added or meeting is None
                else "known_static_route_no_reported_boundary"
            ),
        })
        if symbol_context is not None:
            rows[-1]["changed_symbol_alignment"] = _symbol_alignment(
                path, rows[-1]["import_use_fallback_evidence"], symbol_context,
            )
    selected_paths = {row["test_file"] for row in rows}
    return {
        "schema_version": 1,
        "analysis_kind": "experimental_boundary_evidence_transition_probe",
        "geometry_model": False,
        "probability_calibrated": False,
        "graph_edges_added": 0,
        "actionable_promotions": 0,
        "comparison": {
            "static_candidates": len(baseline),
            "fallback_candidates": len(fallback_selected),
            "combined_candidates": len(selected),
            "continuation_added_candidates": len(recovered - fallback_selected),
            "added_candidates": len(selected - baseline),
            "added_candidates_with_boundary": sum(
                (row["fallback_added"] or row["continuation_added"]) and row["boundary_encountered"]
                for row in rows
            ),
        },
        "boundary_entries": boundaries,
        "continuation": continuation,
        "unselected_depth_boundary_tests": sorted({
            issue["path"] for issue in boundaries
            if isinstance(issue.get("path"), str) and issue["path"] not in selected_paths
        }),
        "candidate_tests": rows,
        "limitations": [
            "One transition denotes candidate provenance, not an inferred runtime edge or a complete mixed-layer route.",
            "Continuation witnesses begin at a recorded terminal node, not at the test; the original depth boundary remains.",
            "Visit budget covers continuation attempts only, not extraction, graph indexing, or existing static meeting computation.",
            "Static meetings use only known edges; fallback selection does not resolve a boundary.",
            "Boundary absence is not completeness, correctness, or a probability estimate.",
            "No cosine weighting, artificial cyclic edges, history routes, or actionable-score changes are applied.",
            "Optional continuation follows known calls under separate budgets and never clears original boundaries.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--changed", action="append", required=True)
    parser.add_argument("--source-root", action="append", default=[])
    parser.add_argument("--ast-delta", type=Path, help="AST delta JSON from the same snapshot comparison; adds display-only symbol alignment")
    parser.add_argument("--max-hops", type=int, default=0)
    parser.add_argument("--extra-call-hops", type=int, default=0)
    parser.add_argument("--max-extra-visits", type=int, default=1000)
    args = parser.parse_args()
    try:
        graphs = [
            build_dependency_graph(
                args.root, changed_files=args.changed, max_hops=args.max_hops,
                include_import_use_test_fallback=enabled,
                source_roots=args.source_root,
            )
            for enabled in (False, True)
        ]
        result = analyze_boundary_transitions(
            *graphs, extra_call_hops=args.extra_call_hops, max_extra_visits=args.max_extra_visits,
            ast_delta=_read_cli_document(args.ast_delta, 64 * 1024 * 1024) if args.ast_delta else None,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
