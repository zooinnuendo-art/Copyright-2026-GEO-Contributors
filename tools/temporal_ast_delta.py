"""Summarize per-symbol AST changes without retaining source text."""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
from io import BytesIO
import tokenize
from pathlib import PurePosixPath
from typing import Any, Mapping


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _signature_dump(node: ast.AST) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        signature = {
            "args": ast.dump(node.args, include_attributes=False),
            "returns": ast.dump(node.returns, include_attributes=False) if node.returns else None,
            "decorators": [ast.dump(item, include_attributes=False) for item in node.decorator_list],
            "type_comment": node.type_comment,
        }
        return repr(signature)
    if isinstance(node, ast.ClassDef):
        signature = {
            "bases": [ast.dump(item, include_attributes=False) for item in node.bases],
            "keywords": [ast.dump(item, include_attributes=False) for item in node.keywords],
            "decorators": [ast.dump(item, include_attributes=False) for item in node.decorator_list],
        }
        return repr(signature)
    return None


def _summarize_nodes(
    nodes: list[ast.AST],
    *,
    max_symbol_ast_nodes: int,
    counted_nodes: list[int],
) -> dict[str, Any]:
    dumps = []
    node_counts: Counter[str] = Counter()
    total_nodes = 0
    for root in nodes:
        for item in ast.walk(root):
            total_nodes += 1
            counted_nodes[0] += 1
            if counted_nodes[0] > max_symbol_ast_nodes:
                raise ValueError("per-symbol AST node budget exceeded")
            node_counts[type(item).__name__] += 1
        dumps.append(ast.dump(root, include_attributes=False))
    normalized = "\n".join(dumps)
    summary = {
        "ast_sha256": _sha256(normalized),
        "node_count": total_nodes,
        "node_type_counts": dict(sorted(node_counts.items())),
    }
    if len(nodes) == 1:
        signature = _signature_dump(nodes[0])
        if signature is not None:
            summary["signature_sha256"] = _sha256(signature)
    return summary


