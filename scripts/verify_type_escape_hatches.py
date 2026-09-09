#!/usr/bin/env python3
"""Baseline-compared tripwire for type-escape hatches."""
import argparse
import re
import sys
from collections import Counter
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


def scan(root: Path) -> Counter[str]:
    found: Counter[str] = Counter()
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
                    found[f"{rel}: {s}"] += 1
                    break
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = ap.parse_args(argv)
    current = scan(ROOT)
    if args.update_baseline:
        lines = sorted(sig for sig, n in current.items() for _ in range(n))
        args.baseline.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        print(f"wrote {len(lines)} entries to {args.baseline}")
        return 0
    if not args.baseline.is_file():
        print(
            f"baseline missing, run with --update-baseline: {args.baseline}",
            file=sys.stderr,
        )
        return 1
    known: Counter[str] = Counter(
        line.strip()
        for line in args.baseline.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    new = sorted(sig for sig, n in current.items() if n > known.get(sig, 0))
    for e in new:
        n = current[e]
        print(f"NEW {e} x{n} (baseline x{known.get(e, 0)})")
    if new:
        print(
            "remove it, or ask the user to approve --update-baseline",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
