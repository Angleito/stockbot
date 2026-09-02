"""Logging configuration: default WARNING, DEBUG+ streaming to a log server,
and the INFO security-event line at the recorder choke point.

Offline: the chat-run test uses a scripted model and no network. Root
handlers are detached before each configure_logging call so handler
assertions see only what configure_logging attached (test collection
imports app.main, which calls configure_logging() at import time).
"""

import contextlib
import logging

import pytest

from app.config import configure_logging
from app.log_stream import LogStreamHandler
from app.storage.runs import RunRecorder


@contextlib.contextmanager
def _no_root_handlers():
    """Temporarily detach root handlers so basicConfig applies (pytest's
    own log-capture handler is attached after fixture setup, so the detach
    must happen inside the test body, immediately before configure_logging);
    restore everything afterwards."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def test_default_configuration_is_warning_without_stream_handler():
    with _no_root_handlers():
        configure_logging()
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert not any(isinstance(h, LogStreamHandler) for h in root.handlers)


def test_stream_url_configures_debug_and_stream_handler():
    with _no_root_handlers():
        configure_logging(stream_url="http://127.0.0.1:1/")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        stream_handlers = [h for h in root.handlers if isinstance(h, LogStreamHandler)]
        assert len(stream_handlers) == 1


def test_security_event_logged_at_info(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite"))
    recorder = RunRecorder(
        run_id="run-log-sec-1", request_id="req", question="q", as_of=None,
        model="test", provider="p", model_parameters={}, agent_version="0.1",
        prompt_version="2", tool_registry_version="x", git_sha="s",
        data_root=tmp_path,
    )
    with recorder:
        with caplog.at_level(logging.INFO):
            recorder.record_security_event(
                source="sec", sha256="h", score=0, verdict="ALLOW",
                rule_ids=["sec"], decision="allowed", reason="ok",
            )
    matches = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("security event:")
    ]
    assert len(matches) == 1
    assert "decision=allowed" in matches[0]
    assert "source=sec" in matches[0]


def test_chat_run_logs_tool_call(monkeypatch, caplog):
    from app import agent
    from app.policy import LOCAL_CONTEXT
    from tests.test_observability import TEST_POLICY, FakeOpenRouter, _final, _tool_round

    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL", "metric": "eps"}),
        _final("AAPL EPS is 6.3."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, args, model, **kwargs: {"ticker": "AAPL", "metric": "eps"},
    )
    with caplog.at_level(logging.INFO):
        agent.run_chat(
            [{"role": "user", "content": "AAPL EPS?"}],
            model="test",
            context=LOCAL_CONTEXT,
            policy=TEST_POLICY,
        )
    assert any("Tool call:" in r.getMessage() for r in caplog.records)
