"""Compare static dependency distance with historical file co-change distance."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from collections.abc import Sequence
from fnmatch import fnmatchcase
from itertools import combinations
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from tools.git_temporal_history import extract_git_temporal_history


_DEPENDENCY_TYPES = {"calls", "imports", "reads", "type_depends_on", "writes"}
_PATH_FROM_NODE_ID = re.compile(r"^(?:file|function|class|global|test):(.+?\.py)(?::|$)")


def _node_path(node_id: Any) -> str | None:
    if not isinstance(node_id, str):
        return None
    match = _PATH_FROM_NODE_ID.match(node_id)
    return match.group(1) if match else None


def _record_changed_paths(record: Mapping[str, Any]) -> set[str]:
    paths = set()
    delta = record.get("delta", {})
    nodes = delta.get("nodes", {}) if isinstance(delta, Mapping) else {}
    for category in ("added", "removed", "changed"):
        for item in nodes.get(category, []) if isinstance(nodes, Mapping) else []:
            candidates = (item.get("before"), item.get("after")) if category == "changed" and isinstance(item, Mapping) else (item,)
            for candidate in candidates:
                if isinstance(candidate, Mapping) and isinstance(candidate.get("path"), str):
                    paths.add(candidate["path"])
    edges = delta.get("edges", {}) if isinstance(delta, Mapping) else {}
    for category in ("added", "removed", "changed"):
        for item in edges.get(category, []) if isinstance(edges, Mapping) else []:
            candidates = (item.get("before"), item.get("after")) if category == "changed" and isinstance(item, Mapping) else (item,)
            for candidate in candidates:
                if isinstance(candidate, Mapping):
                    paths.update(
                        path for path in (_node_path(candidate.get("source")), _node_path(candidate.get("target")))
                        if path is not None
                    )
    ast_delta = record.get("ast_delta", {})
    symbols = ast_delta.get("symbols", {}) if isinstance(ast_delta, Mapping) else {}
    for category in ("added", "removed", "changed"):
        for item in symbols.get(category, []) if isinstance(symbols, Mapping) else []:
            symbol_id = item.get("symbol_id") if isinstance(item, Mapping) else None
            path = _node_path(symbol_id)
            if path is not None:
                paths.add(path)
    return paths


def _static_file_adjacency(graph: Mapping[str, Any]) -> dict[str, set[str]]:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("graph must contain node and edge lists")
    paths_by_id = {
        node.get("id"): node.get("path")
        for node in nodes
        if isinstance(node, Mapping)
        and isinstance(node.get("id"), str)
        and isinstance(node.get("path"), str)
    }
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if not isinstance(edge, Mapping) or edge.get("type") not in _DEPENDENCY_TYPES:
            continue
        source = paths_by_id.get(edge.get("source")) or _node_path(edge.get("source"))
        target = paths_by_id.get(edge.get("target")) or _node_path(edge.get("target"))
        if source and target and source != target:
            adjacency[source].add(target)
            adjacency[target].add(source)
    return adjacency


def _distances_from(start: str, adjacency: Mapping[str, set[str]]) -> dict[str, int]:
    distances = {start: 0}
    pending = deque([start])
    while pending:
        current = pending.popleft()
        for neighbor in sorted(adjacency.get(current, set())):
            if neighbor not in distances:
                distances[neighbor] = distances[current] + 1
                pending.append(neighbor)
    return distances


def _graph_uncertainty(graph: Mapping[str, Any]) -> dict[str, Any]:
    status = graph.get("status")
    if status is not None and not isinstance(status, str):
        raise ValueError("graph status must be a string or null")
    issues = graph.get("issues")
    if issues is not None and (
        not isinstance(issues, list) or any(not isinstance(issue, Mapping) for issue in issues)
    ):
        raise ValueError("graph issues must be a list of mappings")
    causes = graph.get("incomplete_causes")
    if causes is not None and (
        not isinstance(causes, Mapping)
        or any(
            not isinstance(entries, list) or any(not isinstance(item, Mapping) for item in entries)
            for entries in causes.values()
        )
    ):
        raise ValueError("graph incomplete_causes must map categories to lists of mappings")
    summaries = []
    for issue in issues or []:
        if issue.get("kind") not in (
            "unresolved_attribute_call_summary", "unresolved_simple_call_summary",
        ):
            continue
        count = issue.get("count")
        examples = issue.get("examples", [])
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("unresolved call summary count must be a non-negative integer")
        if (
            not isinstance(examples, list)
            or any(not isinstance(example, Mapping) for example in examples)
            or len(examples) > count
        ):
            raise ValueError("unresolved call examples must be mappings and cannot exceed count")
        summaries.append({
            "kind": issue["kind"],
            "count": count,
            "examples": [dict(example) for example in examples],
            "examples_truncated": len(examples) < count,
        })
    return {
        "scope": "whole_graph_not_pair_attributed",
        "graph_status": status,
        "issues_reported": issues is not None,
        "incomplete_causes_reported": causes is not None,
        "incomplete_reported": status == "incomplete/review" or bool(
            causes and any(causes.values())
        ),
        "issues": [dict(issue) for issue in issues] if issues is not None else None,
        "incomplete_causes": (
            {key: [dict(item) for item in entries] for key, entries in causes.items()}
            if causes is not None else None
        ),
        "unresolved_calls": {
            "reported_count": sum(summary["count"] for summary in summaries) if issues is not None else None,
            "summaries": summaries,
            "per_file_counts_available": False,
        },
        "interpretation": [
            "A missing static path is not evidence that no runtime dependency exists.",
            "Known paths are syntax-level evidence, not proof of semantic correctness.",
            "Unresolved-call counts cover reported sites, not all dynamic behavior or a call-resolution rate.",
            "Examples may be sampled; absence of a file from examples does not establish that it has no unresolved calls.",
            "Missing metadata and zero reported issues do not establish complete analysis.",
        ],
    }


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _read_cli_document(path: Path, max_bytes: int) -> Mapping[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"input exceeds --max-input-bytes: {path}")
    document = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(document, Mapping):
        raise ValueError(f"input must contain a JSON object: {path}")
    return document


def _text_report(result: Mapping[str, Any], top: int) -> str:
    uncertainty = result["graph_uncertainty"]
    noise = result["noise_filter"]
    calls = uncertainty["unresolved_calls"]
    count = calls["reported_count"]
    rows = [
        "GEO history tension — review candidates, not a safety verdict",
        f"Comparable revisions: {result['comparable_revisions']} "
        f"(excluded root: {result['excluded_root_records']}, "
        f"incomplete: {result['excluded_incomplete_records']})",
        f"Pairs: {result['pair_count']}; hotspots: {result['hotspot_count']}",
        f"Path identity: {result['path_identity']}",
        f"Excluded paths: {noise['excluded_path_count']}; "
        f"file/revision occurrences: {noise['excluded_path_revision_count']}",
        f"Exclusion patterns: {json.dumps(noise['excluded_path_patterns'], ensure_ascii=True)}",
        f"Graph status: {uncertainty['graph_status'] or 'not reported'}; "
        f"incompleteness reported: {uncertainty['incomplete_reported']}",
        f"Unresolved call sites reported: {count if count is not None else 'unknown'} "
        "(whole graph; not attributed to pairs)",
        "Call examples may be sampled; they are not a per-file census or call-resolution rate.",
        "A missing static path does not mean no runtime dependency exists.",
        "No reported incompleteness or unresolved calls does not prove complete analysis.",
    ]
    if "source" in result:
        rows.append(f"Git source: {json.dumps(result['source'], ensure_ascii=True, sort_keys=True)}")
    for label, metadata in (
        ("Graph issues", uncertainty["issues"]),
        ("Incomplete causes", uncertainty["incomplete_causes"]),
    ):
        rows.append(f"{label}: {json.dumps(metadata, ensure_ascii=True, sort_keys=True)}")
    for file in result["files"]:
        if file["excluded"]:
            rows.append(
                f"Excluded: {json.dumps(file['path'])} "
                f"({file['changed_commits']} revisions; "
                f"patterns={json.dumps(file['matched_exclusion_patterns'])})"
            )
    rows.append(f"Ranked pairs (showing {min(top, result['pair_count'])} of {result['pair_count']}):")
    for pair in result["pairs"][:top]:
        rows.extend((
            f"{'[hotspot]' if pair['hotspot'] else '[candidate]'} "
            f"{json.dumps(pair['left_path'])} <-> {json.dumps(pair['right_path'])}",
            f"  tension={pair['tension']:.6f}; cochange={pair['cochange_commits']}; "
            f"Jaccard={pair['historical_jaccard_proximity']:.6f}; "
            f"evidence={pair['structural_evidence']}; hops={pair['structural_distance_hops']}",
            f"  change frequency={pair['left_change_frequency']:.6f}/{pair['right_change_frequency']:.6f}; "
            f"static degree={pair['left_static_degree']}/{pair['right_static_degree']}",
        ))
    if not result["pairs"]:
        rows.append("No candidate pairs in the retained history; this is not a safety verdict.")
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, help="Static graph JSON for the final history snapshot")
    parser.add_argument("--history", type=Path, help="History JSON, oldest first")
    parser.add_argument("--repository", type=Path, help="Read committed snapshots directly; cannot be combined with JSON inputs")
    parser.add_argument("--revisions", nargs="+", help="Contiguous first-parent revisions, oldest first")
    parser.add_argument("--max-revisions", type=int, default=20)
    parser.add_argument("--max-files", type=int, default=1000)
    parser.add_argument("--source-root", action="append", default=[], help="Import root relative to committed snapshots; repository mode only, repeatable")
    parser.add_argument("--max-total-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-metadata-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-nodes", type=int, default=20000)
    parser.add_argument("--max-edges", type=int, default=50000)
    parser.add_argument("--max-ast-symbols-per-file", type=int, default=10000)
    parser.add_argument("--max-symbol-ast-nodes-per-file", type=int, default=200000)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--top", type=int, default=20, help="Maximum pairs shown in text; JSON always includes all pairs")
    parser.add_argument("--exclude", action="append", default=[], help="Case-sensitive whole-path fnmatch; repeatable")
    parser.add_argument("--minimum-cochange-commits", type=int, default=1)
    parser.add_argument("--hotspot-threshold", type=float, default=0.5)
    parser.add_argument("--max-history-records", type=int, default=1000)
    parser.add_argument("--max-paths-per-revision", type=int, default=1000)
    parser.add_argument("--max-unique-paths", type=int, default=5000)
    parser.add_argument("--max-pairs", type=int, default=100000)
    parser.add_argument("--max-input-bytes", type=int, default=64 * 1024 * 1024, help="Byte limit per JSON input")
    args = parser.parse_args()
    try:
        if args.top <= 0 or args.max_input_bytes <= 0:
            raise ValueError("--top and --max-input-bytes must be positive")
        if args.repository is not None:
            if args.graph is not None or args.history is not None or not args.revisions:
                raise ValueError("--repository requires --revisions and cannot be combined with --graph/--history")
            if len(args.revisions) > args.max_history_records:
                raise ValueError("history record count exceeded the analysis budget")
            if not math.isfinite(args.timeout_seconds):
                raise ValueError("--timeout-seconds must be finite")
            history = extract_git_temporal_history(
                args.repository, args.revisions,
                max_revisions=args.max_revisions,
                max_files=args.max_files,
                max_total_bytes=args.max_total_bytes,
                max_metadata_bytes=args.max_metadata_bytes,
                max_nodes=args.max_nodes,
                max_edges=args.max_edges,
                max_ast_symbols_per_file=args.max_ast_symbols_per_file,
                max_symbol_ast_nodes_per_file=args.max_symbol_ast_nodes_per_file,
                timeout_seconds=args.timeout_seconds,
                include_final_snapshot=True,
                source_roots=args.source_root,
            )
            graph = history.pop("final_snapshot")
        else:
            if args.source_root:
                raise ValueError("--source-root requires --repository; JSON inputs are already extracted")
            if args.revisions is not None or args.graph is None or args.history is None:
                raise ValueError("provide either --repository with --revisions, or both --graph and --history")
            graph = _read_cli_document(args.graph, args.max_input_bytes)
            history = _read_cli_document(args.history, args.max_input_bytes)
        result = analyze_history_structural_tension(
            graph, history,
            minimum_cochange_commits=args.minimum_cochange_commits,
            hotspot_threshold=args.hotspot_threshold,
            excluded_path_patterns=args.exclude,
            max_history_records=args.max_history_records,
            max_paths_per_revision=args.max_paths_per_revision,
            max_unique_paths=args.max_unique_paths,
            max_pairs=args.max_pairs,
        )
        if args.repository is not None:
            final_source = history["records"][-1]["source"]
            result["source"] = {
                "kind": "git_commit",
                "repository": history["repository"],
                "revision_ids": history["selection"]["revision_ids"],
                "baseline_revision_id": history["selection"]["baseline_revision_id"],
                "final_commit_id": final_source["commit_id"],
                "final_tree_id": final_source["tree_id"],
                "graph_root": graph["root"],
                "snapshot_alignment": "same_extraction",
                "working_tree_used": False,
            }
            if args.source_root:
                result["source"]["source_roots"] = graph["source_roots"]
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    print(
        json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
        if args.format == "json" else _text_report(result, args.top)
    )


def _history_changed_paths(
    records: list[Any], max_paths_per_revision: int, max_unique_paths: int,
) -> list[set[str]]:
    """Project Git changes onto final-revision paths, respecting path reuse."""
    if any(not isinstance(record, Mapping) for record in records):
        raise ValueError("history records must be mappings")
    git_count = sum("file_changes" in record for record in records)
    if git_count and git_count != len(records):
        raise ValueError("cannot mix file_changes records with legacy history")
    if git_count:
        revision_ids = set()
        for index, record in enumerate(records):
            revision = record.get("revision")
            if (
                not isinstance(revision, Mapping)
                or not isinstance(revision.get("id"), str)
                or not revision["id"]
            ):
                raise ValueError("Git change records require revision ids")
            if revision["id"] in revision_ids:
                raise ValueError("Git change records must have unique revision ids")
            revision_ids.add(revision["id"])
            if index and revision.get("comparison_parent_id") != records[index - 1]["revision"]["id"]:
                raise ValueError("Git change records must be a contiguous oldest-first history")
    labels: dict[str, str | None] = {}
    result: list[set[str]] = []
    for record in reversed(records):
        if "file_changes" not in record:
            paths = _record_changed_paths(record)
            result.append({
                label for path in paths
                if (label := labels.get(path, path)) is not None
            })
            continue
        changes = record["file_changes"]
        if not isinstance(changes, list):
            raise ValueError("file_changes must be a list")
        if len(changes) > max_paths_per_revision:
            raise ValueError("changed paths exceeded the per-revision analysis budget")
        paths = set()
        before = dict(labels)
        old_paths = set()
        new_paths = set()
        for change in changes:
            if not isinstance(change, Mapping):
                raise ValueError("file_changes entries must be mappings")
            status, path = change.get("status"), change.get("path")
            if (
                not isinstance(status, str) or status not in {"A", "D", "M", "T", "R"}
                or not isinstance(path, str) or not path
            ):
                raise ValueError("invalid file change status or path")
            if path in new_paths:
                raise ValueError("duplicate file change path")
            new_paths.add(path)
            label = labels.get(path, path)
            if status != "D" and not (status == "R" and change.get("content_changed") is False):
                if label is not None:
                    paths.add(label)
            if status in {"A", "R", "D"}:
                before[path] = None
            if status == "R":
                old = change.get("old_path")
                if not isinstance(old, str) or not old or old == path or old in old_paths:
                    raise ValueError("invalid or duplicate rename source")
                content_changed = change.get("content_changed")
                if content_changed is not None and not isinstance(content_changed, bool):
                    raise ValueError("rename content_changed must be boolean or null")
                old_paths.add(old)
        # Apply sources simultaneously, so a chain or swap in one diff does
        # not accidentally follow another rename from the same revision.
        for change in changes:
            if change["status"] == "R":
                before[change["old_path"]] = labels.get(change["path"], change["path"])
        labels = before
        if len(labels) > max_unique_paths:
            raise ValueError("unique historical paths exceeded the analysis budget")
        result.append(paths)
    return list(reversed(result))


def analyze_history_structural_tension(
    graph: Mapping[str, Any],
    history: Mapping[str, Any],
    *,
    minimum_cochange_commits: int = 1,
    hotspot_threshold: float = 0.5,
    excluded_path_patterns: Sequence[str] = (),
    max_history_records: int = 1000,
    max_paths_per_revision: int = 1000,
    max_unique_paths: int = 5000,
    max_pairs: int = 100000,
) -> dict[str, Any]:
    """Report pairs whose static and historical proximity disagree.

    Historical proximity is Jaccard co-change frequency over comparable
    revisions. Structural distance is unweighted shortest file hops in the
    undirected known-dependency graph. The tension score is the absolute gap
    between Jaccard proximity and ``1 / (1 + structural_distance)`` (zero
    structural proximity for disconnected files). It is a hotspot-ranking
    heuristic, not curvature, causation, or proof of a hidden dependency.

    Exclusions use case-sensitive fnmatch against the full projected path
    (``*`` can match ``/``). They remove ranking candidates, not static graph
    intermediates or comparable revisions. File context is descriptive only;
    change frequency and static degree do not reweight the score.
    """
    if (
        not isinstance(excluded_path_patterns, Sequence)
        or isinstance(excluded_path_patterns, (str, bytes))
        or any(
            not isinstance(pattern, str) or not pattern or "\0" in pattern
            for pattern in excluded_path_patterns
        )
    ):
        raise ValueError("excluded_path_patterns must be a sequence of non-empty pattern strings")
    patterns = sorted(set(excluded_path_patterns))
    if isinstance(minimum_cochange_commits, bool) or not isinstance(minimum_cochange_commits, int) or minimum_cochange_commits <= 0:
        raise ValueError("minimum_cochange_commits must be a positive integer")
    if (
        isinstance(hotspot_threshold, bool)
        or not isinstance(hotspot_threshold, (int, float))
        or not math.isfinite(float(hotspot_threshold))
        or not 0.0 <= hotspot_threshold <= 1.0
    ):
        raise ValueError("hotspot_threshold must be between 0 and 1")
    if min(max_history_records, max_paths_per_revision, max_unique_paths, max_pairs) <= 0:
        raise ValueError("history analysis budgets must be positive")
    records = history.get("records")
    if not isinstance(records, list):
        raise ValueError("history must contain a records list")
    if len(records) > max_history_records:
        raise ValueError("history record count exceeded the analysis budget")
    changed_path_sets = _history_changed_paths(records, max_paths_per_revision, max_unique_paths)
    graph_uncertainty = _graph_uncertainty(graph)

    cochange_counts: dict[tuple[str, str], int] = defaultdict(int)
    changed_in: dict[str, int] = defaultdict(int)
    matched_patterns: dict[str, list[str]] = {}
    comparable_revisions = 0
    excluded_root_records = 0
    excluded_incomplete_records = 0
    for record, changed_paths in zip(records, changed_path_sets):
        if not isinstance(record, Mapping):
            raise ValueError("history records must be mappings")
        revision = record.get("revision", {})
        uncertainty = record.get("extractor_uncertainty", {})
        if not isinstance(revision, Mapping):
            raise ValueError("record revision must be a mapping")
        if revision.get("comparison_parent_id") is None:
            excluded_root_records += 1
            continue
        if isinstance(uncertainty, Mapping) and uncertainty.get("status_after") == "incomplete/review":
            excluded_incomplete_records += 1
            continue
        if len(changed_paths) > max_paths_per_revision:
            raise ValueError("changed paths exceeded the per-revision analysis budget")
        comparable_revisions += 1
        for path in changed_paths:
            changed_in[path] += 1
            if path not in matched_patterns:
                matched_patterns[path] = [
                    pattern for pattern in patterns if fnmatchcase(path, pattern)
                ]
        if len(changed_in) > max_unique_paths:
            raise ValueError("unique changed paths exceeded the analysis budget")
        candidate_paths = sorted(path for path in changed_paths if not matched_patterns[path])
        for pair in combinations(candidate_paths, 2):
            cochange_counts[pair] += 1
            if len(cochange_counts) > max_pairs:
                raise ValueError("co-change pair count exceeded the analysis budget")

    adjacency = _static_file_adjacency(graph)
    files = [
        {
            "path": path,
            "changed_commits": changed_in[path],
            "change_frequency": round(changed_in[path] / comparable_revisions, 12),
            "static_degree": len(adjacency.get(path, set())),
            "excluded": bool(matched_patterns[path]),
            "matched_exclusion_patterns": matched_patterns[path],
        }
        for path in sorted(changed_in)
    ]
    files_by_path = {row["path"]: row for row in files}
    paths = [row["path"] for row in files if not row["excluded"]]
    static_distances = {path: _distances_from(path, adjacency) for path in paths}
    pairs = []
    for (left, right), both_count in cochange_counts.items():
        union_count = changed_in[left] + changed_in[right] - both_count
        historical_proximity = both_count / union_count if union_count else 0.0
        structural_distance = static_distances.get(left, {}).get(right)
        structural_proximity = (
            1.0 / (1.0 + structural_distance) if structural_distance is not None else 0.0
        )
        tension = abs(historical_proximity - structural_proximity)
        pairs.append({
            "left_path": left,
            "right_path": right,
            "cochange_commits": both_count,
            "left_changed_commits": changed_in[left],
            "right_changed_commits": changed_in[right],
            "left_change_frequency": files_by_path[left]["change_frequency"],
            "right_change_frequency": files_by_path[right]["change_frequency"],
            "left_static_degree": files_by_path[left]["static_degree"],
            "right_static_degree": files_by_path[right]["static_degree"],
            "historical_jaccard_proximity": round(historical_proximity, 12),
            "historical_jaccard_distance": round(1.0 - historical_proximity, 12),
            "structural_distance_hops": structural_distance,
            "structural_evidence": (
                "known_static_path" if structural_distance is not None
                else "static_path_not_found"
            ),
            "structural_proximity": round(structural_proximity, 12),
            "tension": round(tension, 12),
            "hotspot": both_count >= minimum_cochange_commits and tension >= hotspot_threshold,
        })
    pairs.sort(key=lambda row: (-row["tension"], -row["cochange_commits"], row["left_path"], row["right_path"]))

    return {
        "schema_version": 1,
        "analysis_kind": "geo_history_structural_tension_probe",
        "distance_definitions": {
            "structural": "undirected unweighted shortest hops over known static dependency edges",
            "historical": "Jaccard distance/proximity of changed-file sets over comparable revisions",
        },
        "tension_definition": "absolute gap between historical proximity and normalized structural proximity",
        "graph_uncertainty": graph_uncertainty,
        "comparable_revisions": comparable_revisions,
        "excluded_root_records": excluded_root_records,
        "excluded_incomplete_records": excluded_incomplete_records,
        "noise_filter": {
            "excluded_path_patterns": patterns,
            "excluded_path_count": sum(row["excluded"] for row in files),
            "excluded_path_revision_count": sum(
                row["changed_commits"] for row in files if row["excluded"]
            ),
            "matching_scope": "full projected path; case-sensitive fnmatch; * matches /",
            "static_paths_preserved": True,
        },
        "file_context_definitions": {
            "change_frequency": "changed commits / all comparable revisions, including revisions with only excluded files",
            "static_degree": "distinct neighboring files in the undirected known-dependency graph, including excluded files",
        },
        "files": files,
        "path_identity": (
            "git_changes_projected_to_final_revision"
            if any("file_changes" in record for record in records)
            else "legacy_path_based"
        ),
        "pair_count": len(pairs),
        "hotspot_count": sum(pair["hotspot"] for pair in pairs),
        "pairs": pairs,
        "limitations": [
            "Co-change is association, not causation or proof of a hidden dependency.",
            "The static graph is undirected for distance comparison and omits unknown relations.",
            "Pairs are only emitted when they co-changed at least once in comparable history.",
            "A high tension score is a review-prioritization heuristic, not geometric curvature or a safety verdict.",
            "Git file_changes use final-revision paths; deleted identities and content-identical renames are not counted as edits.",
            "Git rename identity is heuristic; legacy records without file_changes remain path-based.",
            "Exclusions are explicit ranking filters, not evidence that a file is irrelevant or safe.",
            "Change frequency and static degree are descriptive context, not automatic noise classification or score corrections.",
            "Static paths and unresolved-call summaries are evidence for review, not runtime-dependency or correctness verdicts.",
        ],
    }


if __name__ == "__main__":
    main()