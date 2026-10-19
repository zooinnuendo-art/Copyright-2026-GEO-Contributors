"""Build a bounded, read-only heterogeneous dependency graph for Python snapshots."""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import sys
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


EXCLUDED_PARTS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules"}
DEPENDENCY_EDGE_TYPES = {"imports", "calls", "type_depends_on", "reads", "writes"}
MAX_TEST_CALL_HOPS = 8
GRAPH_SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "python_ast_v1"
CACHE_FORMAT_VERSION = 4
FILE_ARTIFACT_SCHEMA_VERSION = 1


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _file_signature(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.stat()
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _module_name(relative_path: Path) -> str:
    parts = list(relative_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class PythonDependencyGraphBuilder:
    """Extract conservative syntax-level relations; this is not whole-program analysis."""

    def __init__(self, root: Path, max_files: int = 1000, max_nodes: int = 20000, max_edges: int = 50000):
        self.root = root.resolve()
        self.max_files = max_files
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self.nodes: dict[str, dict[str, Any]] = {}
        self.node_ids_by_path: dict[str, list[str]] = defaultdict(list)
        self.edges: dict[tuple[str, str, str, int | None], dict[str, Any]] = {}
        self.issues: list[dict[str, Any]] = []
        self.files: dict[str, dict[str, Any]] = {}
        self.nodes_by_path_qualified: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.classes_by_path: dict[str, list[str]] = defaultdict(list)
        self.module_to_file: dict[str, str] = {}
        self.symbols_by_file: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.import_aliases: dict[str, dict[str, tuple[str, str | None]]] = defaultdict(dict)
        self.import_specs_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.function_data: list[dict[str, Any]] = []
        self.instance_attribute_types: dict[tuple[str, str], str | None] = {}
        self.unresolved_attribute_calls = 0
        self.unresolved_attribute_examples: list[dict[str, Any]] = []
        self.unresolved_simple_calls = 0
        self.unresolved_simple_examples: list[dict[str, Any]] = []
        self.incomplete = False

    def _mark_budget_exceeded(self, kind: str, **details: Any) -> None:
        self.incomplete = True
        if not any(issue["kind"] == kind for issue in self.issues):
            self.issues.append({"kind": kind, **details})

    def _add_node(self, node_id: str, node_type: str, **attributes: Any) -> None:
        if node_id in self.nodes:
            return
        if len(self.nodes) >= self.max_nodes:
            self._mark_budget_exceeded("node_budget_exceeded", limit=self.max_nodes)
            return
        self.nodes[node_id] = {"id": node_id, "type": node_type, **attributes}
        path = attributes.get("path")
        qualified_name = attributes.get("qualified_name")
        if path is not None:
            self.node_ids_by_path[path].append(node_id)
        if path is not None and qualified_name is not None:
            self.nodes_by_path_qualified[(path, qualified_name)].append(node_id)
            if node_type == "class":
                self.classes_by_path[path].append(node_id)

    def _add_edge(self, source: str, target: str, edge_type: str, line: int | None, confidence: float = 1.0) -> None:
        if source not in self.nodes or target not in self.nodes:
            if self.incomplete:
                return
            self.issues.append({"kind": "unresolved_graph_endpoint", "source": source, "target": target, "type": edge_type})
            return
        key = (source, target, edge_type, line)
        if key in self.edges:
            return
        if len(self.edges) >= self.max_edges:
            self._mark_budget_exceeded("edge_budget_exceeded", limit=self.max_edges)
            return
        edge = {
            "source": source,
            "target": target,
            "type": edge_type,
            "line": line,
            "extractor": EXTRACTOR_VERSION,
            "evidence": "static_syntax",
            "confidence": confidence,
        }
        self.edges[key] = edge

    def _python_files(self) -> list[Path]:
        paths = [
            path for path in self.root.rglob("*.py")
            if not any(part in EXCLUDED_PARTS for part in path.relative_to(self.root).parts)
        ]
        return sorted(paths, key=lambda path: path.relative_to(self.root).as_posix())

    def _snapshot_matches(self, paths: list[Path], signatures: dict[Path, tuple[int, int, int, int, int]]) -> bool:
        try:
            current_paths = self._python_files()
            same_file_set = [path.relative_to(self.root).as_posix() for path in current_paths] == [path.relative_to(self.root).as_posix() for path in paths]
            return same_file_set and all(_file_signature(path) == signatures[path] for path in paths)
        except OSError:
            return False

    def _import_specs(self, relative: Path, node: ast.AST) -> list[dict[str, Any]]:
        source_module = _module_name(relative)
        if isinstance(node, ast.Import):
            imports = [(alias.name, alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = source_module if relative.name == "__init__.py" else source_module.rpartition(".")[0]
                parts = package.split(".") if package else []
                parts = parts[: max(0, len(parts) - node.level + 1)]
                if node.module:
                    parts.extend(node.module.split("."))
                import_module = ".".join(part for part in parts if part)
            else:
                import_module = node.module or ""
            imports = [(import_module, alias.asname or alias.name, alias.name) for alias in node.names]
        else:
            return []
        return [
            {"module": target_module, "alias": alias_name, "symbol": symbol_name, "line": getattr(node, "lineno", None)}
            for target_module, alias_name, symbol_name in imports
        ]

    def _apply_import_specs(self, file_id: str, relative: Path, imports: list[dict[str, Any]]) -> None:
        self.import_specs_by_file[file_id] = [dict(spec) for spec in imports]
        for spec in imports:
            target_module = spec["module"]
            alias_name = spec["alias"]
            symbol_name = spec["symbol"]
            line = spec["line"]
            target_file = self.module_to_file.get(target_module)
            if symbol_name:
                child_module_file = self.module_to_file.get(f"{target_module}.{symbol_name}")
                if child_module_file is not None:
                    target_file = child_module_file
                    symbol_name = None
            if target_file is not None:
                self._add_edge(file_id, target_file, "imports", line)
                self.import_aliases[file_id][alias_name] = (target_file, symbol_name)
                continue
            top_level = target_module.split(".", 1)[0] if target_module else ""
            if top_level in self.local_roots:
                self.issues.append({
                    "kind": "unresolved_local_import",
                    "path": relative.as_posix(),
                    "line": line,
                    "target": target_module,
                })

    def _record_import(self, file_id: str, relative: Path, node: ast.AST) -> None:
        self._apply_import_specs(file_id, relative, self._import_specs(relative, node))

    @staticmethod
    def _expression_chain(expression: ast.expr) -> dict[str, Any] | None:
        attributes = []
        current = expression
        while isinstance(current, ast.Attribute):
            attributes.insert(0, current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return None
        return {"base": current.id, "attributes": attributes}

    def _function_syntax_facts(self, function_id: str, file_id: str, function: ast.FunctionDef | ast.AsyncFunctionDef, module_name: str, module_globals: set[str]) -> dict[str, Any]:
        walked = list(ast.walk(function))
        calls = []
        names = []
        declared_globals = set()
        invoked_getattr_positions = {
            (node.func.lineno, node.func.col_offset)
            for node in walked
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Call)
            and isinstance(node.func.func, ast.Name)
            and node.func.func.id == "getattr"
        }
        for node in walked:
            if isinstance(node, ast.Global):
                declared_globals.update(node.names)
            elif isinstance(node, ast.Name):
                names.append({"name": node.id, "context": type(node.ctx).__name__, "line": node.lineno})
            elif isinstance(node, ast.Call):
                simple_name = node.func.id if isinstance(node.func, ast.Name) else None
                chain = self._expression_chain(node.func) if isinstance(node.func, ast.Attribute) else None
                getattr_call = node if simple_name == "getattr" else (
                    node.func
                    if isinstance(node.func, ast.Call)
                    and isinstance(node.func.func, ast.Name)
                    and node.func.func.id == "getattr"
                    else None
                )
                literal_getattr = None
                if (
                    getattr_call is not None
                    and len(getattr_call.args) == 2
                    and not getattr_call.keywords
                    and isinstance(getattr_call.args[0], ast.Name)
                    and isinstance(getattr_call.args[1], ast.Constant)
                    and isinstance(getattr_call.args[1].value, str)
                ):
                    literal_getattr = {
                        "base": getattr_call.args[0].id,
                        "attribute": getattr_call.args[1].value,
                    }
                calls.append({
                    "simple_name": simple_name,
                    "chain": chain,
                    "literal_getattr": literal_getattr,
                    "getattr_result_is_invoked": (
                        simple_name == "getattr"
                        and (node.lineno, node.col_offset) in invoked_getattr_positions
                    ),
                    "line": node.lineno,
                    "expression": ast.unparse(node.func),
                })

        local_bindings = {
            argument.arg
            for argument in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
        }
        if function.args.vararg is not None:
            local_bindings.add(function.args.vararg.arg)
        if function.args.kwarg is not None:
            local_bindings.add(function.args.kwarg.arg)

        class LocalBindingVisitor(ast.NodeVisitor):
            def visit_FunctionDef(visitor_self, node: ast.FunctionDef) -> None:
                local_bindings.add(node.name)

            def visit_AsyncFunctionDef(visitor_self, node: ast.AsyncFunctionDef) -> None:
                local_bindings.add(node.name)

            def visit_ClassDef(visitor_self, node: ast.ClassDef) -> None:
                local_bindings.add(node.name)

            def visit_Lambda(visitor_self, node: ast.Lambda) -> None:
                return

            def visit_Name(visitor_self, node: ast.Name) -> None:
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    local_bindings.add(node.id)

            def visit_Global(visitor_self, node: ast.Global) -> None:
                declared_globals.update(node.names)

            def visit_Nonlocal(visitor_self, node: ast.Nonlocal) -> None:
                local_bindings.update(node.names)

            def visit_Import(visitor_self, node: ast.Import) -> None:
                return

            def visit_ImportFrom(visitor_self, node: ast.ImportFrom) -> None:
                return

        binding_visitor = LocalBindingVisitor()
        for statement in function.body:
            binding_visitor.visit(statement)
        local_bindings.difference_update(declared_globals)
        shadowed_getattr_argument_refs = [
            {"name": node.args[0].id, "line": node.lineno}
            for node in walked
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and "getattr" in local_bindings
            and node.args
            and isinstance(node.args[0], ast.Name)
        ]

        field_constructors = []

        def record_assignment(target: ast.expr, value: ast.expr | None) -> None:
            if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name):
                return
            if target.value.id not in {"self", "cls"} or not isinstance(value, ast.Call):
                return
            chain = self._expression_chain(value.func) if isinstance(value.func, (ast.Name, ast.Attribute)) else None
            if chain:
                field_constructors.append({"field": target.attr, "constructor": chain})

        class AssignmentVisitor(ast.NodeVisitor):
            def visit_FunctionDef(visitor_self, node: ast.FunctionDef) -> None:
                return

            def visit_AsyncFunctionDef(visitor_self, node: ast.AsyncFunctionDef) -> None:
                return

            def visit_Lambda(visitor_self, node: ast.Lambda) -> None:
                return

            def visit_Assign(visitor_self, node: ast.Assign) -> None:
                for target in node.targets:
                    record_assignment(target, node.value)
                visitor_self.generic_visit(node.value)

            def visit_AnnAssign(visitor_self, node: ast.AnnAssign) -> None:
                record_assignment(node.target, node.value)
                if node.value is not None:
                    visitor_self.generic_visit(node.value)

        visitor = AssignmentVisitor()
        for statement in function.body:
            visitor.visit(statement)
        return {
            "function_id": function_id,
            "file_id": file_id,
            "path": self.files[file_id]["path"],
            "module_name": module_name,
            "qualified_name": self.nodes[function_id]["qualified_name"],
            "node_type": self.nodes[function_id]["type"],
            "declared_globals": sorted(declared_globals),
            "module_globals": sorted(module_globals),
            "names": names,
            "calls": calls,
            "local_bindings": sorted(local_bindings),
            "shadowed_getattr_argument_refs": shadowed_getattr_argument_refs,
            "field_constructors": field_constructors,
        }

    def _collect_definitions(self, file_id: str, relative: Path, tree: ast.Module) -> None:
        module_globals: set[str] = set()
        for statement in tree.body:
            targets: list[ast.expr] = []
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                module_globals.update(node.id for node in ast.walk(target) if isinstance(node, ast.Name))

        module_name = _module_name(relative)

        class Collector(ast.NodeVisitor):
            def __init__(collector_self) -> None:
                collector_self.scope: list[str] = []
                collector_self.scope_ids: list[str] = []

            def visit_ClassDef(collector_self, node: ast.ClassDef) -> None:
                qualified = ".".join([*collector_self.scope, node.name])
                class_id = f"class:{relative.as_posix()}:{qualified}"
                self._add_node(class_id, "class", path=relative.as_posix(), qualified_name=qualified, line=node.lineno)
                parent_id = collector_self.scope_ids[-1] if collector_self.scope_ids else file_id
                self._add_edge(parent_id, class_id, "contains", node.lineno)
                self.symbols_by_file[file_id][node.name].append(class_id)
                collector_self.scope.append(node.name)
                collector_self.scope_ids.append(class_id)
                collector_self.generic_visit(node)
                collector_self.scope_ids.pop()
                collector_self.scope.pop()

            def _visit_function(collector_self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                qualified = ".".join([*collector_self.scope, node.name])
                is_test_file = relative.name.startswith("test_") or relative.name.endswith("_test.py")
                is_test = is_test_file and node.name.startswith("test_")
                node_type = "test" if is_test else "function"
                function_id = f"{node_type}:{relative.as_posix()}:{qualified}"
                self._add_node(function_id, node_type, path=relative.as_posix(), qualified_name=qualified, line=node.lineno, async_function=isinstance(node, ast.AsyncFunctionDef))
                parent_id = collector_self.scope_ids[-1] if collector_self.scope_ids else file_id
                self._add_edge(parent_id, function_id, "contains", node.lineno)
                self.symbols_by_file[file_id][node.name].append(function_id)
                if function_id in self.nodes:
                    self.function_data.append(self._function_syntax_facts(function_id, file_id, node, module_name, module_globals))
                collector_self.scope.append(node.name)
                collector_self.scope_ids.append(function_id)
                for child in node.body:
                    collector_self.visit(child)
                collector_self.scope_ids.pop()
                collector_self.scope.pop()

            visit_FunctionDef = _visit_function
            visit_AsyncFunctionDef = _visit_function

        Collector().visit(tree)
        for name in sorted(module_globals):
            global_id = f"global:{relative.as_posix()}:{name}"
            self._add_node(global_id, "global_variable", path=relative.as_posix(), qualified_name=name)
            self._add_edge(file_id, global_id, "contains", None)

        self.module_globals = getattr(self, "module_globals", {})
        self.module_globals[file_id] = module_globals

    def _capture_file_artifact(self, file_id: str, imports: list[dict[str, Any]]) -> dict[str, Any]:
        relative_path = self.files[file_id]["path"]
        local_nodes = [
            node for node_id, node in sorted(self.nodes.items())
            if node_id != file_id and node.get("path") == relative_path
        ]
        local_node_ids = {node["id"] for node in local_nodes}
        local_edges = [
            edge for edge in self.edges.values()
            if edge["type"] == "contains"
            and edge["source"] in ({file_id} | local_node_ids)
            and edge["target"] in local_node_ids
        ]
        return {
            "artifact_schema_version": FILE_ARTIFACT_SCHEMA_VERSION,
            "path": relative_path,
            "nodes": local_nodes,
            "edges": sorted(local_edges, key=lambda edge: (edge["source"], edge["target"], edge["line"] or 0)),
            "symbols": {name: list(symbol_ids) for name, symbol_ids in sorted(self.symbols_by_file[file_id].items())},
            "module_globals": sorted(self.module_globals.get(file_id, set())),
            "imports": imports,
            "functions": [fact for fact in self.function_data if fact["file_id"] == file_id],
        }

    def _apply_file_artifact(self, file_id: str, artifact: dict[str, Any], relative: Path) -> None:
        if artifact.get("artifact_schema_version") != FILE_ARTIFACT_SCHEMA_VERSION or artifact.get("path") != relative.as_posix():
            raise ValueError("file artifact schema or path mismatch")
        for node in artifact["nodes"]:
            attributes = {key: value for key, value in node.items() if key not in {"id", "type"}}
            self._add_node(node["id"], node["type"], **attributes)
        for edge in artifact["edges"]:
            self._add_edge(edge["source"], edge["target"], edge["type"], edge["line"], edge["confidence"])
        for name, symbol_ids in artifact["symbols"].items():
            self.symbols_by_file[file_id][name].extend(symbol_ids)
        self.module_globals[file_id] = set(artifact["module_globals"])
        self.function_data.extend(artifact["functions"])
        self._apply_import_specs(file_id, relative, artifact["imports"])

    def _resolve_imported_symbol(self, file_id: str, name: str) -> str | None:
        imported = self.import_aliases[file_id].get(name)
        if imported:
            target_file, symbol_name = imported
            if symbol_name:
                candidates = self.symbols_by_file[target_file].get(symbol_name, [])
                return candidates[0] if len(candidates) == 1 else None
            return target_file
        candidates = self.symbols_by_file[file_id].get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_literal_getattr(
        self,
        file_id: str,
        function: dict[str, Any],
        call: dict[str, Any],
    ) -> str | None:
        access = call.get("literal_getattr")
        if not isinstance(access, dict):
            return None
        base_name = access.get("base")
        attribute = access.get("attribute")
        if not isinstance(base_name, str) or not isinstance(attribute, str):
            return None
        if base_name in function.get("local_bindings", []):
            return None
        module_file = self._resolve_imported_symbol(file_id, base_name)
        if self.nodes.get(module_file or "", {}).get("type") != "file":
            return None
        candidates = [
            node_id
            for node_id in self.symbols_by_file.get(module_file, {}).get(attribute, [])
            if self.nodes.get(node_id, {}).get("type") in {"class", "function", "test"}
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_attribute_call(self, file_id: str, function_id: str, attribute: ast.Attribute) -> str | None:
        return self._resolve_attribute_chain(file_id, function_id, self._expression_chain(attribute))

    @staticmethod
    def _expression_chain(expression: ast.expr) -> dict[str, Any] | None:
        attribute_parts = []
        current = expression
        while isinstance(current, ast.Attribute):
            attribute_parts.insert(0, current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return None
        return {"base": current.id, "attributes": attribute_parts}

    def _resolve_attribute_chain(self, file_id: str, function_id: str, chain: dict[str, Any] | None) -> str | None:
        if chain is None:
            return None
        attribute_parts = chain["attributes"]
        base_name = chain["base"]

        if base_name in {"self", "cls"}:
            function_node = self.nodes.get(function_id, {})
            qualified_name = function_node.get("qualified_name", "")
            path = function_node.get("path", "")
            parent_classes = [
                self.nodes[node_id]
                for node_id in self.classes_by_path.get(path, [])
                if qualified_name.startswith(f"{self.nodes[node_id]['qualified_name']}.")
            ]
            if parent_classes and len(attribute_parts) == 1:
                owner = max(parent_classes, key=lambda node: len(node["qualified_name"]))
                target_name = f"{owner['qualified_name']}.{attribute_parts[0]}"
                matches = [
                    node_id for node_id in self.nodes_by_path_qualified.get((path, target_name), [])
                    if self.nodes[node_id].get("type") in {"function", "test"}
                ]
                return matches[0] if len(matches) == 1 else None
            if parent_classes and len(attribute_parts) == 2:
                owner = max(parent_classes, key=lambda node: len(node["qualified_name"]))
                field_type = self.instance_attribute_types.get((owner["id"], attribute_parts[0]))
                if not field_type:
                    return None
                field_class = self.nodes.get(field_type, {})
                target_name = f"{field_class.get('qualified_name', '')}.{attribute_parts[1]}"
                matches = [
                    node_id for node_id in self.nodes_by_path_qualified.get((field_class.get("path", ""), target_name), [])
                    if self.nodes[node_id].get("type") in {"function", "test"}
                ]
                return matches[0] if len(matches) == 1 else None
            return None

        imported = self.import_aliases[file_id].get(base_name)
        if not imported:
            return None
        target_file, imported_symbol = imported
        if imported_symbol:
            imported_node = self._resolve_imported_symbol(file_id, base_name)
            if not imported_node or self.nodes.get(imported_node, {}).get("type") != "class":
                return None
            class_node = self.nodes[imported_node]
            target_name = f"{class_node['qualified_name']}.{'.'.join(attribute_parts)}"
            matches = [
                node_id for node_id in self.nodes_by_path_qualified.get((class_node.get("path", ""), target_name), [])
                if self.nodes[node_id].get("type") in {"function", "test"}
            ]
            return matches[0] if len(matches) == 1 else None

        target_module = _module_name(Path(self.files[target_file]["path"]))
        for prefix_length in range(len(attribute_parts), -1, -1):
            module_name = ".".join([target_module, *attribute_parts[:prefix_length]]).strip(".")
            module_file = self.module_to_file.get(module_name)
            if module_file is None:
                continue
            remaining = attribute_parts[prefix_length:]
            if not remaining:
                return module_file
            symbol_matches = self.symbols_by_file[module_file].get(remaining[0], [])
            if len(symbol_matches) != 1:
                return None
            target = symbol_matches[0]
            for member_name in remaining[1:]:
                owner = self.nodes.get(target, {})
                if owner.get("type") != "class":
                    return None
                qualified = f"{owner['qualified_name']}.{member_name}"
                member_matches = [
                    node_id for node_id in self.nodes_by_path_qualified.get((owner.get("path", ""), qualified), [])
                    if self.nodes[node_id].get("type") in {"function", "test"}
                ]
                if len(member_matches) != 1:
                    return None
                target = member_matches[0]
            return target
        return None

    def _resolve_constructor_class(self, file_id: str, expression: dict[str, Any]) -> str | None:
        if not expression["attributes"]:
            target = self._resolve_imported_symbol(file_id, expression["base"])
            return target if self.nodes.get(target, {}).get("type") == "class" else None
        target = self._resolve_attribute_chain(file_id, "", expression)
        return target if self.nodes.get(target, {}).get("type") == "class" else None

    def _collect_instance_attribute_types(self) -> None:
        for function in self.function_data:
            file_id = function["file_id"]
            qualified_name = function["qualified_name"]
            path = function["path"]
            owners = [
                self.nodes[node_id]
                for node_id in self.classes_by_path.get(path, [])
                if qualified_name.startswith(f"{self.nodes[node_id]['qualified_name']}.")
            ]
            if not owners:
                continue
            owner = max(owners, key=lambda node: len(node["qualified_name"]))

            for assignment in function["field_constructors"]:
                class_id = self._resolve_constructor_class(file_id, assignment["constructor"])
                if not class_id:
                    continue
                key = (owner["id"], assignment["field"])
                if key not in self.instance_attribute_types:
                    self.instance_attribute_types[key] = class_id
                elif self.instance_attribute_types[key] != class_id:
                    self.instance_attribute_types[key] = None

    def _record_unresolved_attribute(self, file_id: str, function_id: str, line: int, expression: str) -> None:
        self.unresolved_attribute_calls += 1
        if len(self.unresolved_attribute_examples) < 20:
            self.unresolved_attribute_examples.append({
                "path": self.files[file_id]["path"],
                "line": line,
                "source": function_id,
                "expression": expression,
            })

    def _record_unresolved_simple_call(self, file_id: str, function_id: str, line: int, name: str) -> None:
        self.unresolved_simple_calls += 1
        if len(self.unresolved_simple_examples) < 20:
            self.unresolved_simple_examples.append({
                "path": self.files[file_id]["path"],
                "line": line,
                "source": function_id,
                "name": name,
            })

    def _relation_resolution_key(self, file_id: str, file_artifact: dict[str, Any]) -> str:
        import_interfaces = []
        for alias, (target_file, symbol_name) in sorted(self.import_aliases[file_id].items()):
            symbols = self.symbols_by_file.get(target_file, {})
            if symbol_name:
                symbol_names = [symbol_name]
            else:
                symbol_names = sorted(symbols)
            interface_nodes = []
            for name in symbol_names:
                for node_id in symbols.get(name, []):
                    node = self.nodes[node_id]
                    interface_nodes.append((node_id, node["type"]))
                    if node["type"] == "class":
                        interface_nodes.extend(
                            (member_id, self.nodes[member_id]["type"])
                            for member_id in self.nodes_by_path_qualified.get((node.get("path", ""), node["qualified_name"]), [])
                            if member_id != node_id
                        )
            import_interfaces.append((alias, target_file, symbol_name, sorted(interface_nodes)))

        path = self.files[file_id]["path"]
        field_types = sorted(
            (class_id, field_name, target_class)
            for (class_id, field_name), target_class in self.instance_attribute_types.items()
            if self.nodes.get(class_id, {}).get("path") == path
        )
        resolution_context = {
            "artifact_schema_version": FILE_ARTIFACT_SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "extractor_sha256": file_artifact.get("extractor_sha256"),
            "source_sha256": self.files[file_id]["sha256"],
            "path": path,
            "import_interfaces": import_interfaces,
            "field_types": field_types,
            "local_symbols": sorted(
                (node_id, self.nodes[node_id]["type"])
                for node_id in self.node_ids_by_path.get(path, [])
            ),
        }
        return _sha256(_canonical_json_bytes(resolution_context))

    def _collect_relations(self, file_artifacts: dict[str, dict[str, Any]] | None = None, force_recompute_file_ids: set[str] | None = None) -> dict[str, int]:
        relation_cache_hits = 0
        relation_cache_misses = 0
        functions_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for function in self.function_data:
            functions_by_file[function["file_id"]].append(function)

        for file_id, functions in functions_by_file.items():
            file_path = self.files[file_id]["path"]
            artifact_entry = file_artifacts.get(file_path) if file_artifacts is not None else None
            relation_key = self._relation_resolution_key(file_id, artifact_entry) if artifact_entry is not None else None
            cached_relations = artifact_entry.get("resolved_relations") if artifact_entry is not None else None
            if isinstance(cached_relations, dict) and file_id not in (force_recompute_file_ids or set()):
                cached_payload = {key: value for key, value in cached_relations.items() if key != "facts_sha256"}
                if (
                    cached_relations.get("resolution_key") == relation_key
                    and cached_relations.get("facts_sha256") == _sha256(_canonical_json_bytes(cached_payload))
                ):
                    for edge in cached_relations["edges"]:
                        self._add_edge(edge["source"], edge["target"], edge["type"], edge.get("line"), edge.get("confidence", 1.0))
                    self.unresolved_attribute_calls += cached_relations["unresolved_attribute_calls"]
                    self.unresolved_simple_calls += cached_relations["unresolved_simple_calls"]
                    self.unresolved_attribute_examples.extend(cached_relations["unresolved_attribute_examples"][: max(0, 20 - len(self.unresolved_attribute_examples))])
                    self.unresolved_simple_examples.extend(cached_relations["unresolved_simple_examples"][: max(0, 20 - len(self.unresolved_simple_examples))])
                    relation_cache_hits += 1
                    continue

            relation_cache_misses += int(artifact_entry is not None)
            start_edges = set(self.edges)
            start_attribute_count = self.unresolved_attribute_calls
            start_simple_count = self.unresolved_simple_calls
            start_attribute_examples = len(self.unresolved_attribute_examples)
            start_simple_examples = len(self.unresolved_simple_examples)
            for function in functions:
                function_id = function["function_id"]
                module_globals = set(function["module_globals"])
                declared_globals = set(function["declared_globals"])
                for name in function["names"]:
                    if name["name"] in module_globals:
                        global_id = f"global:{self.files[file_id]['path']}:{name['name']}"
                        if name["context"] == "Load":
                            self._add_edge(function_id, global_id, "reads", name["line"], confidence=0.9)
                        elif name["context"] == "Store" and name["name"] in declared_globals:
                            self._add_edge(function_id, global_id, "writes", name["line"], confidence=0.9)

                    target = self._resolve_imported_symbol(file_id, name["name"])
                    if target and self.nodes.get(target, {}).get("type") == "class":
                        self._add_edge(function_id, target, "type_depends_on", name["line"], confidence=0.75)

                for call in function["calls"]:
                    target_name = call["simple_name"]
                    if target_name == "getattr":
                        is_shadowed = (
                            "getattr" in function.get("local_bindings", [])
                            or self._resolve_imported_symbol(file_id, "getattr") is not None
                        )
                        if not is_shadowed:
                            if call.get("getattr_result_is_invoked"):
                                continue
                            else:
                                self._record_unresolved_attribute(
                                    file_id,
                                    function_id,
                                    call["line"],
                                    call["expression"],
                                )
                            continue
                    if target_name is None and call.get("literal_getattr") is not None:
                        is_shadowed = (
                            "getattr" in function.get("local_bindings", [])
                            or self._resolve_imported_symbol(file_id, "getattr") is not None
                        )
                        target = None if is_shadowed else self._resolve_literal_getattr(file_id, function, call)
                        if target:
                            self._add_edge(function_id, target, "calls", call["line"], confidence=0.6)
                            if function["node_type"] == "test" and self.nodes.get(target, {}).get("type") != "test":
                                self._add_edge(function_id, target, "tests", call["line"], confidence=0.55)
                        else:
                            self._record_unresolved_attribute(
                                file_id,
                                function_id,
                                call["line"],
                                call["expression"],
                            )
                        continue
                    if target_name:
                        if target_name in function.get("local_bindings", []):
                            if target_name not in vars(builtins):
                                self._record_unresolved_simple_call(
                                    file_id,
                                    function["function_id"],
                                    call["line"],
                                    target_name,
                                )
                            continue
                        target = self._resolve_imported_symbol(file_id, target_name)
                        if target:
                            self._add_edge(function_id, target, "calls", call["line"], confidence=0.85)
                            if function["node_type"] == "test" and self.nodes.get(target, {}).get("type") != "test":
                                self._add_edge(function_id, target, "tests", call["line"], confidence=0.8)
                        elif target_name not in vars(builtins):
                            self._record_unresolved_simple_call(file_id, function_id, call["line"], target_name)
                    elif call["chain"] is not None:
                        target = self._resolve_attribute_chain(file_id, function_id, call["chain"])
                        if target:
                            self._add_edge(function_id, target, "calls", call["line"], confidence=0.7)
                            if function["node_type"] == "test" and self.nodes.get(target, {}).get("type") != "test":
                                self._add_edge(function_id, target, "tests", call["line"], confidence=0.65)
                        else:
                            self._record_unresolved_attribute(file_id, function_id, call["line"], call["expression"])
                    else:
                        self._record_unresolved_attribute(file_id, function_id, call["line"], call["expression"])

            if artifact_entry is not None and not self.incomplete:
                relation_edges = [
                    edge for key, edge in self.edges.items()
                    if key not in start_edges
                    and self.nodes.get(edge["source"], {}).get("path") == file_path
                    and edge["type"] in {"calls", "tests", "type_depends_on", "reads", "writes"}
                ]
                relation_payload = {
                    "resolution_key": relation_key,
                    "edges": relation_edges,
                    "unresolved_attribute_calls": self.unresolved_attribute_calls - start_attribute_count,
                    "unresolved_simple_calls": self.unresolved_simple_calls - start_simple_count,
                    "unresolved_attribute_examples": self.unresolved_attribute_examples[start_attribute_examples:],
                    "unresolved_simple_examples": self.unresolved_simple_examples[start_simple_examples:],
                }
                artifact_entry["resolved_relations"] = {
                    **relation_payload,
                    "facts_sha256": _sha256(_canonical_json_bytes(relation_payload)),
                }

        return {"hits": relation_cache_hits, "misses": relation_cache_misses}

    def build(
        self,
        changed_files: list[str] | None = None,
        max_hops: int = 2,
        cache_path: Path | None = None,
        recompute_affected_importers: bool = False,
        include_import_use_test_fallback: bool = True,
    ) -> dict[str, Any]:
        if not 0 <= max_hops <= 2:
            raise ValueError("max_hops must be between 0 and 2")
        if min(self.max_files, self.max_nodes, self.max_edges) <= 0:
            raise ValueError("graph budgets must be positive")
        paths = self._python_files()
        cache_metadata: dict[str, Any] = {"status": "disabled" if cache_path is None else "miss", "reason": "disabled" if cache_path is None else "not_checked"}
        snapshot_bytes: dict[Path, bytes] = {}
        snapshot_signatures: dict[Path, tuple[int, int, int, int, int]] = {}
        reusable_file_artifacts: dict[str, dict[str, Any]] = {}
        previous_file_artifacts: dict[str, dict[str, Any]] = {}
        next_file_artifacts: dict[str, dict[str, Any]] = {}
        dependency_invalidation: dict[str, Any] = {"changed_modules": [], "affected_importers": []}
        file_artifact_hits = 0
        file_artifact_misses = 0
        extractor_hash = None
        cache_key = None
        if len(paths) > self.max_files:
            paths = paths[: self.max_files]
            self.incomplete = True
            self.issues.append({"kind": "file_budget_exceeded", "limit": self.max_files})
            if cache_path is not None:
                cache_metadata = {"status": "bypassed", "reason": "file_budget_exceeded"}

        if cache_path is not None and not self.incomplete:
            manifest = []
            for path in paths:
                try:
                    before = _file_signature(path)
                    content = path.read_bytes()
                    after = _file_signature(path)
                except OSError as error:
                    self.incomplete = True
                    self.issues.append({"kind": "snapshot_read_error", "path": path.relative_to(self.root).as_posix(), "detail": type(error).__name__})
                    cache_metadata = {"status": "bypassed", "reason": "snapshot_read_error"}
                    break
                if before != after or len(content) != after[2]:
                    self.incomplete = True
                    self.issues.append({"kind": "snapshot_changed_during_read", "path": path.relative_to(self.root).as_posix()})
                    cache_metadata = {"status": "bypassed", "reason": "snapshot_changed_during_read"}
                    break
                snapshot_bytes[path] = content
                snapshot_signatures[path] = after
                manifest.append({"path": path.relative_to(self.root).as_posix(), "sha256": _sha256(content)})
            extractor_path = Path(__file__).resolve()
            extractor_hash = _sha256(extractor_path.read_bytes())
            cache_identity = {
                "cache_format_version": CACHE_FORMAT_VERSION,
                "graph_schema_version": GRAPH_SCHEMA_VERSION,
                "file_artifact_schema_version": FILE_ARTIFACT_SCHEMA_VERSION,
                "extractor_version": EXTRACTOR_VERSION,
                "extractor_sha256": extractor_hash,
                "root": str(self.root),
                "python_version": list(sys.version_info[:3]),
                "ast_version": getattr(ast, "__version__", "stdlib"),
                "max_hops": max_hops,
                "max_files": self.max_files,
                "max_nodes": self.max_nodes,
                "max_edges": self.max_edges,
                "changed_files": sorted(Path(path).as_posix() for path in (changed_files or [])),
                "recompute_affected_importers": recompute_affected_importers,
                "include_import_use_test_fallback": include_import_use_test_fallback,
                "snapshot": manifest,
            }
            cache_key = _sha256(_canonical_json_bytes(cache_identity))
            try:
                if self.incomplete:
                    raise RuntimeError("snapshot_not_stable")
                envelope = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_graph = envelope.get("graph")
                if envelope.get("cache_format_version") != CACHE_FORMAT_VERSION:
                    cache_metadata = {"status": "miss", "reason": "cache_format_mismatch", "key": cache_key}
                else:
                    current_paths = {path.relative_to(self.root).as_posix(): path for path in paths}
                    cached_artifacts = envelope.get("file_artifacts", {})
                    if isinstance(cached_artifacts, dict):
                        for relative_path, entry in cached_artifacts.items():
                            if not isinstance(entry, dict):
                                continue
                            artifact_json = entry.get("artifact_json")
                            if (
                                entry.get("artifact_schema_version") == FILE_ARTIFACT_SCHEMA_VERSION
                                and entry.get("extractor_sha256") == extractor_hash
                                and entry.get("python_version") == list(sys.version_info[:3])
                                and entry.get("ast_version") == getattr(ast, "__version__", "stdlib")
                                and isinstance(artifact_json, str)
                                and entry.get("artifact_sha256") == _sha256(artifact_json.encode("utf-8"))
                            ):
                                previous_file_artifacts[relative_path] = entry
                                expected_source_hash = next((item["sha256"] for item in manifest if item["path"] == relative_path), None)
                                if relative_path in current_paths and entry.get("source_sha256") == expected_source_hash:
                                    reusable_file_artifacts[relative_path] = entry

                    previous_hashes = {
                        item["path"]: item["sha256"]
                        for item in envelope.get("manifest", [])
                        if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str)
                    }
                    current_hashes = {item["path"]: item["sha256"] for item in manifest}
                    changed_paths = {
                        path for path in set(previous_hashes) | set(current_hashes)
                        if previous_hashes.get(path) != current_hashes.get(path)
                    }
                    changed_modules = sorted({
                        _module_name(Path(path))
                        for path in changed_paths
                        if path.endswith(".py") and _module_name(Path(path))
                    })
                    affected_importers = set()
                    for importer_path, entry in previous_file_artifacts.items():
                        if importer_path not in current_paths:
                            continue
                        artifact = json.loads(entry["artifact_json"])
                        for import_spec in artifact.get("imports", []):
                            imported_module = import_spec.get("module", "")
                            if any(imported_module == module or imported_module.startswith(f"{module}.") for module in changed_modules):
                                affected_importers.add(importer_path)
                                break
                    dependency_invalidation = {
                        "changed_modules": changed_modules,
                        "affected_importers": sorted(affected_importers),
                    }

                    if envelope.get("cache_key") == cache_key:
                        if not isinstance(cached_graph, dict) or envelope.get("graph_sha256") != _sha256(_canonical_json_bytes(cached_graph)):
                            cache_metadata = {"status": "miss", "reason": "cached_graph_integrity_failure", "key": cache_key}
                        elif cached_graph.get("schema_version") != GRAPH_SCHEMA_VERSION or cached_graph.get("root") != str(self.root):
                            cache_metadata = {"status": "miss", "reason": "cached_graph_schema_mismatch", "key": cache_key}
                        elif self._snapshot_matches(paths, snapshot_signatures):
                            cached_graph["cache"] = {"status": "hit", "reason": "exact_snapshot_match", "key": cache_key}
                            return cached_graph
                        else:
                            self.incomplete = True
                            self.issues.append({"kind": "snapshot_changed_before_cache_return"})
                            cache_metadata = {"status": "bypassed", "reason": "snapshot_changed_before_cache_return", "key": cache_key}
                    else:
                        cache_metadata = {"status": "miss", "reason": "snapshot_or_configuration_changed", "key": cache_key}
            except FileNotFoundError:
                cache_metadata = {"status": "miss", "reason": "cache_not_found", "key": cache_key}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                cache_metadata = {"status": "miss", "reason": "cache_unreadable_or_invalid", "key": cache_key}
            except RuntimeError:
                pass
            if not self.incomplete and not self._snapshot_matches(paths, snapshot_signatures):
                self.incomplete = True
                self.issues.append({"kind": "snapshot_changed_before_parse"})
                cache_metadata = {"status": "bypassed", "reason": "snapshot_changed_before_parse", "key": cache_key}

        for path in paths:
            relative = path.relative_to(self.root)
            file_id = f"file:{relative.as_posix()}"
            content = snapshot_bytes.get(path)
            if content is None:
                content = path.read_bytes()
            self.files[file_id] = {"path": relative.as_posix(), "sha256": _sha256(content)}
            self._add_node(file_id, "file", path=relative.as_posix(), sha256=_sha256(content))
            module_name = _module_name(relative)
            if module_name:
                self.module_to_file[module_name] = file_id
        self.local_roots = {name.split(".", 1)[0] for name in self.module_to_file}
        self.module_globals: dict[str, set[str]] = {}

        for path in paths:
            relative = path.relative_to(self.root)
            file_id = f"file:{relative.as_posix()}"
            relative_path = relative.as_posix()
            artifact_entry = reusable_file_artifacts.get(relative_path) if cache_path is not None else None
            if artifact_entry is not None:
                try:
                    artifact = json.loads(artifact_entry["artifact_json"])
                    self._apply_file_artifact(file_id, artifact, relative)
                    file_artifact_hits += 1
                    next_file_artifacts[relative_path] = artifact_entry
                    continue
                except (KeyError, TypeError, ValueError):
                    artifact_entry = None

            file_artifact_misses += int(cache_path is not None)
            try:
                content = snapshot_bytes.get(path)
                source = content.decode("utf-8") if content is not None else path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative.as_posix())
            except (SyntaxError, UnicodeDecodeError) as error:
                self.incomplete = True
                self.issues.append({"kind": "parse_error", "path": relative.as_posix(), "detail": str(error)})
                continue
            import_specs = [
                spec
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for spec in self._import_specs(relative, node)
            ]
            self._apply_import_specs(file_id, relative, import_specs)
            self._collect_definitions(file_id, relative, tree)
            if cache_path is not None and extractor_hash is not None:
                artifact = self._capture_file_artifact(file_id, import_specs)
                artifact_json = _canonical_json_bytes(artifact).decode("utf-8")
                artifact_entry = {
                    "source_sha256": self.files[file_id]["sha256"],
                    "artifact_schema_version": FILE_ARTIFACT_SCHEMA_VERSION,
                    "extractor_sha256": extractor_hash,
                    "python_version": list(sys.version_info[:3]),
                    "ast_version": getattr(ast, "__version__", "stdlib"),
                    "artifact_sha256": _sha256(artifact_json.encode("utf-8")),
                    "artifact_json": artifact_json,
                }
                next_file_artifacts[relative_path] = artifact_entry

        self._collect_instance_attribute_types()
        dependency_invalidation = self._dependency_invalidation_metadata(changed_files or [])
        scope_seed = list(changed_files or [])
        if recompute_affected_importers:
            scope_seed = list(dict.fromkeys(scope_seed + dependency_invalidation.get("affected_importers", [])))
        selected_relation_file_ids = {
            file_id
            for file_id, payload in self.files.items()
            if payload["path"] in {Path(path).as_posix() for path in scope_seed}
        } if recompute_affected_importers else set()
        relation_cache_counts = self._collect_relations(
            next_file_artifacts if cache_path is not None else None,
            selected_relation_file_ids,
        )
        if self.unresolved_attribute_calls:
            self.issues.append({
                "kind": "unresolved_attribute_call_summary",
                "count": self.unresolved_attribute_calls,
                "examples": self.unresolved_attribute_examples,
            })
        if self.unresolved_simple_calls:
            self.issues.append({
                "kind": "unresolved_simple_call_summary",
                "count": self.unresolved_simple_calls,
                "examples": self.unresolved_simple_examples,
            })
        selected_scope, scope_issues = self._scope(
            scope_seed,
            max_hops,
            include_import_use_test_fallback=include_import_use_test_fallback,
        )
        if recompute_affected_importers:
            scope_seed = sorted(set(scope_seed) | set(selected_scope["files"]))
        self.issues.extend(scope_issues)
        status = "incomplete/review" if self.incomplete or scope_issues else "snapshot_only/review_required"
        global_incomplete_kinds = {
            "edge_budget_exceeded",
            "file_budget_exceeded",
            "node_budget_exceeded",
            "parse_error",
            "snapshot_changed_before_cache_return",
            "snapshot_changed_before_parse",
            "snapshot_changed_during_read",
            "snapshot_read_error",
        }
        global_incomplete_causes = [
            issue for issue in self.issues
            if issue["kind"] in global_incomplete_kinds
        ]
        scope_incomplete_causes = list(scope_issues)
        cache_metadata["dependency_invalidation"] = dependency_invalidation
        cache_metadata["selection_seed"] = sorted(scope_seed)
        if cache_path is not None:
            cache_metadata["file_artifacts"] = {"hits": file_artifact_hits, "misses": file_artifact_misses}
            cache_metadata["relation_facts"] = relation_cache_counts
        cache_metadata["dependency_invalidation"] = dependency_invalidation
        result = {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "analysis_kind": "python_snapshot_only",
            "root": str(self.root),
            "python_ast_version": f"{ast.__name__}:{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
            "scope": {"max_hops": max_hops, "changed_files": sorted(changed_files or []), "selection_seed": sorted(scope_seed), **selected_scope},
            "status": status,
            "incomplete_causes": {
                "global": sorted(global_incomplete_causes, key=lambda item: json.dumps(item, sort_keys=True)),
                "scope": sorted(scope_incomplete_causes, key=lambda item: json.dumps(item, sort_keys=True)),
            },
            "nodes": [self.nodes[node_id] for node_id in sorted(self.nodes)],
            "edges": [self.edges[key] for key in sorted(self.edges)],
            "issues": sorted(self.issues, key=lambda item: json.dumps(item, sort_keys=True)),
            "limitations": [
                "No commit history, CI labels, semantic embeddings, or safety envelope are analyzed.",
                "Call, type, and global data-flow edges are conservative syntax-level approximations.",
                "This artifact selects review scope; it does not approve changes or prove correctness.",
            ],
        }
        result["cache"] = cache_metadata
        if cache_path is not None and cache_key is not None and result["status"] == "snapshot_only/review_required":
            cache_graph = {key: value for key, value in result.items() if key != "cache"}
            envelope = {
                "cache_format_version": CACHE_FORMAT_VERSION,
                "cache_key": cache_key,
                "graph_sha256": _sha256(_canonical_json_bytes(cache_graph)),
                "graph": cache_graph,
                "file_artifacts": next_file_artifacts,
                "manifest": [{"path": path.relative_to(self.root).as_posix(), "sha256": self.files[f"file:{path.relative_to(self.root).as_posix()}"]["sha256"]} for path in paths],
            }
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp", delete=False) as temporary:
                    temporary.write(json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
                    temporary.write("\n")
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, cache_path)
            except OSError as error:
                result["cache"] = {"status": "write_failed", "reason": type(error).__name__, "key": cache_key}
        return result

    def _dependency_invalidation_metadata(self, changed_files: list[str]) -> dict[str, Any]:
        normalized = {Path(path).as_posix() for path in changed_files}
        changed_modules = sorted({
            module_name
            for path in normalized
            if path.endswith(".py")
            for module_name in [ _module_name(Path(path)) ]
            if module_name
        })
        if not changed_modules:
            return {"changed_modules": [], "affected_importers": []}
        affected_importers = set()
        for file_id, file in self.files.items():
            file_path = file["path"]
            for target_file, _ in self.import_aliases.get(file_id, {}).values():
                target_module = _module_name(Path(self.files[target_file]["path"])) if target_file in self.files else ""
                if any(target_module == module_name or target_module.startswith(f"{module_name}.") for module_name in changed_modules):
                    affected_importers.add(file_path)
                    break
        return {
            "changed_modules": changed_modules,
            "affected_importers": sorted(affected_importers),
        }

    def _scope(
        self,
        changed_files: list[str],
        max_hops: int,
        *,
        include_import_use_test_fallback: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        normalized = {Path(path).as_posix() for path in changed_files}
        changed_ids = {node_id for node_id, file in self.files.items() if file["path"] in normalized}
        issues = []
        missing = sorted(normalized - {self.files[node_id]["path"] for node_id in changed_ids})
        for path in missing:
            issues.append({"kind": "changed_file_not_found", "path": path})

        file_adjacency: dict[str, set[str]] = defaultdict(set)
        file_for_node = {node_id: node_id for node_id in self.files}
        test_file_ids = {
            node_id for node_id, file in self.files.items()
            if "tests" in Path(file["path"]).parts or Path(file["path"]).name.startswith("test_")
        }
        for node_id, node in self.nodes.items():
            if node["type"] != "file":
                owner = f"file:{node['path']}"
                if owner in self.files:
                    file_for_node[node_id] = owner
        for edge in self.edges.values():
            if edge["type"] not in DEPENDENCY_EDGE_TYPES:
                continue
            if self.nodes.get(edge["source"], {}).get("type") == "test" or self.nodes.get(edge["target"], {}).get("type") == "test":
                continue
            source_file = file_for_node.get(edge["source"])
            target_file = file_for_node.get(edge["target"])
            if source_file in test_file_ids or target_file in test_file_ids:
                continue
            if source_file and target_file and source_file != target_file:
                file_adjacency[source_file].add(target_file)
                file_adjacency[target_file].add(source_file)

        distances = {node_id: 0 for node_id in changed_ids}
        queue = deque(changed_ids)
        while queue:
            current = queue.popleft()
            if distances[current] >= max_hops:
                continue
            for neighbor in sorted(file_adjacency.get(current, set())):
                if neighbor not in distances:
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)

        scoped_files = set(distances)
        scoped_paths = {self.files[file_id]["path"] for file_id in scoped_files}
        scoped_nodes = {
            node_id
            for path in scoped_paths
            for node_id in self.node_ids_by_path.get(path, [])
        }
        linked_test_files = set()
        calls_by_source: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges.values():
            if edge["type"] == "calls":
                calls_by_source[edge["source"]].append(edge["target"])
        test_nodes = [node_id for node_id, node in self.nodes.items() if node["type"] == "test"]
        for test_node in test_nodes:
            test_path = self.nodes[test_node]["path"]
            pending = [(test_node, 0)]
            visited: dict[str, int] = {}
            reaches_scoped_code = False
            while pending and not reaches_scoped_code:
                current, call_hops = pending.pop()
                if current in visited and visited[current] <= call_hops:
                    continue
                visited[current] = call_hops
                for target in calls_by_source.get(current, []):
                    target_node = self.nodes.get(target, {})
                    if call_hops >= MAX_TEST_CALL_HOPS:
                        if visited.get(target, call_hops + 1) <= call_hops:
                            continue
                        issues.append({
                            "kind": "test_call_depth_limit_reached",
                            "path": test_path,
                            "limit": MAX_TEST_CALL_HOPS,
                            "terminal_node": current,
                            "untraversed_target": target,
                        })
                        continue
                    if call_hops < MAX_TEST_CALL_HOPS:
                        if target_node.get("path") in scoped_paths and target_node.get("type") != "test":
                            reaches_scoped_code = True
                            break
                    if (
                        call_hops < MAX_TEST_CALL_HOPS
                        and target_node.get("type") in {"function", "class"}
                    ):
                        pending.append((target, call_hops + 1))
            if reaches_scoped_code:
                test_file = f"file:{test_path}"
                linked_test_files.add(test_file)
                scoped_nodes.update(self.node_ids_by_path.get(test_path, []))

        for edge in self.edges.values():
            if edge["type"] != "tests":
                continue
            source_node = self.nodes.get(edge["source"], {})
            target_node = self.nodes.get(edge["target"], {})
            if source_node.get("type") == "test" and target_node.get("path") in scoped_paths:
                test_file = f"file:{source_node['path']}"
                linked_test_files.add(test_file)
                scoped_nodes.update(self.node_ids_by_path.get(source_node["path"], []))

        if include_import_use_test_fallback:
            test_references_by_file: dict[str, set[str]] = defaultdict(set)
            for function in self.function_data:
                if function["node_type"] != "test":
                    continue
                shadowed_getattr_refs = {
                    (reference["name"], reference["line"])
                    for reference in function.get("shadowed_getattr_argument_refs", [])
                }
                test_references_by_file[function["path"]].update(
                    name["name"]
                    for name in function["names"]
                    if name["name"] not in function.get("local_bindings", [])
                    and (name["name"], name["line"]) not in shadowed_getattr_refs
                )
                test_references_by_file[function["path"]].update(
                    call["simple_name"]
                    for call in function["calls"]
                    if call["simple_name"]
                    and call["simple_name"] not in function.get("local_bindings", [])
                )
            scoped_modules = {
                _module_name(Path(path))
                for path in scoped_paths
                if path.endswith(".py")
            }
            changed_modules = {
                _module_name(Path(path))
                for path in normalized
                if path.endswith(".py")
            }
            candidate_modules = scoped_modules | changed_modules
            for file_id, imports in self.import_specs_by_file.items():
                test_path = self.files[file_id]["path"]
                if test_path not in test_references_by_file:
                    continue
                references = test_references_by_file[test_path]
                if any(
                    spec.get("alias") in references
                    and any(
                        module_name
                        and (
                            spec.get("module") == module_name
                            or (
                                spec.get("symbol")
                                and f"{spec.get('module')}.{spec.get('symbol')}" == module_name
                            )
                        )
                        for module_name in candidate_modules
                    )
                    for spec in imports
                ):
                    test_file = f"file:{test_path}"
                    linked_test_files.add(test_file)
                    scoped_nodes.update(self.node_ids_by_path.get(test_path, []))
        return ({
            "files": sorted(self.files[file_id]["path"] for file_id in scoped_files | linked_test_files),
            "distances": {self.files[file_id]["path"]: distance for file_id, distance in sorted(distances.items())},
            "linked_test_files": sorted(self.files[file_id]["path"] for file_id in linked_test_files),
            "node_ids": sorted(scoped_nodes),
        }, issues)


def build_dependency_graph(
    root: str | Path,
    changed_files: list[str] | None = None,
    max_hops: int = 2,
    max_files: int = 1000,
    max_nodes: int = 20000,
    max_edges: int = 50000,
    cache_path: str | Path | None = None,
    recompute_affected_importers: bool = False,
    include_import_use_test_fallback: bool = True,
) -> dict[str, Any]:
    builder = PythonDependencyGraphBuilder(Path(root), max_files=max_files, max_nodes=max_nodes, max_edges=max_edges)
    return builder.build(
        changed_files=changed_files,
        max_hops=max_hops,
        cache_path=Path(cache_path) if cache_path is not None else None,
        recompute_affected_importers=recompute_affected_importers,
        include_import_use_test_fallback=include_import_use_test_fallback,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Python source snapshot root")
    parser.add_argument("--changed", action="append", default=[], help="Changed path relative to root; repeatable")
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--max-files", type=int, default=1000)
    parser.add_argument("--max-nodes", type=int, default=20000)
    parser.add_argument("--max-edges", type=int, default=50000)
    parser.add_argument("--cache", type=Path, help="Optional exact-snapshot JSON cache; any Python file change invalidates it")
    parser.add_argument("--recompute-affected-importers", action="store_true", help="Expand scope to include files that import changed modules, while keeping default exact-graph behavior unless requested")
    parser.add_argument("--no-import-use-test-fallback", action="store_false", dest="include_import_use_test_fallback", help="Disable test selection based on use of imports from scoped modules")
    parser.set_defaults(include_import_use_test_fallback=True)
    arguments = parser.parse_args()
    result = build_dependency_graph(
        arguments.root,
        changed_files=arguments.changed,
        max_hops=arguments.max_hops,
        max_files=arguments.max_files,
        max_nodes=arguments.max_nodes,
        max_edges=arguments.max_edges,
        cache_path=arguments.cache,
        recompute_affected_importers=arguments.recompute_affected_importers,
        include_import_use_test_fallback=arguments.include_import_use_test_fallback,
    )
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()