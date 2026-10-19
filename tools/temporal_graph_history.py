"""Build bounded revision-time records from dependency graph snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("snapshot must contain canonical JSON values") from error


def _snapshot_records(snapshot: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    records = snapshot.get(key)
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError(f"snapshot {key} must be a sequence")
    normalized = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError(f"snapshot {key} entries must be mappings")
        normalized.append(dict(record))
    return normalized


def _nodes_by_id(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = {}
    for node in _snapshot_records(snapshot, "nodes"):
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("every graph node requires a non-empty id")
        if node_id in nodes:
            raise ValueError(f"duplicate graph node id: {node_id}")
        nodes[node_id] = node
    return nodes


def _edge_identity(edge: Mapping[str, Any]) -> tuple[Any, ...]:
    source = edge.get("source")
    target = edge.get("target")
    edge_type = edge.get("type")
    line = edge.get("line")
    if not isinstance(source, str) or not isinstance(target, str) or not isinstance(edge_type, str):
        raise ValueError("every graph edge requires source, target, and type")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
        raise ValueError("graph edge line must be an integer or null")
    return source, target, edge_type, line


def _edges_by_identity(snapshot: Mapping[str, Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    edges = {}
    for edge in _snapshot_records(snapshot, "edges"):
        identity = _edge_identity(edge)
        if identity in edges:
            raise ValueError(f"duplicate graph edge identity: {identity}")
        edges[identity] = edge
    return edges


def _changed_records(
    before: Mapping[Any, dict[str, Any]],
    after: Mapping[Any, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    before_keys = set(before)
    after_keys = set(after)
    added = sorted(after_keys - before_keys, key=str)
    removed = sorted(before_keys - after_keys, key=str)
    common = sorted(before_keys & after_keys, key=str)
    changed = [key for key in common if before[key] != after[key]]
    return {
        "added": [after[key] for key in added],
        "removed": [before[key] for key in removed],
        "changed": [
            {"before": before[key], "after": after[key]}
            for key in changed
        ],
    }


def _issue_set(snapshot: Mapping[str, Any]) -> dict[bytes, dict[str, Any]]:
    issues = {}
    for issue in _snapshot_records(snapshot, "issues"):
        issues[_canonical_bytes(issue)] = issue
    return issues


def _snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    semantic_snapshot = {key: value for key, value in snapshot.items() if key != "cache"}
    return hashlib.sha256(_canonical_bytes(semantic_snapshot)).hexdigest()


def _directly_linked_tests(
    parent_nodes: Mapping[str, dict[str, Any]],
    current_nodes: Mapping[str, dict[str, Any]],
    parent_edges: Mapping[tuple[Any, ...], dict[str, Any]],
    current_edges: Mapping[tuple[Any, ...], dict[str, Any]],
    node_delta: Mapping[str, list[dict[str, Any]]],
    edge_delta: Mapping[str, list[dict[str, Any]]],
) -> list[str]:
    changed_node_ids = {
        node["id"]
        for category in ("added", "removed")
        for node in node_delta[category]
    }
    changed_node_ids.update(
        change["after"]["id"] for change in node_delta["changed"]
    )
    changed_endpoints = set(changed_node_ids)
    changed_edge_records = [
        *edge_delta["added"],
        *edge_delta["removed"],
        *(change["after"] for change in edge_delta["changed"]),
    ]
    for edge in changed_edge_records:
        changed_endpoints.update((edge["source"], edge["target"]))

    all_nodes = {**parent_nodes, **current_nodes}
    affected = {
        node_id for node_id in changed_node_ids
        if all_nodes.get(node_id, {}).get("type") == "test"
    }
    all_edges = {**parent_edges, **current_edges}
    for edge in all_edges.values():
        if edge["type"] != "tests":
            continue
        if edge["source"] not in changed_endpoints and edge["target"] not in changed_endpoints:
            continue
        for endpoint in (edge["source"], edge["target"]):
            if all_nodes.get(endpoint, {}).get("type") == "test":
                affected.add(endpoint)
    return sorted(affected)


def build_temporal_snapshot_record(
    snapshot: Mapping[str, Any],
    *,
    revision_id: str,
    sequence_index: int,
    parent_revision_ids: Sequence[str] = (),
    parent_snapshot: Mapping[str, Any] | None = None,
    comparison_parent_revision_id: str | None = None,
    comparison_parent_sequence_index: int | None = None,
) -> dict[str, Any]:
    """Record one revision and its delta from an explicitly selected parent.

    Sequence order is the temporal coordinate. Wall-clock timestamps and
    outcome labels are intentionally not inferred from graph snapshots.
    """
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("revision_id must be a non-empty string")
    if not isinstance(sequence_index, int) or isinstance(sequence_index, bool) or sequence_index < 0:
        raise ValueError("sequence_index must be a non-negative integer")
    parents = tuple(parent_revision_ids)
    if any(not isinstance(parent, str) or not parent or parent == revision_id for parent in parents):
        raise ValueError("parent revision ids must be non-empty and differ from revision_id")
    if len(set(parents)) != len(parents):
        raise ValueError("parent revision ids must be unique")
    if parent_snapshot is None:
        if (
            parents
            or comparison_parent_revision_id is not None
            or comparison_parent_sequence_index is not None
            or sequence_index != 0
        ):
            raise ValueError("a non-root revision requires its comparison parent snapshot")
        parent_snapshot = {"nodes": [], "edges": [], "issues": []}
        comparison_parent_revision_id = None
    elif (
        not parents
        or comparison_parent_revision_id not in parents
        or sequence_index == 0
        or not isinstance(comparison_parent_sequence_index, int)
        or isinstance(comparison_parent_sequence_index, bool)
        or comparison_parent_sequence_index < 0
        or comparison_parent_sequence_index >= sequence_index
    ):
        raise ValueError("a parent snapshot requires a declared earlier comparison parent")

    current_nodes = _nodes_by_id(snapshot)
    parent_nodes = _nodes_by_id(parent_snapshot)
    current_edges = _edges_by_identity(snapshot)
    parent_edges = _edges_by_identity(parent_snapshot)
    node_delta = _changed_records(parent_nodes, current_nodes)
    edge_delta = _changed_records(parent_edges, current_edges)
    parent_issues = _issue_set(parent_snapshot)
    current_issues = _issue_set(snapshot)
    node_changes = [*node_delta["added"], *node_delta["removed"]]
    node_changes.extend(change["after"] for change in node_delta["changed"])
    changed_contracts = [node for node in node_changes if node.get("type") == "contract"]

    return {
        "schema_version": 1,
        "analysis_kind": "temporal_dependency_snapshot",
        "revision": {
            "id": revision_id,
            "parent_ids": list(parents),
            "comparison_parent_id": comparison_parent_revision_id,
            "comparison_parent_sequence_index": comparison_parent_sequence_index,
            "sequence_index": sequence_index,
        },
        "graph_sha256": _snapshot_hash(snapshot),
        "parent_graph_sha256": _snapshot_hash(parent_snapshot) if parents else None,
        "delta": {
            "nodes": node_delta,
            "edges": edge_delta,
            "changed_contract_nodes": changed_contracts,
            "directly_linked_tests": _directly_linked_tests(
                parent_nodes, current_nodes, parent_edges, current_edges, node_delta, edge_delta
            ),
        },
        "extractor_uncertainty": {
            "status_before": parent_snapshot.get("status"),
            "status_after": snapshot.get("status"),
            "introduced_issues": [current_issues[key] for key in sorted(current_issues.keys() - parent_issues.keys())],
            "resolved_issues": [parent_issues[key] for key in sorted(parent_issues.keys() - current_issues.keys())],
            "current_issues": [current_issues[key] for key in sorted(current_issues)],
        },
        "outcome_annotation": None,
    }