#!/usr/bin/env python3
"""Zero-tolerance tripwire for type-escape hatches."""
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
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            for pat in _PATTERNS:
                if pat.search(line):
                    found.append(f"{rel}: {s}")
                    break
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    hits = scan(ROOT)
    for e in hits:
        print(f"FORBIDDEN {e}")
    print(f"type escapes: {len(hits)}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
