"""OAuth persistence and MCP OAuth provider construction.

The MCP SDK is intentionally imported only when an OAuth provider is built;
offline users of stockbot do not need to install it.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import queue
import tempfile
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_TOKEN_PATH = Path.home() / ".stockbot" / "robinhood" / "oauth.json"


class OAuthStoreError(RuntimeError):
    """Raised when the persisted OAuth state cannot be read or written."""


@dataclass
class OAuthConfig:
    server_url: str
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    scopes: tuple[str, ...] = ()


def parse_callback_url(callback_url: str) -> Any:
    """Extract OAuth redirect parameters while preserving state and issuer."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    if query.get("error"):
        description = query.get("error_description", [""])[0]
        suffix = f": {description}" if description else ""
        raise OAuthStoreError(f"OAuth authorization was declined{suffix}")
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    if not code or not state:
        raise OAuthStoreError("OAuth callback did not contain code and state")
    return code, state, query.get("iss", [None])[0]


class LoopbackCallback:
    """Serve one OAuth callback on localhost instead of requiring pasted URLs."""

    def __init__(self, redirect_uri: str):
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise OAuthStoreError("OAuth redirect URI must be a localhost HTTP URL")
        self.path = parsed.path or "/callback"
        self.host = parsed.hostname or "127.0.0.1"
        self.requested_port = parsed.port or 0
        self._received: queue.Queue[str] = queue.Queue(maxsize=1)
        callback = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler API
                request = urllib.parse.urlparse(self.path)
                if request.path != callback.path:
                    self.send_error(404)
                    return
                callback._received.put(request.geturl())
                body = b"Stockbot Robinhood authorization received. You can close this tab."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 - stdlib handler API
                return

        self._handler_type = Handler
        self.server = None
        self.redirect_uri = (
            f"http://{self.host}:{self.requested_port}{self.path}"
            if self.requested_port
            else ""
        )
        if not self.requested_port:
            self._bind()

    def _bind(self) -> None:
        if self.server is not None:
            return
        try:
            self.server = http.server.ThreadingHTTPServer(
                (self.host, self.requested_port), self._handler_type
            )
        except OSError as exc:
            raise OAuthStoreError(
                f"Cannot bind OAuth callback listener at "
                f"http://{self.host}:{self.requested_port}{self.path}; "
                "close the process using that port or set another redirect URI"
            ) from exc
        self.server.daemon_threads = True
        actual_port = self.server.server_address[1]
        self.redirect_uri = f"http://{self.host}:{actual_port}{self.path}"
        self._thread = threading.Thread(
            target=self.server.serve_forever,
            name="stockbot-robinhood-oauth",
            daemon=True,
        )
        self._started = False
        self._closed = False

    def start(self) -> None:
        self._bind()
        if not self._started:
            self._started = True
            self._thread.start()

    def close(self) -> None:
        if self.server is not None and not self._closed:
            self._closed = True
            self.server.shutdown()
            self.server.server_close()
            self._thread.join(timeout=2)

    async def redirect_handler(self, url: str) -> None:
        print(f"Opening Robinhood authorization in your browser:\n{url}")
        if not webbrowser.open(url):
            print("If it did not open, paste that URL into a browser.")

    async def callback_handler(self) -> Any:
        callback_url = await asyncio.to_thread(self._received.get)
        try:
            return parse_callback_url(callback_url)
        finally:
            self.close()


def load_tokens(path: Path = DEFAULT_TOKEN_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OAuthStoreError(f"Cannot read OAuth state at {path}") from exc
    return value if isinstance(value, dict) else None


def save_tokens(tokens: dict[str, Any], path: Path = DEFAULT_TOKEN_PATH) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix="oauth.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(tokens, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise OAuthStoreError(f"Cannot write OAuth state at {path}") from exc


def build_oauth_provider(config: OAuthConfig, path: Path = DEFAULT_TOKEN_PATH) -> Any:
    """Build the SDK v2 OAuthClientProvider with callbacks for token storage."""
    try:
        from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
        from mcp.shared.auth import (
            OAuthClientInformationFull,
            OAuthClientMetadata,
            OAuthToken,
        )
        from pydantic import AnyUrl
    except ImportError as exc:
        raise RuntimeError("Robinhood MCP support requires the optional 'mcp' package") from exc

    class Storage:
        async def get_tokens(self):
            state = load_tokens(path) or {}
            tokens = state.get("tokens")
            return OAuthToken.model_validate(tokens) if tokens else None

        async def set_tokens(self, tokens):
            state = load_tokens(path) or {}
            state["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            save_tokens(state, path)

        async def get_client_info(self):
            state = load_tokens(path) or {}
            info = state.get("client_info")
            return OAuthClientInformationFull.model_validate(info) if info else None

        async def set_client_info(self, client_info):
            state = load_tokens(path) or {}
            state["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
            save_tokens(state, path)

    callback = LoopbackCallback(config.redirect_uri)

    async def redirect_handler(url: str) -> None:
        callback.start()
        await callback.redirect_handler(url)

    async def callback_handler() -> Any:
        code, state, iss = await callback.callback_handler()
        return AuthorizationCodeResult(code=code, state=state, iss=iss)

    metadata = OAuthClientMetadata(
        client_name="stockbot",
        redirect_uris=[AnyUrl(callback.redirect_uri)],
        scope=" ".join(config.scopes) or None,
    )
    return OAuthClientProvider(
        server_url=config.server_url,
        client_metadata=metadata,
        storage=Storage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
