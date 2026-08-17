"""基于 Python AST 生成可重复的 AutoSlice 架构快照。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "architecture_baseline.json"
SCHEMA_VERSION = 1
LONGEST_FUNCTION_LIMIT = 25
LOW_LEVEL_MODULE_PREFIX = "autoslice"
FORBIDDEN_LOW_LEVEL_TARGETS = ("app", "subtitle_workflow", "topic_engine")

# 这些目录只保存本地环境、用户媒体或生成产物，不属于源码扫描范围。
EXCLUDED_DIRECTORY_NAMES = frozenset({
    ".git",
    ".idea",
    ".test-tmp",
    ".venv",
    ".vscode",
    "__pycache__",
    "covers",
    "local",
    "models",
    "output",
    "recordings",
    "screenshots",
    "stickers",
    "submissions",
    "timelines",
    "venv",
})
EXCLUDED_DIRECTORY_PREFIXES = (".codex-tmp-",)


def _normalise_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_excluded_directory(name: str) -> bool:
    return (
        name in EXCLUDED_DIRECTORY_NAMES
        or name.startswith(EXCLUDED_DIRECTORY_PREFIXES)
    )


def discover_python_files(root: Path) -> tuple[list[Path], list[Path]]:
    """只在 ``root`` 内发现产品源码和测试源码，不跟随目录链接。"""
    root = root.resolve()
    production_files: list[Path] = []
    test_files: list[Path] = []
    for current_dir, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not _is_excluded_directory(name)
            and not Path(current_dir, name).is_symlink()
        )
        current_path = Path(current_dir)
        relative_parts = current_path.relative_to(root).parts
        in_test_package = "tests" in relative_parts
        for file_name in sorted(file_names):
            if not file_name.endswith(".py"):
                continue
            path = current_path / file_name
            if path.is_symlink():
                continue
            if in_test_package or file_name.startswith("test_"):
                test_files.append(path)
            else:
                production_files.append(path)
    return production_files, test_files


def module_name_for_path(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _read_python_source(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _definition_kind(node: ast.AST) -> str:
    if isinstance(node, ast.AsyncFunctionDef):
        return "async_function"
    if isinstance(node, ast.FunctionDef):
        return "function"
    return "class"


def _definition_record(node: ast.AST) -> dict[str, object]:
    return {
        "name": node.name,
        "kind": _definition_kind(node),
        "line": node.lineno,
        "end_line": node.end_lineno,
    }


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.functions: list[dict[str, object]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        end_line = node.end_lineno or node.lineno
        self.functions.append({
            "qualname": ".".join((*self.scope, node.name)),
            "kind": _definition_kind(node),
            "line": node.lineno,
            "end_line": end_line,
            "line_count": end_line - node.lineno + 1,
        })
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _absolute_from_import(
        module_name: str,
        is_package: bool,
        imported_module: str | None,
        level: int) -> str:
    if level == 0:
        return imported_module or ""
    package_parts = module_name.split(".") if is_package else module_name.split(".")[:-1]
    keep_count = max(0, len(package_parts) - level + 1)
    imported_parts = imported_module.split(".") if imported_module else []
    return ".".join((*package_parts[:keep_count], *imported_parts))


def _candidate_local_module(
        imported_name: str,
        known_modules: set[str]) -> str | None:
    if not imported_name:
        return None
    if imported_name in known_modules:
        return imported_name
    # autocover_tool 同时是独立应用目录，运行时会把该目录加入 sys.path。
    suffix_matches = sorted(
        module
        for module in known_modules
        if module.endswith(f".{imported_name}")
    )
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return None


def _raw_imports(
        tree: ast.AST,
        module_name: str,
        is_package: bool) -> list[dict[str, object]]:
    imports: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "base": alias.name,
                    "member": None,
                    "kind": "import",
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_from_import(
                module_name,
                is_package,
                node.module,
                node.level,
            )
            for alias in node.names:
                imports.append({
                    "base": base,
                    "member": None if alias.name == "*" else alias.name,
                    "kind": "from",
                    "line": node.lineno,
                })
    return imports


def _resolve_import_target(
        raw_import: dict[str, object],
        known_modules: set[str]) -> tuple[str | None, str]:
    base = str(raw_import["base"])
    member = raw_import.get("member")
    imported_name = f"{base}.{member}" if base and member else base
    if member:
        member_target = _candidate_local_module(imported_name, known_modules)
        if member_target:
            return member_target, imported_name
    return _candidate_local_module(base, known_modules), imported_name


def analyse_module(path: Path, root: Path) -> dict[str, object]:
    source = _read_python_source(path)
    tree = ast.parse(source, filename=str(path))
    module_name = module_name_for_path(path, root)
    top_level_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    top_level_functions = [
        _definition_record(node)
        for node in top_level_nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    top_level_classes = [
        _definition_record(node)
        for node in top_level_nodes
        if isinstance(node, ast.ClassDef)
    ]

    definitions_by_name: dict[str, list[dict[str, object]]] = defaultdict(list)
    for node in top_level_nodes:
        definitions_by_name[node.name].append(_definition_record(node))
    duplicate_definitions = [
        {
            "name": name,
            "definitions": definitions,
        }
        for name, definitions in sorted(definitions_by_name.items())
        if len(definitions) > 1
    ]

    collector = _FunctionCollector()
    collector.visit(tree)
    longest_function = None
    if collector.functions:
        longest_function = sorted(
            collector.functions,
            key=lambda item: (
                -int(item["line_count"]),
                str(item["qualname"]),
                int(item["line"]),
            ),
        )[0]

    relative_path = _normalise_path(path, root)
    return {
        "module": module_name,
        "path": relative_path,
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "line_count": len(source.splitlines()),
        "top_level_functions": top_level_functions,
        "top_level_classes": top_level_classes,
        "duplicate_top_level_definitions": duplicate_definitions,
        "longest_function": longest_function,
        "_functions": collector.functions,
        "_raw_imports": _raw_imports(
            tree,
            module_name,
            path.name == "__init__.py",
        ),
    }


def _expression_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _constant_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _private_patch_targets(path: Path) -> list[str]:
    tree = ast.parse(_read_python_source(path), filename=str(path))
    targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _expression_name(node.func) or ""
        target = None
        if call_name == "patch" or call_name.endswith(".mock.patch"):
            if node.args:
                target = _constant_string(node.args[0])
        elif call_name.endswith("patch.object"):
            if len(node.args) >= 2:
                object_name = _expression_name(node.args[0]) or "<dynamic>"
                attribute_name = _constant_string(node.args[1])
                if attribute_name:
                    target = f"{object_name}.{attribute_name}"
        if target and target.rsplit(".", 1)[-1].startswith("_"):
            targets.append(target)
    return targets


def _test_private_patch_snapshot(
        test_files: list[Path], root: Path) -> dict[str, object]:
    by_test_file: dict[str, int] = {}
    target_counts: Counter[str] = Counter()
    for path in test_files:
        targets = _private_patch_targets(path)
        if targets:
            by_test_file[_normalise_path(path, root)] = len(targets)
            target_counts.update(targets)
    by_target_module: Counter[str] = Counter()
    for target, count in target_counts.items():
        module = target.rsplit(".", 1)[0] if "." in target else "<unknown>"
        by_target_module[module] += count
    return {
        "total": sum(target_counts.values()),
        "by_test_file": dict(sorted(by_test_file.items())),
        "by_target_module": dict(sorted(by_target_module.items())),
        "targets": [
            {"target": target, "count": count}
            for target, count in sorted(target_counts.items())
        ],
    }


def find_dependency_cycles(
        module_names: set[str],
        import_edges: list[dict[str, object]]) -> list[dict[str, object]]:
    """以强连通分量返回真实 import 环；结果不依赖文件遍历顺序。"""
    graph = {module: set() for module in module_names}
    for edge in import_edges:
        source = str(edge["from"])
        target = str(edge["to"])
        if source in graph and target in graph:
            graph[source].add(target)

    next_index = 0
    indexes: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(module: str) -> None:
        nonlocal next_index
        indexes[module] = next_index
        low_links[module] = next_index
        next_index += 1
        stack.append(module)
        on_stack.add(module)

        for dependency in sorted(graph[module]):
            if dependency not in indexes:
                visit(dependency)
                low_links[module] = min(low_links[module], low_links[dependency])
            elif dependency in on_stack:
                low_links[module] = min(low_links[module], indexes[dependency])

        if low_links[module] != indexes[module]:
            return
        component: list[str] = []
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.append(dependency)
            if dependency == module:
                break
        components.append(sorted(component))

    for module in sorted(module_names):
        if module not in indexes:
            visit(module)

    cycles: list[dict[str, object]] = []
    for component in components:
        component_set = set(component)
        if len(component) == 1 and component[0] not in graph[component[0]]:
            continue
        internal_edges = sorted(
            (
                {"from": str(edge["from"]), "to": str(edge["to"])}
                for edge in import_edges
                if str(edge["from"]) in component_set
                and str(edge["to"]) in component_set
            ),
            key=lambda edge: (edge["from"], edge["to"]),
        )
        cycles.append({
            "modules": component,
            "internal_edges": internal_edges,
            "debt_status": "present",
        })
    cycles.sort(key=lambda cycle: tuple(cycle["modules"]))
    return cycles


def dependency_contract_violations(
        current_snapshot: dict[str, object],
        debt_baseline: dict[str, object]) -> list[str]:
    """拒绝基线外的新环及 ``autoslice`` 底层模块的反向导入。"""
    violations: list[str] = []
    known_cycle_keys: set[tuple[str, ...]] = set()
    for cycle in debt_baseline.get("dependency_cycles", []):
        modules = tuple(str(module) for module in cycle.get("modules", []))
        if not modules or any("*" in module for module in modules):
            violations.append(f"循环依赖债务基线不是有限模块集合：{modules!r}")
            continue
        known_cycle_keys.add(modules)

    for cycle in current_snapshot.get("dependency_cycles", []):
        modules = tuple(str(module) for module in cycle.get("modules", []))
        if modules not in known_cycle_keys:
            violations.append(f"检测到基线外模块依赖环：{' -> '.join(modules)}")

    forbidden_targets = set(FORBIDDEN_LOW_LEVEL_TARGETS)
    for edge in current_snapshot.get("import_edges", []):
        source = str(edge["from"])
        target = str(edge["to"])
        is_low_level = (
            source == LOW_LEVEL_MODULE_PREFIX
            or source.startswith(f"{LOW_LEVEL_MODULE_PREFIX}.")
        )
        if is_low_level and target in forbidden_targets:
            violations.append(
                f"底层模块 {source} 禁止反向导入高层模块 {target}"
            )
    return violations


def build_snapshot(root: Path = ROOT) -> dict[str, object]:
    root = root.resolve()
    production_files, test_files = discover_python_files(root)
    modules = [analyse_module(path, root) for path in production_files]
    modules.sort(key=lambda item: (str(item["module"]), str(item["path"])))
    known_modules = {str(module["module"]) for module in modules}

    edge_details: dict[tuple[str, str], dict[str, object]] = {}
    longest_functions: list[dict[str, object]] = []
    duplicate_definitions: list[dict[str, object]] = []
    for module in modules:
        module_name = str(module["module"])
        for raw_import in module.pop("_raw_imports"):
            target, imported_name = _resolve_import_target(raw_import, known_modules)
            if not target or target == module_name:
                continue
            key = module_name, target
            detail = edge_details.setdefault(key, {
                "from": module_name,
                "to": target,
                "imports": set(),
                "lines": set(),
            })
            detail["imports"].add(imported_name)
            detail["lines"].add(int(raw_import["line"]))
        for function in module.pop("_functions"):
            longest_functions.append({
                "module": module_name,
                "path": module["path"],
                **function,
            })
        for duplicate in module["duplicate_top_level_definitions"]:
            duplicate_definitions.append({
                "module": module_name,
                "path": module["path"],
                **duplicate,
            })

    import_edges = [
        {
            "from": detail["from"],
            "to": detail["to"],
            "imports": sorted(detail["imports"]),
            "lines": sorted(detail["lines"]),
        }
        for _, detail in sorted(edge_details.items())
    ]
    longest_functions.sort(key=lambda item: (
        -int(item["line_count"]),
        str(item["module"]),
        str(item["qualname"]),
        int(item["line"]),
    ))
    duplicate_definitions.sort(key=lambda item: (
        str(item["module"]),
        str(item["name"]),
    ))
    private_patches = _test_private_patch_snapshot(test_files, root)
    dependency_cycles = find_dependency_cycles(known_modules, import_edges)

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "scripts/architecture_snapshot.py",
        "scope": {
            "root": ".",
            "production_files": [
                _normalise_path(path, root) for path in production_files
            ],
            "test_files": [
                _normalise_path(path, root) for path in test_files
            ],
            "excluded_directory_names": sorted(EXCLUDED_DIRECTORY_NAMES),
            "excluded_directory_prefixes": list(EXCLUDED_DIRECTORY_PREFIXES),
        },
        "summary": {
            "production_module_count": len(modules),
            "production_line_count": sum(int(module["line_count"]) for module in modules),
            "top_level_function_count": sum(
                len(module["top_level_functions"]) for module in modules
            ),
            "top_level_class_count": sum(
                len(module["top_level_classes"]) for module in modules
            ),
            "duplicate_top_level_definition_count": len(duplicate_definitions),
            "import_edge_count": len(import_edges),
            "test_private_patch_count": private_patches["total"],
        },
        "modules": modules,
        "import_edges": import_edges,
        "dependency_cycles": dependency_cycles,
        "dependency_policy": {
            "low_level_module_prefix": LOW_LEVEL_MODULE_PREFIX,
            "forbidden_reverse_import_targets": list(FORBIDDEN_LOW_LEVEL_TARGETS),
        },
        "duplicate_top_level_definitions": duplicate_definitions,
        "longest_functions": longest_functions[:LONGEST_FUNCTION_LIMIT],
        "test_private_patches": private_patches,
    }


def serialise_snapshot(snapshot: dict[str, object]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_snapshot(snapshot: dict[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialise_snapshot(snapshot), encoding="utf-8", newline="\n")


def _check_snapshot(snapshot: dict[str, object], output_path: Path) -> bool:
    if not output_path.is_file():
        print(f"架构快照不存在：{output_path}", file=sys.stderr)
        return False
    expected = output_path.read_text(encoding="utf-8")
    actual = serialise_snapshot(snapshot)
    if expected == actual:
        print(f"架构快照与代码一致：{output_path}")
        return True
    print(
        f"架构快照已过期：{output_path}；请审查变化后重新生成。",
        file=sys.stderr,
    )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="待扫描的仓库根目录")
    parser.add_argument("--output", type=Path, help="JSON 输出路径，默认位于扫描根目录")
    parser.add_argument("--check", action="store_true", help="只检查现有快照是否与代码一致")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    output = args.output or (root / DEFAULT_OUTPUT.name)
    snapshot = build_snapshot(root)
    if args.check:
        return 0 if _check_snapshot(snapshot, output) else 1
    write_snapshot(snapshot, output)
    print(f"已生成架构快照：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
