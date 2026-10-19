"""Compute weighted graph-geodesic review distances for a GEO snapshot.

This is a directed shortest-path probe over known static relations, not a
semantic metric or a safety/manifold guarantee. Missing relations are reported
as boundary evidence and are never imputed as edges.
"""

from __future__ import annotations

import heapq
import math
from typing import Any, Mapping


_EDGE_BASE_COSTS = {
    "contains": 0.25,
    "imports": 1.0,
    "calls": 1.0,
    "tests": 1.0,
    "type_depends_on": 1.25,
    "reads": 1.5,
    "writes": 1.5,
}
_UNCERTAINTY_KINDS = {
    "changed_file_not_found",
    "edge_budget_exceeded",
    "file_budget_exceeded",
    "node_budget_exceeded",
    "parse_error",
    "snapshot_read_error",
    "test_call_depth_limit_reached",
    "unresolved_attribute_call_summary",
    "unresolved_local_import",
    "unresolved_simple_call_summary",
}


def _graph_indexes(graph: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[str, float]]]]:
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("graph must contain node and edge lists")

    nodes = {}
    for node in raw_nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("id"), str):
            raise ValueError("every graph node must have a string id")
        if node["id"] in nodes:
            raise ValueError(f"duplicate graph node id: {node['id']}")
        nodes[node["id"]] = dict(node)

    adjacency: dict[str, list[tuple[str, float]]] = {node_id: [] for node_id in nodes}
    for edge in raw_edges:
        if not isinstance(edge, Mapping):
            raise ValueError("every graph edge must be a mapping")
        source, target, edge_type = edge.get("source"), edge.get("target"), edge.get("type")
        if source not in nodes or target not in nodes or edge_type not in _EDGE_BASE_COSTS:
            continue
        confidence = edge.get("confidence", 1.0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= confidence <= 1.0
        ):
            raise ValueError("edge confidence must be a finite number from 0 to 1")
        cost = _EDGE_BASE_COSTS[edge_type] * (2.0 - float(confidence))
        if edge_type == "contains":
            adjacency[source].append((target, cost))
            adjacency[target].append((source, cost))
        else:
            # A dependency is traversed from the depended-on node to its user.
            adjacency[target].append((source, cost))
    for neighbors in adjacency.values():
        neighbors.sort(key=lambda item: (item[0], item[1]))
    return nodes, adjacency


def _shortest_paths(
    starts: list[str],
    adjacency: Mapping[str, list[tuple[str, float]]],
) -> dict[str, tuple[float, tuple[str, ...]]]:
    best: dict[str, tuple[float, int, tuple[str, ...]]] = {}
    pending = []
    for start in sorted(set(starts)):
        key = (0.0, 0, (start,))
        best[start] = key
        heapq.heappush(pending, (*key, start))

    while pending:
        distance, hops, path, current = heapq.heappop(pending)
        if best.get(current) != (distance, hops, path):
            continue
        for neighbor, cost in adjacency[current]:
            candidate = (distance + cost, hops + 1, (*path, neighbor))
            if neighbor not in best or candidate < best[neighbor]:
                best[neighbor] = candidate
                heapq.heappush(pending, (*candidate, neighbor))
    return {node_id: (value[0], value[2]) for node_id, value in best.items()}


def _boundary_evidence(
    graph: Mapping[str, Any],
    scope_paths: set[str],
) -> list[dict[str, Any]]:
    result = []
    for issue in graph.get("issues", []):
        if not isinstance(issue, Mapping) or issue.get("kind") not in _UNCERTAINTY_KINDS:
            continue
        issue_path = issue.get("path")
        examples = issue.get("examples", [])
        relevant_examples = [
            dict(example)
            for example in examples
            if isinstance(example, Mapping) and example.get("path") in scope_paths
        ] if isinstance(examples, list) else []
        directly_relevant = issue_path in scope_paths
        if directly_relevant or relevant_examples:
            result.append({
                "kind": issue["kind"],
                "path": issue_path,
                "count": issue.get("count", 1),
                "examples_in_scope": relevant_examples,
                "example_count_is_lower_bound": issue.get("count", 1) > len(examples),
            })
        elif issue_path is None and issue.get("kind") in {
            "edge_budget_exceeded", "file_budget_exceeded", "node_budget_exceeded",
        }:
            result.append({
                "kind": issue["kind"],
                "path": None,
                "count": issue.get("count", 1),
                "examples_in_scope": [],
                "example_count_is_lower_bound": False,
            })
    return sorted(result, key=lambda row: (row["kind"], row["path"] or ""))


