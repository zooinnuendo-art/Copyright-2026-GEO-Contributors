"""Build bounded temporal graph records from a read-only Git revision series."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import tempfile
from typing import Any, Sequence

from tools.code_dependency_graph import build_dependency_graph
from tools.temporal_ast_delta import build_python_ast_delta
from tools.temporal_graph_history import build_temporal_snapshot_record


class GitHistoryError(ValueError):
    """Raised when Git history cannot be read within the declared contract."""


def _validate_git_python_path(path: str) -> PurePosixPath:
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        raise GitHistoryError("Git contains an unsafe Python path")
    relative = PurePosixPath(path)
    windows = PureWindowsPath(path)
    components = path.split("/")
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if (
        relative.is_absolute() or windows.is_absolute() or windows.drive
        or not relative.parts or any(part in {"", ".", ".."} for part in components)
        or any(
            part.endswith((" ", "."))
            or any(
                character in '<>:"|?*'
                or os.name == "nt" and (ord(character) < 32 or ord(character) == 127)
                for character in part
            )
            or part.split(".", 1)[0].rstrip(" .").upper() in reserved
            for part in components
        )
    ):
        raise GitHistoryError("Git contains an unsafe Python path")
    return relative


def _validate_git_python_paths(paths: Sequence[str]) -> None:
    files: dict[str, str] = {}
    directories: set[str] = set()
    for path in paths:
        _validate_git_python_path(path)
        parts = path.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index]).casefold()
            if prefix in files:
                raise GitHistoryError("Git Python file path collides with a directory")
            directories.add(prefix)
        folded_path = path.casefold()
        if folded_path in directories:
            raise GitHistoryError("Git Python file path collides with a directory")
        if folded_path in files and files[folded_path] != path:
            raise GitHistoryError("Git Python paths collide on Windows")
        files[folded_path] = path


def _git(
    repository: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: float,
    output_limit_bytes: int,
) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitHistoryError(f"Git command failed: {type(error).__name__}") from error
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitHistoryError(f"Git command failed: {detail or completed.returncode}")
    if len(completed.stdout) > output_limit_bytes:
        raise GitHistoryError("Git command output exceeded the configured byte limit")
    return completed.stdout


def _resolve_commit(repository: Path, revision: str, timeout_seconds: float) -> str:
    if not isinstance(revision, str) or not revision or "\0" in revision:
        raise GitHistoryError("revision must be a non-empty Git revision expression")
    output = _git(
        repository,
        ("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=256,
    )
    commit_id = output.decode("ascii", errors="strict").strip()
    if len(commit_id) not in {40, 64} or any(character not in "0123456789abcdef" for character in commit_id):
        raise GitHistoryError("Git returned an invalid commit object id")
    return commit_id


def _commit_info(repository: Path, commit_id: str, timeout_seconds: float) -> tuple[list[str], str]:
    parents_line = _git(
        repository,
        ("rev-list", "--parents", "-n", "1", commit_id),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=1024,
    ).decode("ascii", errors="strict").strip().split()
    if not parents_line or parents_line[0] != commit_id:
        raise GitHistoryError("Git returned inconsistent commit ancestry")
    tree_id = _git(
        repository,
        ("rev-parse", "--verify", "--end-of-options", f"{commit_id}^{{tree}}"),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=256,
    ).decode("ascii", errors="strict").strip()
    return parents_line[1:], tree_id


def _tree_python_blobs(
    repository: Path,
    commit_id: str,
    *,
    max_files: int,
    max_total_bytes: int,
    timeout_seconds: float,
    max_metadata_bytes: int,
) -> tuple[list[tuple[str, str, int, str]], list[dict[str, Any]]]:
    raw_tree = _git(
        repository,
        ("ls-tree", "-r", "-z", "-l", "--full-tree", commit_id),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=max_metadata_bytes,
    )
    blobs = []
    issues = []
    total_bytes = 0
    for entry in raw_tree.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode_bytes, object_type_bytes, object_id_bytes, size_bytes = metadata.split(b" ", 3)
            path = os.fsdecode(raw_path)
            object_type = object_type_bytes.decode("ascii")
            mode = mode_bytes.decode("ascii")
            object_id = object_id_bytes.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitHistoryError("Git returned a malformed tree entry") from error
        if not path.endswith(".py"):
            continue
        _validate_git_python_path(path)
        if object_type != "blob" or mode not in {"100644", "100755"}:
            issues.append({"kind": "unsupported_git_python_entry", "path": path, "mode": mode})
            continue
        try:
            size = int(size_bytes)
        except ValueError as error:
            raise GitHistoryError("Git returned an invalid Python blob size") from error
        if len(blobs) >= max_files:
            raise GitHistoryError("Python file count exceeded the configured limit")
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise GitHistoryError("Python blob bytes exceeded the configured limit")
        blobs.append((path, object_id, size, mode))
    _validate_git_python_paths([path for path, _object_id, _size, _mode in blobs])
    return blobs, issues


def _read_blobs(
    repository: Path,
    blobs: Sequence[tuple[str, str, int, str]],
    *,
    max_total_bytes: int,
    timeout_seconds: float,
) -> list[tuple[str, bytes, str]]:
    request = b"".join(object_id.encode("ascii") + b"\n" for _path, object_id, _size, _mode in blobs)
    output_limit = max_total_bytes + len(blobs) * 128 + 4096
    response = _git(
        repository,
        ("cat-file", "--batch"),
        input_bytes=request,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit,
    )
    results = []
    offset = 0
    for path, expected_id, expected_size, mode in blobs:
        header_end = response.find(b"\n", offset)
        if header_end < 0:
            raise GitHistoryError("Git returned a truncated blob header")
        header = response[offset:header_end].decode("ascii", errors="strict").split()
        if len(header) != 3 or header[0] != expected_id or header[1] != "blob":
            raise GitHistoryError("Git returned an unexpected blob object")
        try:
            size = int(header[2])
        except ValueError as error:
            raise GitHistoryError("Git returned an invalid blob size") from error
        if size != expected_size:
            raise GitHistoryError("Git tree and blob sizes disagree")
        offset = header_end + 1
        content_end = offset + size
        if content_end >= len(response) or response[content_end:content_end + 1] != b"\n":
            raise GitHistoryError("Git returned a truncated blob")
        results.append((path, response[offset:content_end], mode))
        offset = content_end + 1
    if offset != len(response):
        raise GitHistoryError("Git returned unexpected trailing blob data")
    return results


def _dependency_snapshot(
    repository: Path,
    commit_id: str,
    tree_id: str,
    *,
    max_files: int,
    max_total_bytes: int,
    max_nodes: int,
    max_edges: int,
    timeout_seconds: float,
    max_metadata_bytes: int,
    source_roots: list[str] | None = None,
    changed_files: list[str] | None = None,
    include_import_use_test_fallback: bool = True,
    snapshot_blobs: list[tuple[str, str, int, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes], set[str], bool]:
    blobs, extraction_issues = (snapshot_blobs, []) if snapshot_blobs is not None else _tree_python_blobs(
        repository,
        commit_id,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        timeout_seconds=timeout_seconds,
        max_metadata_bytes=max_metadata_bytes,
    )
    contents = _read_blobs(
        repository,
        blobs,
        max_total_bytes=max_total_bytes,
        timeout_seconds=timeout_seconds,
    )
    with tempfile.TemporaryDirectory(prefix="ain-git-snapshot-") as directory:
        root = Path(directory)
        _validate_git_python_paths([path for path, _content, _mode in contents])
        for relative_name, content, _mode in contents:
            relative = _validate_git_python_path(relative_name)
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.resolve().relative_to(root.resolve())
            except ValueError as error:
                raise GitHistoryError("Git Python path escapes the snapshot directory") from error
            target.write_bytes(content)
        try:
            graph = build_dependency_graph(
                root,
                changed_files=changed_files or [],
                max_hops=0,
                max_files=max_files,
                max_nodes=max_nodes,
                max_edges=max_edges,
                source_roots=source_roots,
                include_import_use_test_fallback=include_import_use_test_fallback,
            )
        except ValueError as error:
            raise GitHistoryError(f"snapshot {commit_id}: {error}") from error
    graph["root"] = f"git-tree://{tree_id}"
    if extraction_issues:
        graph["issues"] = sorted(
            [*graph["issues"], *extraction_issues],
            key=lambda issue: json.dumps(issue, sort_keys=True),
        )
        graph["status"] = "incomplete/review"
    graph_paths = {node["path"] for node in graph["nodes"] if node["type"] == "file"}
    sources = {path: content for path, content, _mode in contents if path in graph_paths}
    uncertain_paths = {
        issue["path"]
        for issue in graph["issues"]
        if "path" in issue and issue["kind"] in {"parse_error", "snapshot_read_error", "unsupported_git_python_entry"}
    }
    global_uncertainty = any(
        issue["kind"] in {"file_budget_exceeded", "node_budget_exceeded", "edge_budget_exceeded"}
        for issue in graph["issues"]
    )
    return graph, sources, uncertain_paths, global_uncertainty


def _empty_ast_delta(paths: set[str], reason: str) -> dict[str, Any]:
    return {
        "symbols": {"added": [], "removed": [], "changed": []},
        "uncertain_files": sorted(paths),
        "partially_uncertain_files": [],
        "uncertain_symbols": [],
        "issues": [{"kind": "ast_delta_skipped_incomplete_snapshot", "reason": reason}],
        "source_text_retained": False,
    }


def _file_changes(
    repository: Path,
    parent_id: str,
    commit_id: str,
    *,
    timeout_seconds: float,
    max_metadata_bytes: int,
    max_files: int,
    staged: bool = False,
) -> list[dict[str, Any]]:
    command = ("diff", "--cached") if staged else ("diff-tree", "--no-commit-id", "-r")
    revisions = (parent_id,) if staged else (parent_id, commit_id)
    raw = _git(
        repository,
        command + ("--name-status", "-z",
         "--no-ext-diff", "--no-textconv", "--find-renames=50%", f"-l{max_files}",
         "--diff-filter=ADMRT", *revisions, "--"),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=max_metadata_bytes,
    )
    fields = raw.split(b"\0")
    if fields.pop() != b"":
        raise GitHistoryError("Git returned unterminated file changes")
    changes = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", errors="strict")
        count = 2 if status.startswith("R") else 1
        if index + count >= len(fields):
            raise GitHistoryError("Git returned truncated file changes")
        paths = [os.fsdecode(value) for value in fields[index + 1:index + count + 1]]
        index += count + 1
        if status.startswith("R"):
            if not status[1:].isdigit() or not 0 <= int(status[1:]) <= 100:
                raise GitHistoryError("Git returned invalid rename similarity")
            old, new = paths
            if old.endswith(".py") and new.endswith(".py"):
                changes.append({
                    "status": "R", "old_path": old, "path": new,
                    "similarity_percent": int(status[1:]),
                })
            elif old.endswith(".py"):
                changes.append({"status": "D", "path": old})
            elif new.endswith(".py"):
                changes.append({"status": "A", "path": new})
        elif status in {"A", "D", "M", "T"}:
            if paths[0].endswith(".py"):
                changes.append({"status": status, "path": paths[0]})
        else:
            raise GitHistoryError("Git returned unsupported file change status")
    return changes


def _renamed_nodes(
    parent: dict[str, Any], current: dict[str, Any], renames: dict[str, str],
) -> list[dict[str, str]]:
    current_ids = {node["id"] for node in current["nodes"]}
    matches = []
    for node in parent["nodes"]:
        old_path = node.get("path")
        if old_path not in renames:
            continue
        node_id = node["id"]
        kind = node_id.split(":", 1)[0]
        prefix = f"{kind}:{old_path}"
        if node_id != prefix and not node_id.startswith(prefix + ":"):
            continue
        target = f"{kind}:{renames[old_path]}" + node_id[len(prefix):]
        if target in current_ids:
            matches.append({"before_id": node_id, "after_id": target})
    return sorted(matches, key=lambda item: item["before_id"])


def extract_git_temporal_history(
    repository: str | Path,
    revisions: Sequence[str],
    *,
    max_revisions: int = 20,
    max_files: int = 1000,
    max_total_bytes: int = 64 * 1024 * 1024,
    max_metadata_bytes: int = 32 * 1024 * 1024,
    max_nodes: int = 20000,
    max_edges: int = 50000,
    max_ast_symbols_per_file: int = 10000,
    max_symbol_ast_nodes_per_file: int = 200000,
    timeout_seconds: float = 30.0,
    include_final_snapshot: bool = False,
    source_roots: list[str] | None = None,
) -> dict[str, Any]:
    """Read a contiguous first-parent series without changing its repository."""
    repo = Path(repository).resolve()
    if not revisions or isinstance(revisions, (str, bytes)):
        raise GitHistoryError("provide at least one revision")
    if len(revisions) > max_revisions:
        raise GitHistoryError("revision count exceeded the configured limit")
    if min(
        max_files,
        max_total_bytes,
        max_metadata_bytes,
        max_nodes,
        max_edges,
        max_ast_symbols_per_file,
        max_symbol_ast_nodes_per_file,
    ) <= 0:
        raise GitHistoryError("all extraction budgets must be positive")
    if timeout_seconds <= 0:
        raise GitHistoryError("timeout_seconds must be positive")

    commit_ids = [_resolve_commit(repo, revision, timeout_seconds) for revision in revisions]
    if len(set(commit_ids)) != len(commit_ids):
        raise GitHistoryError("revision list must not contain duplicates")
    commit_info = [_commit_info(repo, commit_id, timeout_seconds) for commit_id in commit_ids]
    for index in range(1, len(commit_ids)):
        parents = commit_info[index][0]
        if not parents or parents[0] != commit_ids[index - 1]:
            raise GitHistoryError("revisions must form one contiguous first-parent series")

    records = []
    baseline_revision_id = None
    previous_snapshot = None
    previous_sources: dict[str, bytes] = {}
    previous_uncertain_paths: set[str] = set()
    previous_global_uncertainty = False
    previous_revision_id = None
    previous_sequence_index = None
    start_index = 0
    if commit_info[0][0]:
        baseline_revision_id = commit_info[0][0][0]
        _, baseline_tree_id = _commit_info(repo, baseline_revision_id, timeout_seconds)
        (
            previous_snapshot,
            previous_sources,
            previous_uncertain_paths,
            previous_global_uncertainty,
        ) = _dependency_snapshot(
            repo,
            baseline_revision_id,
            baseline_tree_id,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
            max_nodes=max_nodes,
            max_edges=max_edges,
            timeout_seconds=timeout_seconds,
            max_metadata_bytes=max_metadata_bytes,
            source_roots=source_roots,
        )
        previous_revision_id = baseline_revision_id
        previous_sequence_index = 0
        start_index = 1

    for input_index, (commit_id, (parents, tree_id)) in enumerate(zip(commit_ids, commit_info)):
        snapshot, sources, uncertain_paths, global_uncertainty = _dependency_snapshot(
            repo,
            commit_id,
            tree_id,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
            max_nodes=max_nodes,
            max_edges=max_edges,
            timeout_seconds=timeout_seconds,
            max_metadata_bytes=max_metadata_bytes,
            source_roots=source_roots,
        )
        sequence_index = start_index + input_index
        if previous_snapshot is None:
            record = build_temporal_snapshot_record(
                snapshot,
                revision_id=commit_id,
                sequence_index=sequence_index,
            )
        else:
            record = build_temporal_snapshot_record(
                snapshot,
                revision_id=commit_id,
                sequence_index=sequence_index,
                parent_revision_ids=parents,
                parent_snapshot=previous_snapshot,
                comparison_parent_revision_id=previous_revision_id,
                comparison_parent_sequence_index=previous_sequence_index,
            )
        changes = _file_changes(
            repo, previous_revision_id, commit_id,
            timeout_seconds=timeout_seconds,
            max_metadata_bytes=max_metadata_bytes,
            max_files=max_files,
        ) if previous_revision_id is not None else [
            {"status": "A", "path": path} for path in sorted(sources)
        ]
        renames = {
            change["old_path"]: change["path"]
            for change in changes if change["status"] == "R"
        }
        for change in changes:
            if change["status"] == "R":
                old, new = change["old_path"], change["path"]
                change["content_changed"] = (
                    previous_sources[old] != sources[new]
                    if old in previous_sources and new in sources else None
                )
        record["file_changes"] = changes
        record["rename_detection"] = {
            "method": "git_similarity", "threshold_percent": 50,
            "rename_limit": max_files,
            "limitations": [
                "Similarity is heuristic, not proof of file or symbol identity.",
                "Unmatched moves remain additions/deletions; edited-rename detection is bounded by rename_limit.",
            ],
        }
        record["delta"]["renamed_nodes"] = (
            _renamed_nodes(previous_snapshot, snapshot, renames)
            if previous_snapshot is not None else []
        )
        if previous_global_uncertainty or global_uncertainty:
            ast_delta = _empty_ast_delta(
                set(previous_sources) | set(sources) | previous_uncertain_paths | uncertain_paths,
                "parent_or_current_graph_incomplete",
            )
        else:
            try:
                ast_delta = build_python_ast_delta(
                    previous_sources,
                    sources,
                    uncertain_paths=previous_uncertain_paths | uncertain_paths,
                    max_files=max_files,
                    max_symbols_per_file=max_ast_symbols_per_file,
                    max_symbol_ast_nodes_per_file=max_symbol_ast_nodes_per_file,
                )
            except ValueError as error:
                raise GitHistoryError(f"AST delta failed closed: {error}") from error
        if renames:
            if previous_global_uncertainty or global_uncertainty:
                rename_aware = _empty_ast_delta(
                    {renames.get(path, path) for path in previous_sources} | set(sources),
                    "parent_or_current_graph_incomplete",
                )
            else:
                try:
                    normalized_parent_sources = {
                        renames.get(path, path): content for path, content in previous_sources.items()
                    }
                    if len(normalized_parent_sources) != len(previous_sources):
                        raise ValueError("rename destinations collide with existing parent paths")
                    rename_aware = build_python_ast_delta(
                        normalized_parent_sources,
                        sources,
                        uncertain_paths={
                            renames.get(path, path) for path in previous_uncertain_paths
                        } | uncertain_paths,
                        max_files=max_files,
                        max_symbols_per_file=max_ast_symbols_per_file,
                        max_symbol_ast_nodes_per_file=max_symbol_ast_nodes_per_file,
                    )
                except ValueError as error:
                    raise GitHistoryError(f"Rename-aware AST delta failed closed: {error}") from error
            ast_delta["rename_aware"] = rename_aware
        record["ast_delta"] = ast_delta
        record["source"] = {"kind": "git_commit", "commit_id": commit_id, "tree_id": tree_id}
        records.append(record)
        previous_snapshot = snapshot
        previous_sources = sources
        previous_uncertain_paths = uncertain_paths
        previous_global_uncertainty = global_uncertainty
        previous_revision_id = commit_id
        previous_sequence_index = sequence_index

    result = {
        "schema_version": 1,
        "analysis_kind": "git_temporal_dependency_history",
        "repository": str(repo),
        "selection": {
            "revision_ids": commit_ids,
            "first_parent_only": True,
            "baseline_revision_id": baseline_revision_id,
        },
        "records": records,
    }
    if include_final_snapshot:
        result["final_snapshot"] = previous_snapshot
    if source_roots:
        result["selection"]["source_roots"] = previous_snapshot["source_roots"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("revisions", nargs="+", help="Contiguous first-parent revisions, oldest first")
    parser.add_argument("--max-revisions", type=int, default=20)
    parser.add_argument("--max-files", type=int, default=1000)
    parser.add_argument("--source-root", action="append", default=[], help="Import root relative to every committed Python snapshot; repeatable")
    parser.add_argument("--max-total-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-metadata-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-nodes", type=int, default=20000)
    parser.add_argument("--max-edges", type=int, default=50000)
    parser.add_argument("--max-ast-symbols-per-file", type=int, default=10000)
    parser.add_argument("--max-symbol-ast-nodes-per-file", type=int, default=200000)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    arguments = parser.parse_args()
    try:
        result = extract_git_temporal_history(
            arguments.repository,
            arguments.revisions,
            max_revisions=arguments.max_revisions,
            max_files=arguments.max_files,
            max_total_bytes=arguments.max_total_bytes,
            max_metadata_bytes=arguments.max_metadata_bytes,
            max_nodes=arguments.max_nodes,
            max_edges=arguments.max_edges,
            max_ast_symbols_per_file=arguments.max_ast_symbols_per_file,
            max_symbol_ast_nodes_per_file=arguments.max_symbol_ast_nodes_per_file,
            timeout_seconds=arguments.timeout_seconds,
            source_roots=arguments.source_root,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()