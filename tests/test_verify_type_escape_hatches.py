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


def test_clean_tree_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "app/foo.py", "x = 1\n")
    assert v.scan(tmp_path) == []
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main([]) == 0
    assert "type escapes: 0" in capsys.readouterr().out


def test_cast_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "app/foo.py", "x = " + "cast" + "(int, y)\n")
    hits = v.scan(tmp_path)
    assert len(hits) == 1
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main([]) == 1
    out = capsys.readouterr().out
    assert "FORBIDDEN" in out
    assert "type escapes: 1" in out


def test_duplicate_identical_cast_counts_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    line = "rows = " + "cast" + "(list[object], data)\n"
    _write(tmp_path, "app/foo.py", line)
    p = tmp_path / "app" / "foo.py"
    p.write_text(p.read_text(encoding="utf-8") + line, encoding="utf-8")
    hits = v.scan(tmp_path)
    assert len(hits) == 2
    assert hits[0] == hits[1]
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main([]) == 1


def test_removed_hit_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = "a = " + "cast" + "(int, b)\n"
    second = "c = " + "cast" + "(str, d)\n"
    _write(tmp_path, "app/foo.py", first + second)
    assert len(v.scan(tmp_path)) == 2
    _write(tmp_path, "app/foo.py", "x = 1\n")
    assert v.scan(tmp_path) == []
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main([]) == 0


def test_type_ignore_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "app/foo.py", "x = 1\n")
    assert v.scan(tmp_path) == []
    p = tmp_path / "app" / "foo.py"
    p.write_text(p.read_text(encoding="utf-8") + "x = None  " + "# " + "type:" + " ignore\n", encoding="utf-8")
    assert len(v.scan(tmp_path)) == 1
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main([]) == 1