def _backward_adjacency(
    graph: Mapping[str, Any],
    nodes: Mapping[str, dict[str, Any]],
) -> dict[str, list[tuple[str, float]]]:
    """Index provider dependencies outward from their consumer/caller."""
    adjacency: dict[str, list[tuple[str, float]]] = {node_id: [] for node_id in nodes}
    for edge in graph.get("edges", []):
        if not isinstance(edge, Mapping):
            continue
        source, target, edge_type = edge.get("source"), edge.get("target"), edge.get("type")
        if source not in nodes or target not in nodes or edge_type not in _EDGE_BASE_COSTS:
            continue
        confidence = edge.get("confidence", 1.0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= confidence <= 1.0
        ):
            raise ValueError("edge confidence must be a finite number from 0 to 1")
        cost = _EDGE_BASE_COSTS[edge_type] * (2.0 - float(confidence))
        if edge_type == "contains":
            adjacency[source].append((target, cost))
            adjacency[target].append((source, cost))
        else:
            adjacency[source].append((target, cost))
    for neighbors in adjacency.values():
        neighbors.sort(key=lambda item: (item[0], item[1]))
    return adjacency


def compute_dependency_geodesics(
    graph: Mapping[str, Any],
    changed_files: list[str],
) -> dict[str, Any]:
    """Rank linked test files by weighted shortest path from changed files.

    Edge confidence is used only as an uncalibrated cost adjustment. Dependency
    edges are traversed from provider to consumer; ownership edges are
    traversed in both directions. Unresolved relations remain boundary
    evidence and do not produce distances.
    """
    if not isinstance(changed_files, list) or not changed_files or any(
        not isinstance(path, str) or not path for path in changed_files
    ):
        raise ValueError("changed_files must be a non-empty list of paths")

    nodes, adjacency = _graph_indexes(graph)
    normalized = sorted(set(changed_files))
    starts = [f"file:{path}" for path in normalized if f"file:{path}" in nodes]
    missing = sorted(set(normalized) - {nodes[node_id]["path"] for node_id in starts})
    shortest = _shortest_paths(starts, adjacency)

    scope = graph.get("scope", {})
    linked_test_paths = sorted(set(scope.get("linked_test_files", [])))
    scope_paths = set(scope.get("files", [])) | set(linked_test_paths) | set(normalized)
    rows = []
    for test_path in linked_test_paths:
        test_file_id = f"file:{test_path}"
        result = shortest.get(test_file_id)
        rows.append({
            "test_file": test_path,
            "reachable": result is not None,
            "distance": round(result[0], 12) if result is not None else None,
            "hops": len(result[1]) - 1 if result is not None else None,
            "path": list(result[1]) if result is not None else [],
        })
    rows.sort(key=lambda row: (
        row["distance"] is None,
        row["distance"] if row["distance"] is not None else math.inf,
        row["test_file"],
    ))
    boundary_evidence = _boundary_evidence(graph, scope_paths)
    return {
        "schema_version": 1,
        "analysis_kind": "geo_weighted_dependency_geodesic_probe",
        "distance_definition": "directed weighted shortest path over known static relations",
        "distance_is_metric": False,
        "confidence_is_calibrated_probability": False,
        "changed_files": normalized,
        "missing_changed_files": missing,
        "reachable_test_files": sum(row["reachable"] for row in rows),
        "unreachable_test_files": sum(not row["reachable"] for row in rows),
        "test_distances": rows,
        "unresolved_boundary_evidence": boundary_evidence,
        "limitations": [
            "Directed reachability can be asymmetric and disconnected pairs have no finite distance.",
            "Edge costs are review-ordering weights, not semantic impact probabilities.",
            "Unknown relations are reported as boundary evidence, not imputed or treated as safe.",
            "The probe does not establish a continuous manifold, smoothness, correctness, or safety.",
        ],
    }


