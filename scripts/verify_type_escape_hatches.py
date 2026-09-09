#!/usr/bin/env python3
"""Zero-tolerance tripwire for type-escape hatches."""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("app", "scripts", "tests")
SCAN_FILES = ("cli.py", "conftest.py")
SKIP_PARTS = frozenset({"venv", "data", "__pycache__", ".pytest_cache"})

_PATTERNS = [
    re.compile(r"^\s*from\s+typing\s+import\s+.*\bAny\b"),
    re.compile(r"\btyping\.Any\b"),
    re.compile(r"\bcast\s*\("),
    re.compile(r"#\s*type:\s*ignore"),
    re.compile(r"#\s*pyrefly\s*:\s*ignore"),
    re.compile(r"#\s*pyrefly-ignore"),
]


def _iter_files(root: Path):
    for name in SCAN_DIRS:
        d = root / name
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")) + sorted(d.rglob("*.pyi")):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            yield p
    for name in SCAN_FILES:
        p = root / name
        if p.is_file():
            yield p


def scan(root: Path) -> list[str]:
    found: list[str] = []
    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        flagged: set[int] = set()
        def flag(lineno: int) -> None:
            if lineno in flagged:
                return
            if lineno < 1 or lineno > len(lines):
                return
            s = lines[lineno - 1].strip()
            if not s:
                return
            flagged.add(lineno)
            found.append(f"{rel}: {s}")
        for idx, line in enumerate(lines, start=1):
            s = line.strip()
            if not s:
                continue
            for pat in _PATTERNS:
                if pat.search(line):
                    flag(idx)
                    break
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        cast_names: set[str] = set()
        any_names: set[str] = set()
        ntc_names: set[str] = set()
        mod_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "typing" or node.module == "typing_extensions":
                    for alias in node.names:
                        bound = alias.asname if alias.asname is not None else alias.name
                        if alias.name == "*":
                            cast_names.add("cast")
                            any_names.add("Any")
                            ntc_names.add("no_type_check")
                            flag(node.lineno)
                        elif alias.name == "cast":
                            cast_names.add(bound)
                            flag(node.lineno)
                        elif alias.name == "Any":
                            any_names.add(bound)
                            flag(node.lineno)
                        elif alias.name == "no_type_check":
                            ntc_names.add(bound)
                            flag(node.lineno)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "typing" or alias.name == "typing_extensions":
                        bound = alias.asname if alias.asname is not None else alias.name
                        mod_aliases.add(bound)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    if func.id in cast_names:
                        flag(node.lineno)
                elif isinstance(func, ast.Attribute):
                    if func.attr == "cast":
                        v = func.value
                        if isinstance(v, ast.Name):
                            if v.id in mod_aliases:
                                flag(node.lineno)
            elif isinstance(node, ast.Name):
                if node.id in any_names:
                    flag(node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Name):
                        if dec.id in ntc_names:
                            flag(dec.lineno)
                    elif isinstance(dec, ast.Attribute):
                        if dec.attr == "no_type_check":
                            v = dec.value
                            if isinstance(v, ast.Name):
                                if v.id in mod_aliases:
                                    flag(dec.lineno)
            elif isinstance(node, ast.Attribute):
                if node.attr == "Any":
                    v = node.value
                    if isinstance(v, ast.Name):
                        if v.id in mod_aliases:
                            flag(node.lineno)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            if sub.id == "__no_type_check__":
                                flag(node.lineno)
                        elif isinstance(sub, ast.Attribute):
                            if sub.attr == "__no_type_check__":
                                flag(node.lineno)
            elif isinstance(node, ast.AnnAssign):
                for sub in ast.walk(node.target):
                    if isinstance(sub, ast.Name):
                        if sub.id == "__no_type_check__":
                            flag(node.lineno)
                    elif isinstance(sub, ast.Attribute):
                        if sub.attr == "__no_type_check__":
                            flag(node.lineno)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    hits = scan(ROOT)
    for e in hits:
        print(f"FORBIDDEN {e}")
    print(f"type escapes: {len(hits)}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
