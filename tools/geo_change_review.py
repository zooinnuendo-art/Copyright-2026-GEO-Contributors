"""Review a committed or staged Python change using Git objects, without running code."""

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
from typing import Any

from tools.geo_boundary_transition_probe import analyze_boundary_transitions
from tools.git_temporal_history import (
    GitHistoryError,
    _commit_info,
    _dependency_snapshot,
    _empty_ast_delta,
    _file_changes,
    _git,
    _resolve_commit,
    extract_git_temporal_history,
)
from tools.temporal_ast_delta import build_python_ast_delta


def _index_entries(repo, timeout, metadata_limit):
    raw = _git(
        repo, ("ls-files", "--stage", "--full-name", "-z"),
        timeout_seconds=timeout, output_limit_bytes=metadata_limit,
    )
    entries = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise GitHistoryError("staged review requires an index without unmerged entries")
        path = os.fsdecode(path)
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise GitHistoryError("index contains an unsafe path")
        if path.endswith(".py"):
            if mode not in {"100644", "100755"} or set(oid) == {"0"}:
                raise GitHistoryError(f"unsupported or intent-to-add Python index entry: {path}")
            entries.append((path, oid, mode))
    return raw, entries


def _index_blobs(repo, entries, budgets):
    if len(entries) > budgets["max_files"]:
        raise GitHistoryError("Python file count exceeded the configured limit")
    output = _git(
        repo, ("cat-file", "--batch-check"),
        input_bytes=b"".join(oid.encode("ascii") + b"\n" for _, oid, _ in entries),
        timeout_seconds=budgets["timeout_seconds"],
        output_limit_bytes=budgets["max_metadata_bytes"],
    ).splitlines()
    if len(output) != len(entries):
        raise GitHistoryError("Git returned inconsistent index object metadata")
    blobs = []
    total = 0
    for (path, oid, mode), line in zip(entries, output):
        fields = line.decode("ascii").split()
        if len(fields) != 3 or fields[:2] != [oid, "blob"]:
            raise GitHistoryError("index object is not an available blob")
        size = int(fields[2])
        total += size
        if total > budgets["max_total_bytes"]:
            raise GitHistoryError("Python blob bytes exceeded the configured limit")
        blobs.append((path, oid, size, mode))
    return blobs


def _is_test_path(path: str) -> bool:
    relative = PurePosixPath(path)
    return (
        bool({"tests", "testing"} & set(relative.parts))
        or relative.name.startswith("test_")
        or relative.name.endswith("_test.py")
    )


