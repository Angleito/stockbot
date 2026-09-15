#!/usr/bin/env python3
"""Zero-tolerance tripwire for type-escape hatches."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("app", "scripts", "tests")
SCAN_FILES = ("cli.py", "conftest.py")
SKIP_PARTS = frozenset({"venv", "data", "__pycache__", ".pytest_cache"})
_TYPING_MODULES = ("typing", "typing_extensions")

_PATTERNS = [
    re.compile(r"^\s*from\s+typing\s+import\s+.*\bAny\b"),
    re.compile(r"\btyping\.Any\b"),
    re.compile(r"\bcast\s*\("),
    re.compile(r"#\s*type:\s*ignore"),
    re.compile(r"#\s*pyrefly\s*:\s*ignore"),
    re.compile(r"#\s*pyrefly-ignore"),
]


def _iter_scan_dir(root: Path, name: str):
    d = root / name
    if not d.is_dir():
        return
    for p in sorted(d.rglob("*.py")) + sorted(d.rglob("*.pyi")):
        if any(part in SKIP_PARTS for part in p.parts):
            continue
        yield p

def _iter_scan_file(root: Path, name: str):
    p = root / name
    if p.is_file():
        yield p

def _iter_files(root: Path):
    for name in SCAN_DIRS:
        yield from _iter_scan_dir(root, name)
    for name in SCAN_FILES:
        yield from _iter_scan_file(root, name)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


class _FileHits:
    """Per-file findings with per-line dedup (repeats on one line count once)."""

    def __init__(self, rel: str, lines: list[str]) -> None:
        self._rel = rel
        self._lines = lines
        self._flagged: set[int] = set()
        self.hits: list[str] = []

    def flag(self, lineno: int) -> None:
        if lineno in self._flagged:
            return
        if lineno < 1 or lineno > len(self._lines):
            return
        s = self._lines[lineno - 1].strip()
        if not s:
            return
        self._flagged.add(lineno)
        self.hits.append(f"{self._rel}: {s}")


def _scan_patterns(collector: _FileHits, lines: list[str]) -> None:
    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        for pat in _PATTERNS:
            if pat.search(line):
                collector.flag(idx)
                break


class _TypeBindings:
    """Per-file names bound to typing escapes by import statements."""

    def __init__(self) -> None:
        self.cast_names: set[str] = set()
        self.any_names: set[str] = set()
        self.ntc_names: set[str] = set()
        self.mod_aliases: set[str] = set()


def _bind_star_alias(bindings: _TypeBindings) -> None:
    bindings.cast_names.add("cast")
    bindings.any_names.add("Any")
    bindings.ntc_names.add("no_type_check")

def _bind_named_alias(alias: ast.alias, bound: str, bindings: _TypeBindings) -> bool:
    if alias.name == "cast":
        bindings.cast_names.add(bound)
    elif alias.name == "Any":
        bindings.any_names.add(bound)
    elif alias.name == "no_type_check":
        bindings.ntc_names.add(bound)
    else:
        return False
    return True

def _bind_from_alias(
    alias: ast.alias, bindings: _TypeBindings, collector: _FileHits, lineno: int
) -> None:
    bound = alias.asname if alias.asname is not None else alias.name
    if alias.name == "*":
        _bind_star_alias(bindings)
    elif not _bind_named_alias(alias, bound, bindings):
        return
    collector.flag(lineno)


def _bind_plain_import(node: ast.Import, bindings: _TypeBindings) -> None:
    for alias in node.names:
        if alias.name in _TYPING_MODULES:
            bound = alias.asname if alias.asname is not None else alias.name
            bindings.mod_aliases.add(bound)


def _bind_from_import(node: ast.ImportFrom, bindings: _TypeBindings, collector: _FileHits) -> None:
    if node.module not in _TYPING_MODULES:
        return
    for alias in node.names:
        _bind_from_alias(alias, bindings, collector, node.lineno)

def _bind_imports(tree: ast.AST, bindings: _TypeBindings, collector: _FileHits) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            _bind_from_import(node, bindings, collector)
        elif isinstance(node, ast.Import):
            _bind_plain_import(node, bindings)


def _flag_namespaced_cast(func: ast.Attribute, bindings: _TypeBindings, collector: _FileHits, lineno: int) -> None:
    if func.attr != "cast":
        return
    target = func.value
    if isinstance(target, ast.Name) and target.id in bindings.mod_aliases:
        collector.flag(lineno)

def _flag_call(node: ast.Call, bindings: _TypeBindings, collector: _FileHits) -> None:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in bindings.cast_names:
            collector.flag(node.lineno)
    elif isinstance(func, ast.Attribute):
        _flag_namespaced_cast(func, bindings, collector, node.lineno)


def _flag_name(node: ast.Name, bindings: _TypeBindings, collector: _FileHits) -> None:
    if node.id in bindings.any_names:
        collector.flag(node.lineno)


def _flag_namespaced_decorator(
    dec: ast.Attribute, bindings: _TypeBindings, collector: _FileHits
) -> None:
    if dec.attr != "no_type_check":
        return
    target = dec.value
    if isinstance(target, ast.Name) and target.id in bindings.mod_aliases:
        collector.flag(dec.lineno)


def _flag_decorator(dec: ast.expr, bindings: _TypeBindings, collector: _FileHits) -> None:
    if isinstance(dec, ast.Name):
        if dec.id in bindings.ntc_names:
            collector.flag(dec.lineno)
    elif isinstance(dec, ast.Attribute):
        _flag_namespaced_decorator(dec, bindings, collector)


def _flag_decorated(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    bindings: _TypeBindings,
    collector: _FileHits,
) -> None:
    for dec in node.decorator_list:
        _flag_decorator(dec, bindings, collector)


def _flag_any_attr(node: ast.Attribute, bindings: _TypeBindings, collector: _FileHits) -> None:
    if node.attr != "Any":
        return
    target = node.value
    if isinstance(target, ast.Name) and target.id in bindings.mod_aliases:
        collector.flag(node.lineno)


def _flag_assign_sub(sub: ast.AST, collector: _FileHits, lineno: int) -> None:
    if isinstance(sub, ast.Name):
        if sub.id == "__no_type_check__":
            collector.flag(lineno)
    elif isinstance(sub, ast.Attribute):
        if sub.attr == "__no_type_check__":
            collector.flag(lineno)

def _flag_assign_target(target: ast.expr, collector: _FileHits, lineno: int) -> None:
    for sub in ast.walk(target):
        _flag_assign_sub(sub, collector, lineno)


def _flag_assignment(node: ast.Assign | ast.AnnAssign, collector: _FileHits) -> None:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        _flag_assign_target(target, collector, node.lineno)


def _flag_one_usage(node: ast.AST, bindings: _TypeBindings, collector: _FileHits) -> None:
    if isinstance(node, ast.Call):
        _flag_call(node, bindings, collector)
    elif isinstance(node, ast.Name):
        _flag_name(node, bindings, collector)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        _flag_decorated(node, bindings, collector)
    elif isinstance(node, ast.Attribute):
        _flag_any_attr(node, bindings, collector)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        _flag_assignment(node, collector)

def _flag_usages(tree: ast.AST, bindings: _TypeBindings, collector: _FileHits) -> None:
    for node in ast.walk(tree):
        _flag_one_usage(node, bindings, collector)


def _scan_text(rel: str, text: str, found: list[str]) -> None:
    lines = text.splitlines()
    collector = _FileHits(rel, lines)
    _scan_patterns(collector, lines)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        found.extend(collector.hits)
        return
    bindings = _TypeBindings()
    _bind_imports(tree, bindings, collector)
    _flag_usages(tree, bindings, collector)
    found.extend(collector.hits)


def _scan_one(root: Path, path: Path, found: list[str]) -> None:
    text = _read_text(path)
    if text is None:
        return
    _scan_text(path.relative_to(root).as_posix(), text, found)

def scan(root: Path) -> list[str]:
    found: list[str] = []
    for path in _iter_files(root):
        _scan_one(root, path, found)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    hits = scan(ROOT)
    for e in hits:
        print(f"FORBIDDEN {e}")
    print(f"type escapes: {len(hits)}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