def compute_bidirectional_dependency_meetings(
    graph: Mapping[str, Any],
    changed_files: list[str],
    test_files: list[str] | None = None,
) -> dict[str, Any]:
    """Find common nodes reached by the impact and test-dependency waves.

    The forward wave starts at changed provider files and follows known
    dependency edges toward consumers. The backward wave starts at each test
    file and follows its declared dependencies toward providers. Shared nodes
    are meeting points that expose a static route; they are not proof that the
    route captures the complete semantic impact.
    """
    if not isinstance(changed_files, list) or not changed_files or any(
        not isinstance(path, str) or not path for path in changed_files
    ):
        raise ValueError("changed_files must be a non-empty list of paths")

    nodes, forward_adjacency = _graph_indexes(graph)
    backward_adjacency = _backward_adjacency(graph, nodes)
    normalized_changes = sorted(set(changed_files))
    changed_starts = [f"file:{path}" for path in normalized_changes if f"file:{path}" in nodes]
    missing_changes = sorted(set(normalized_changes) - {
        nodes[node_id].get("path") for node_id in changed_starts
    })
    forward = _shortest_paths(changed_starts, forward_adjacency)

    if test_files is None:
        test_files = list(graph.get("scope", {}).get("linked_test_files", []))
    if not isinstance(test_files, list) or any(not isinstance(path, str) or not path for path in test_files):
        raise ValueError("test_files must be a list of non-empty paths")

    candidate_results = []
    all_relevant_paths = set(normalized_changes)
    for test_path in sorted(set(test_files)):
        test_file_id = f"file:{test_path}"
        if test_file_id not in nodes:
            candidate_results.append({
                "test_file": test_path,
                "classification": "test_file_missing",
                "meeting_points": [],
                "boundary_evidence": [],
            })
            continue
        backward = _shortest_paths([test_file_id], backward_adjacency)
        common_nodes = sorted(set(forward) & set(backward))
        meetings = []
        scope_paths = {test_path, *normalized_changes}
        for node_id in common_nodes:
            forward_distance, forward_path = forward[node_id]
            backward_distance, backward_path = backward[node_id]
            for route_node_id in (*forward_path, *backward_path):
                route_path = nodes[route_node_id].get("path")
                if isinstance(route_path, str):
                    scope_paths.add(route_path)
            meetings.append({
                "node_id": node_id,
                "node_type": nodes[node_id].get("type"),
                "path": nodes[node_id].get("path"),
                "forward_distance": round(forward_distance, 12),
                "backward_distance": round(backward_distance, 12),
                "combined_distance": round(forward_distance + backward_distance, 12),
                "forward_path": list(forward_path),
                "backward_path_from_test": list(backward_path),
            })
        boundary = _boundary_evidence(graph, scope_paths)
        boundary.sort(key=lambda row: (row["kind"], row["path"] or ""))
        all_relevant_paths.update(scope_paths)
        meetings.sort(key=lambda row: (
            row["combined_distance"],
            row["node_id"],
        ))
        if meetings:
            classification = "connected_with_boundary_evidence" if boundary else "connected_static_path"
        else:
            classification = "no_static_meeting_with_boundary" if boundary else "no_static_meeting"
        candidate_results.append({
            "test_file": test_path,
            "classification": classification,
            "meeting_points": meetings,
            "boundary_evidence": boundary,
        })

    graph_status = graph.get("status")
    incomplete_causes = graph.get("incomplete_causes")
    if isinstance(incomplete_causes, Mapping):
        global_causes = incomplete_causes.get("global", [])
        scope_causes = incomplete_causes.get("scope", [])
        globally_incomplete = bool(global_causes) or any(
            not isinstance(issue, Mapping)
            or issue.get("kind") != "test_call_depth_limit_reached"
            for issue in scope_causes
        )
    else:
        graph_issues = graph.get("issues", [])
        only_call_depth_boundaries = bool(graph_issues) and all(
            isinstance(issue, Mapping) and issue.get("kind") == "test_call_depth_limit_reached"
            for issue in graph_issues
        )
        globally_incomplete = graph_status == "incomplete/review" and not only_call_depth_boundaries
        if not graph_issues and graph_status == "incomplete/review":
            globally_incomplete = True
    if globally_incomplete:
        for candidate in candidate_results:
            if candidate["classification"] == "test_file_missing":
                continue
            candidate["boundary_evidence"].append({
                "kind": "snapshot_incomplete",
                "path": None,
                "count": 1,
                "examples_in_scope": [],
                "example_count_is_lower_bound": False,
            })
            candidate["classification"] = (
                "connected_with_boundary_evidence"
                if candidate["meeting_points"]
                else "no_static_meeting_with_boundary"
            )
    return {
        "schema_version": 1,
        "analysis_kind": "geo_bidirectional_dependency_wave_probe",
        "forward_wave": "changed providers to consumers over known static dependencies",
        "backward_wave": "test files to declared dependencies over known static edges",
        "meeting_interpretation": "shared reachable nodes provide a static route witness, not semantic proof",
        "distance_is_metric": False,
        "graph_status": graph_status,
        "global_incomplete": globally_incomplete,
        "missing_changed_files": missing_changes,
        "candidate_test_count": len(candidate_results),
        "connected_candidate_count": sum(bool(item["meeting_points"]) for item in candidate_results),
        "boundary_candidate_count": sum(bool(item["boundary_evidence"]) for item in candidate_results),
        "candidate_tests": candidate_results,
        "limitations": [
            "No static meeting does not prove absence of a dynamic or otherwise unresolved dependency.",
            "A static meeting is evidence of a modeled route, not proof of true or complete semantic impact.",
            "Any relevant unresolved evidence prevents a candidate from being labeled actionable by this probe.",
            "Graph status and unresolved issues are evidence states, not a calibrated abstention probability.",
        ],
    }