def review_git_change(
    repository: str | Path,
    revision: str | None = None,
    *,
    staged: bool = False,
    source_roots: list[str] | None = None,
    extra_call_hops: int = 0,
    max_extra_visits: int = 1000,
    max_files: int = 1000,
    max_total_bytes: int = 64 * 1024 * 1024,
    max_metadata_bytes: int = 32 * 1024 * 1024,
    max_nodes: int = 20000,
    max_edges: int = 50000,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Return review evidence, not an impact oracle or an executable test plan."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    for value, name, minimum in (
        (extra_call_hops, "extra_call_hops", 0),
        (max_extra_visits, "max_extra_visits", 1),
        (max_files, "max_files", 1),
        (max_total_bytes, "max_total_bytes", 1),
        (max_metadata_bytes, "max_metadata_bytes", 1),
        (max_nodes, "max_nodes", 1),
        (max_edges, "max_edges", 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    repo = Path(repository).resolve()
    if staged == (revision is not None):
        raise ValueError("provide exactly one of revision or staged=True")
    if staged:
        repo = Path(os.fsdecode(_git(
            repo, ("rev-parse", "--show-toplevel"),
            timeout_seconds=timeout_seconds, output_limit_bytes=max_metadata_bytes,
        )).rstrip("\n")).resolve()
    commit = _resolve_commit(repo, "HEAD" if staged else revision, timeout_seconds)
    parents, tree = _commit_info(repo, commit, timeout_seconds)
    if not parents and not staged:
        raise GitHistoryError("change review requires a commit with a first parent")
    budgets = {
        "max_files": max_files,
        "max_total_bytes": max_total_bytes,
        "max_metadata_bytes": max_metadata_bytes,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
        "timeout_seconds": timeout_seconds,
        "source_roots": source_roots,
    }
    index_before = None
    snapshot_options = {}
    if staged:
        index_before, entries = _index_entries(repo, timeout_seconds, max_metadata_bytes)
        blobs = _index_blobs(repo, entries, budgets)
        identity = "index-sha256:" + hashlib.sha256(index_before).hexdigest()
        snapshot_options = {"snapshot_blobs": blobs}
        _parent, before_sources, before_uncertain, before_global = _dependency_snapshot(
            repo, commit, tree, **budgets,
        )
        final, after_sources, after_uncertain, after_global = _dependency_snapshot(
            repo, commit, identity, **budgets, **snapshot_options,
        )
        changes = _file_changes(
            repo, commit, commit, timeout_seconds=timeout_seconds,
            max_metadata_bytes=max_metadata_bytes, max_files=max_files, staged=True,
        )
        renamed = {change["old_path"]: change["path"] for change in changes if change["status"] == "R"}
        normalized_before = {renamed.get(path, path): content for path, content in before_sources.items()}
        if len(normalized_before) != len(before_sources):
            raise GitHistoryError("rename destinations collide with parent paths")
        delta = (
            _empty_ast_delta(set(before_sources) | set(after_sources), "parent_or_index_graph_incomplete")
            if before_global or after_global else
            build_python_ast_delta(
                normalized_before, after_sources,
                uncertain_paths={renamed.get(path, path) for path in before_uncertain} | after_uncertain,
                max_files=max_files,
            )
        )
        record = {"file_changes": changes, "ast_delta": delta}
        snapshot_revision, snapshot_tree = commit, identity
    else:
        history = extract_git_temporal_history(
            repo, [commit], include_final_snapshot=True, **budgets,
        )
        final = history["final_snapshot"]
        record = history["records"][0]
        snapshot_revision, snapshot_tree = commit, tree
    changes = record["file_changes"]
    production = sorted({change["path"] for change in changes if not _is_test_path(change["path"])})
    tests = sorted({change["path"] for change in changes if _is_test_path(change["path"])})
    delta = record["ast_delta"].get("rename_aware", record["ast_delta"])
    graphs = []
    if production:
        for enabled in (False, True):
            graph, _sources, _uncertain, _global = _dependency_snapshot(
                repo, snapshot_revision, snapshot_tree, changed_files=production,
                include_import_use_test_fallback=enabled, **budgets, **snapshot_options,
            )
            for key in ("root", "source_roots", "nodes", "edges"):
                if graph.get(key) != final.get(key):
                    raise GitHistoryError(f"review snapshot differs from history snapshot: {key}")
            graphs.append(graph)
        probe = analyze_boundary_transitions(
            *graphs, extra_call_hops=extra_call_hops,
            max_extra_visits=max_extra_visits, ast_delta=delta,
        )
    else:
        probe = None
    analyzed = graphs[0] if graphs else final
    if staged:
        index_after, _entries = _index_entries(repo, timeout_seconds, max_metadata_bytes)
        if index_after != index_before or _resolve_commit(repo, "HEAD", timeout_seconds) != commit:
            raise GitHistoryError("HEAD or index changed during staged review; retry on a stable index")
    return {
        "schema_version": 1,
        "analysis_kind": "experimental_staged_change_review" if staged else "experimental_committed_change_review",
        "source": {
            "repository": str(repo),
            "commit_id": None if staged else commit,
            "comparison_parent_id": commit if staged else parents[0],
            "tree_id": None if staged else tree,
            "source_roots": final.get("source_roots", []),
            "working_tree_used": False,
            "target_code_executed": False,
            "snapshot_alignment": "index_entries_and_head_verified" if staged else "same_commit_and_tree_verified",
            **({"index_entries_sha256": hashlib.sha256(index_before).hexdigest()} if staged else {}),
        },
        "status": analyzed["status"],
        "selection_status": "production_python_changes" if production else "no_production_python_changes",
        "production_max_hops": 0,
        "file_changes": changes,
        "changed_production_files": production,
        "changed_test_files": tests,
        "ast_delta": delta,
        "graph_summary": {
            "node_count": len(final["nodes"]),
            "edge_count": len(final["edges"]),
            "issues": analyzed["issues"],
            "incomplete_causes": analyzed["incomplete_causes"],
        },
        "review": probe,
        "limitations": [
            "Staged mode compares HEAD with stage-0 index blobs; unborn HEAD and unmerged entries are rejected."
            if staged else "Compares one commit with its first parent; root commits are rejected.",
            "Only changed Python production files seed selection; tests/testing directories and test_*/ *_test.py names are treated as tests or test support.",
            "Production scope is file-level at zero hops, not changed-line or changed-symbol scope.",
            "Deleted providers remain seeds and can produce changed_file_not_found scope issues.",
            "Source roots must exist in each extracted Python snapshot, including the parent.",
            "Git extraction is repeated for candidate scopes and checked against the pinned snapshot.",
            *(["Index entry hashes are checked before and after analysis; this is change detection, not an index lock. Intent-to-add entries follow Git cached-diff semantics and are not change seeds."] if staged else []),
            "Budgets apply per extraction or Git command, not as a global wall-clock limit.",
            "Static and fallback evidence does not prove runtime impact, completeness or test correctness.",
            "No working-tree analysis, package loading, test execution, candidate ranking or remote posting is performed.",
        ],
    }


def _text_report(report: dict[str, Any]) -> str:
    source = report["source"]
    lines = [
        f'GEO change review: {source["commit_id"]} (first parent {source["comparison_parent_id"]})'
        if source["commit_id"] else f'GEO staged review: index versus HEAD {source["comparison_parent_id"]}',
        f'Status: {report["status"]}; selection: {report["selection_status"]}',
        "Git object snapshots only; review evidence, not runtime impact proof.",
        "Changed production Python files:",
        *[f"  {json.dumps(path, ensure_ascii=True)}" for path in report["changed_production_files"]],
        "Changed symbols (AST):",
    ]
    for category in ("added", "changed", "removed"):
        for entry in report["ast_delta"]["symbols"][category]:
            lines.append(f'  {category}: {json.dumps(entry["symbol_id"], ensure_ascii=True)}')
    delta = report["ast_delta"]
    lines.append(
        f'AST uncertainty: {len(delta["uncertain_files"])} files, '
        f'{len(delta.get("partially_uncertain_files", []))} partial, '
        f'{len(delta.get("uncertain_symbols", []))} excluded symbol IDs'
    )
    review = report["review"]
    lines.append("Candidate tests (not ranked):")
    for candidate in review["candidate_tests"] if review else []:
        alignment = candidate["changed_symbol_alignment"]
        provenance = (
            "continuation" if candidate["continuation_added"] else
            "import-use fallback" if candidate["fallback_added"] else "static"
        )
        lines.append(
            f'  {json.dumps(candidate["test_file"], ensure_ascii=True)}: {provenance}; '
            f'{alignment["classification"]}; {candidate["evidence_status"]}'
        )
        for detail in alignment["direct_match_details"]:
            suffix = " [container includes changed descendants; descendant reference unproven]" if detail["container_ast_includes_changed_descendants"] else ""
            lines.append(f'    {detail["reference_kind"]}: {json.dumps(detail["symbol_id"], ensure_ascii=True)}{suffix}')
        evidence = candidate["import_use_fallback_evidence"]
        if evidence:
            kinds = sorted({kind for record in evidence["records"] for kind in record["reference_kinds"]})
            lines.append(
                f'    Import reference kinds: {", ".join(kinds)}; '
                f'{len(evidence["records"])}/{evidence["record_count"]} records shown'
            )
    summary = report["graph_summary"]
    lines.append(f'Graph issues: {len(summary["issues"])}; depth boundaries: {len(review["boundary_entries"]) if review else 0}')
    counts = Counter(issue["kind"] for issue in summary["issues"])
    for kind, count in sorted(counts.items()):
        lines.append(f"  {kind}: {count} issue records")
    lines.append("Use --format json for full reference records, boundaries and uncertainty details.")
    if not review:
        lines.append("  No production Python change seeds; candidate selection was not run.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--revision")
    selection.add_argument("--staged", action="store_true")
    parser.add_argument("--source-root", action="append", default=[])
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--extra-call-hops", type=int, default=0)
    parser.add_argument("--max-extra-visits", type=int, default=1000)
    parser.add_argument("--max-files", type=int, default=1000)
    parser.add_argument("--max-total-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-metadata-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-nodes", type=int, default=20000)
    parser.add_argument("--max-edges", type=int, default=50000)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    try:
        report = review_git_change(
            args.repository, args.revision, staged=args.staged, source_roots=args.source_root,
            extra_call_hops=args.extra_call_hops, max_extra_visits=args.max_extra_visits,
            max_files=args.max_files, max_total_bytes=args.max_total_bytes,
            max_metadata_bytes=args.max_metadata_bytes, max_nodes=args.max_nodes,
            max_edges=args.max_edges, timeout_seconds=args.timeout_seconds,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
          if args.format == "json" else _text_report(report))


if __name__ == "__main__":
    main()
