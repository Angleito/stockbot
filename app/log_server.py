"""Local log server: prints log lines POSTed by chat/API clients."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_LOG_SERVER_PORT = 8765

_print_lock = threading.Lock()


class LogServerHandler(BaseHTTPRequestHandler):
    """Prints each received log line verbatim (display only, no persistence)."""

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length") or 0)
        line = self.rfile.read(length).decode("utf-8", "replace").rstrip("\n")
        with _print_lock:
            print(line, flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass  # suppress default request-line noise


def run_log_server(port: int = DEFAULT_LOG_SERVER_PORT) -> None:
    """Serve until Ctrl-C; binds 127.0.0.1 only (local tool, no auth)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), LogServerHandler)
    print(f"Log server listening on http://127.0.0.1:{port} (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Log server stopped", flush=True)
    finally:
        server.server_close()
