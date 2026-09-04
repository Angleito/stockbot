"""Log stream handler and log-server CLI: HTTP POST of formatted records to
a local log server, outage tolerance, and parser wiring."""

import logging
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import cli
from app.config import LOG_FORMAT
from app.log_server import DEFAULT_LOG_SERVER_PORT, LogServerHandler
from app.log_stream import LogStreamHandler


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("app.test", logging.INFO, __file__, 1, message, (), None)


def test_streams_record_to_running_server(capsys):
    server = ThreadingHTTPServer(("127.0.0.1", 0), LogServerHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        handler = LogStreamHandler(f"http://127.0.0.1:{server.server_address[1]}/")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        record = _record("hello log stream")
        expected = logging.Formatter(LOG_FORMAT).format(record)
        handler.emit(record)
        captured = ""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and expected not in captured:
            captured += capsys.readouterr().out
            time.sleep(0.02)
        assert expected in captured
    finally:
        server.shutdown()
        server.server_close()


def test_emit_with_server_down_does_not_raise():
    handler = LogStreamHandler("http://127.0.0.1:1/")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.emit(_record("hello log stream"))  # must not raise


def test_cmd_log_server_bind_error_exits(monkeypatch, capsys):
    def boom(port):
        raise OSError("in use")

    monkeypatch.setattr(cli, "run_log_server", boom)
    with pytest.raises(SystemExit) as exc_info:
        cli._cmd_log_server(9999)
    assert exc_info.value.code == 1
    assert "cannot bind" in capsys.readouterr().err


def test_parser_log_server_flag_positions():
    parser = cli._build_parser()
    assert parser.parse_args(["--log-server"]).log_server == "http://127.0.0.1:8765"
    assert (
        parser.parse_args(["--log-server", "http://x:1", "runs"]).log_server == "http://x:1"
    )
    assert parser.parse_args(["runs"]).log_server is None
    assert parser.parse_args(["log-server", "--port", "9999"]).port == 9999
    assert parser.parse_args(["log-server"]).port == DEFAULT_LOG_SERVER_PORT


def test_bare_log_server_before_subcommand_keeps_subcommand():
    # nargs="?" would consume "runs" as the URL value; the rewrite must
    # recognize the subcommand and fall back to the default URL.
    rewritten = cli._rewrite_bare_log_server(["--log-server", "runs"])
    assert rewritten == ["--log-server=http://127.0.0.1:8765", "runs"]
    args = cli._build_parser().parse_args(rewritten)
    assert args.command == "runs"
    assert args.log_server == "http://127.0.0.1:8765"
    # explicit URL, bare flag after the subcommand, and trailing bare flag
    # are all left untouched.
    assert cli._rewrite_bare_log_server(
        ["--log-server", "http://x:1", "runs"]
    ) == ["--log-server", "http://x:1", "runs"]
    assert cli._rewrite_bare_log_server(["runs", "--log-server"]) == ["runs", "--log-server"]
    assert cli._rewrite_bare_log_server(["--log-server"]) == ["--log-server"]
