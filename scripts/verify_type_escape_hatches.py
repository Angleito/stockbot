#!/usr/bin/env python3
"""Baseline-compared tripwire for type-escape hatches."""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = Path(__file__).resolve().with_name(
    "verify_type_escape_hatches.baseline.txt"
)
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
    found: set[str] = set()
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
                    found.add(f"{rel}: {s}")
                    break
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = ap.parse_args(argv)
    current = scan(ROOT)
    if args.update_baseline:
        args.baseline.write_text(
            "\n".join(current) + ("\n" if current else ""), encoding="utf-8"
        )
        print(f"wrote {len(current)} entries to {args.baseline}")
        return 0
    if not args.baseline.is_file():
        print(
            f"baseline missing, run with --update-baseline: {args.baseline}",
            file=sys.stderr,
        )
        return 1
    known = {
        line.strip()
        for line in args.baseline.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    new = [e for e in current if e not in known]
    for e in new:
        print(f"NEW {e}")
    if new:
        print(
            "remove it, or ask the user to approve --update-baseline",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
