"""Sentinel-file helpers for the Pi gateway (no `pi` binary needed)."""

import json

from app.thesis.pi_json import _db_terminal, _read_done


def test_read_done_accepts_valid_sentinel(tmp_path):
    p = tmp_path / "done.json"
    assert _read_done(p) is None  # missing: keep polling
    p.write_text('{"status":')
    assert _read_done(p) is None  # partial write: keep polling
    p.write_text('{"status": "completed"}')
    assert _read_done(p) is None  # no answer yet
    p.write_text(json.dumps({"status": "completed", "answer": '{"a": 1}'}))
    assert _read_done(p) == '{"a": 1}'
    p.write_text(json.dumps({"status": "completed", "answer": {"a": 1}}))
    assert json.loads(_read_done(p)) == {"a": 1}


def test_db_terminal(tmp_path):
    import sqlite3

    db = tmp_path / "run.db"
    assert _db_terminal(db) is False
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE agent_runs (status TEXT)")
    conn.execute("INSERT INTO agent_runs VALUES ('running')")
    conn.commit()
    conn.close()
    assert _db_terminal(db) is False
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE agent_runs SET status='completed'")
    conn.commit()
    conn.close()
    assert _db_terminal(db) is True