def _parse_file_symbols(
    path: str,
    source: bytes,
    *,
    max_symbols: int,
    max_symbol_ast_nodes: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    try:
        encoding, _ = tokenize.detect_encoding(BytesIO(source).readline)
        text = source.decode(encoding)
        tree = ast.parse(text, filename=path, type_comments=True)
    except (SyntaxError, UnicodeError, LookupError) as error:
        return {}, [{"kind": "ast_parse_error", "path": path, "detail": type(error).__name__}]

    full_tree_nodes = sum(1 for _ in ast.walk(tree))
    if full_tree_nodes > max_symbol_ast_nodes:
        raise ValueError(f"AST node budget exceeded for {path}")

    symbols: dict[str, tuple[str, ast.AST | list[ast.AST]]] = {}
    duplicates = []

    class SymbolVisitor(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []

        def _visit_definition(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str):
            qualified_name = ".".join([*self.scope, node.name])
            symbol_kind = kind
            if kind == "function":
                filename = PurePosixPath(path).name
                is_test_file = filename.startswith("test_") or filename.endswith("_test.py")
                if is_test_file and node.name.startswith("test_"):
                    symbol_kind = "test"
            symbol_id = f"{symbol_kind}:{path}:{qualified_name}"
            if symbol_id in symbols:
                duplicates.append({"kind": "duplicate_symbol_id", "path": path, "symbol_id": symbol_id})
            else:
                if len(symbols) >= max_symbols:
                    raise ValueError(f"AST symbol budget exceeded for {path}")
                symbols[symbol_id] = (symbol_kind, node)
            self.scope.append(node.name)
            for child in node.body:
                self.visit(child)
            self.scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef):
            self._visit_definition(node, "class")

        def visit_FunctionDef(self, node: ast.FunctionDef):
            self._visit_definition(node, "function")

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            self._visit_definition(node, "function")

    visitor = SymbolVisitor()
    visitor.visit(tree)

    global_assignments: dict[str, list[ast.AST]] = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        names = {
            target.id
            for expression in targets
            for target in ast.walk(expression)
            if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
        }
        for name in names:
            global_assignments.setdefault(name, []).append(statement)
    for name, statements in global_assignments.items():
        symbol_id = f"global:{path}:{name}"
        if symbol_id in symbols:
            duplicates.append({"kind": "duplicate_symbol_id", "path": path, "symbol_id": symbol_id})
        else:
            if len(symbols) >= max_symbols:
                raise ValueError(f"AST symbol budget exceeded for {path}")
            symbols[symbol_id] = ("global_variable", statements)

    if len(symbols) > max_symbols:
        raise ValueError(f"AST symbol budget exceeded for {path}")
    if duplicates:
        return {}, duplicates

    counted_nodes = [full_tree_nodes]
    summaries = {
        symbol_id: {
            "symbol_id": symbol_id,
            "kind": kind,
            **_summarize_nodes(
                [node] if isinstance(node, ast.AST) else node,
                max_symbol_ast_nodes=max_symbol_ast_nodes,
                counted_nodes=counted_nodes,
            ),
        }
        for symbol_id, (kind, node) in sorted(symbols.items())
    }
    return summaries, []


def build_python_ast_delta(
    parent_sources: Mapping[str, bytes],
    current_sources: Mapping[str, bytes],
    *,
    uncertain_paths: set[str] | frozenset[str] = frozenset(),
    max_files: int = 1000,
    max_symbols_per_file: int = 10000,
    max_symbol_ast_nodes_per_file: int = 200000,
) -> dict[str, Any]:
    """Compare symbol AST summaries; omit paths whose extraction is uncertain."""
    paths = set(parent_sources) | set(current_sources) | set(uncertain_paths)
    if len(paths) > max_files:
        raise ValueError("AST file budget exceeded")
    if min(max_files, max_symbols_per_file, max_symbol_ast_nodes_per_file) <= 0:
        raise ValueError("AST budgets must be positive")

    parent_symbols: dict[str, dict[str, Any]] = {}
    current_symbols: dict[str, dict[str, Any]] = {}
    uncertain = set(uncertain_paths)
    issues = [
        {"kind": "ast_delta_skipped_uncertain_path", "path": path}
        for path in sorted(uncertain_paths)
    ]
    for path in sorted(set(parent_sources) | set(current_sources)):
        if path in uncertain:
            continue
        before, before_issues = _parse_file_symbols(
            path,
            parent_sources[path],
            max_symbols=max_symbols_per_file,
            max_symbol_ast_nodes=max_symbol_ast_nodes_per_file,
        ) if path in parent_sources else ({}, [])
        after, after_issues = _parse_file_symbols(
            path,
            current_sources[path],
            max_symbols=max_symbols_per_file,
            max_symbol_ast_nodes=max_symbol_ast_nodes_per_file,
        ) if path in current_sources else ({}, [])
        if before_issues or after_issues:
            uncertain.add(path)
            issues.extend(before_issues)
            issues.extend(after_issues)
            continue
        parent_symbols.update(before)
        current_symbols.update(after)

    before_ids = set(parent_symbols)
    after_ids = set(current_symbols)
    common_ids = sorted(before_ids & after_ids)
    return {
        "symbols": {
            "added": [current_symbols[symbol_id] for symbol_id in sorted(after_ids - before_ids)],
            "removed": [parent_symbols[symbol_id] for symbol_id in sorted(before_ids - after_ids)],
            "changed": [
                {
                    "symbol_id": symbol_id,
                    "before": parent_symbols[symbol_id],
                    "after": current_symbols[symbol_id],
                }
                for symbol_id in common_ids
                if parent_symbols[symbol_id] != current_symbols[symbol_id]
            ],
        },
        "uncertain_files": sorted(uncertain),
        "issues": issues,
        "source_text_retained": False,
    }