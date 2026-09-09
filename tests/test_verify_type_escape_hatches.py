"""Offline unit tests for scripts/verify_type_escape_hatches.py (tmp_path only)."""

from pathlib import Path

import pytest

import scripts.verify_type_escape_hatches as v

# NOTE: payloads are concatenated so this file stays clean for the in-tree scanner.


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _write_baseline(baseline: Path, current: dict[str, int]) -> None:
    lines = sorted(sig for sig, n in current.items() for _ in range(n))
    baseline.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def test_existing_baseline_entry_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "app/foo.py", "x = " + "cast" + "(int, y)\n")
    current = v.scan(tmp_path)
    assert sum(current.values()) == 1
    baseline = tmp_path / "baseline.txt"
    _write_baseline(baseline, current)
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main(["--baseline", str(baseline)]) == 0


def test_new_cast_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "app/foo.py", "x = 1\n")
    assert sum(v.scan(tmp_path).values()) == 0
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("", encoding="utf-8")
    p = tmp_path / "app" / "foo.py"
    p.write_text(p.read_text(encoding="utf-8") + "y = " + "cast" + "(int, z)\n", encoding="utf-8")
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main(["--baseline", str(baseline)]) == 1


def test_duplicate_identical_cast_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    line = "rows = " + "cast" + "(list[object], data)\n"
    _write(tmp_path, "app/foo.py", line)
    first = v.scan(tmp_path)
    assert sum(first.values()) == 1
    sig = next(iter(first))
    baseline = tmp_path / "baseline.txt"
    _write_baseline(baseline, first)
    p = tmp_path / "app" / "foo.py"
    p.write_text(p.read_text(encoding="utf-8") + line, encoding="utf-8")
    second = v.scan(tmp_path)
    assert second[sig] == 2
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main(["--baseline", str(baseline)]) == 1


def test_removed_baseline_entry_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = "a = " + "cast" + "(int, b)\n"
    second = "c = " + "cast" + "(str, d)\n"
    _write(tmp_path, "app/foo.py", first + second)
    current = v.scan(tmp_path)
    assert sum(current.values()) == 2
    baseline = tmp_path / "baseline.txt"
    _write_baseline(baseline, current)
    _write(tmp_path, "app/foo.py", first)
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main(["--baseline", str(baseline)]) == 0


def test_new_type_ignore_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "app/foo.py", "x = 1\n")
    assert sum(v.scan(tmp_path).values()) == 0
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("", encoding="utf-8")
    p = tmp_path / "app" / "foo.py"
    p.write_text(p.read_text(encoding="utf-8") + "x = None  " + "# " + "type:" + " ignore\n", encoding="utf-8")
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main(["--baseline", str(baseline)]) == 1
