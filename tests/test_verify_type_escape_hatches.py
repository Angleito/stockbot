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


def test_aliased_cast_evasion_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "app/foo.py", "from typing import " + "cast" + " as narrow\nvalue = narrow(str, x)\n")
    hits = v.scan(tmp_path)
    assert len(hits) == 2
    assert hits[0] != hits[1]
    assert all("app/foo.py" in h for h in hits)
    monkeypatch.setattr(v, "ROOT", tmp_path)
    assert v.main([]) == 1


def test_typing_extensions_cast_fails(tmp_path: Path) -> None:
    _write(tmp_path, "app/foo.py", "from typing_extensions import " + "cast" + "\nv = " + "cast" + "(str, x)\n")
    hits = v.scan(tmp_path)
    assert len(hits) == 2


def test_star_import_fails(tmp_path: Path) -> None:
    _write(tmp_path, "app/foo.py", "from typing import *\n")
    hits = v.scan(tmp_path)
    assert len(hits) == 1
    assert "app/foo.py" in hits[0]
    assert "from typing import *" in hits[0]

def test_module_alias_cast_fails(tmp_path: Path) -> None:
    _write(tmp_path, "app/foo.py", "import typing as t\nv = t." + "cast" + "(str, x)\n")
    hits = v.scan(tmp_path)
    assert len(hits) >= 1
    assert any("app/foo.py" in h for h in hits)


def test_no_type_check_decorator_and_dunder_fail(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/foo.py",
        "from typing import no_type_check\n@no_type_check\ndef f() -> int:\n    return 1\n",
    )
    hits = v.scan(tmp_path)
    assert len(hits) == 2
    _write(tmp_path, "app/bar.py", "__no_type_check__ = True\n")
    hits = v.scan(tmp_path)
    assert len(hits) == 3


def test_multiline_any_import_fails(tmp_path: Path) -> None:
    _write(tmp_path, "app/foo.py", "from typing import (\n    Any,\n)\n")
    hits = v.scan(tmp_path)
    assert len(hits) == 1


def test_syntax_error_fallback(tmp_path: Path) -> None:
    _write(tmp_path, "app/foo.py", "def broken(:\n")
    assert v.scan(tmp_path) == []
    p = tmp_path / "app" / "foo.py"
    p.write_text(p.read_text(encoding="utf-8") + "x = None  " + "# " + "type:" + " ignore\n", encoding="utf-8")
    assert len(v.scan(tmp_path)) == 1


def test_scope_parity_with_pyrefly() -> None:
    import tomllib

    pyrefly_toml = v.ROOT / "pyrefly.toml"
    includes = set(tomllib.loads(pyrefly_toml.read_text(encoding="utf-8"))["project-includes"])
    assert set(v.SCAN_FILES) <= includes
    assert set(v.SCAN_DIRS) <= includes
