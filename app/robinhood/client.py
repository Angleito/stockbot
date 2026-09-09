"""Synchronous, read-only facade over Robinhood's MCP server."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Callable, TypeVar

from app.thesis.models import JSONValue

from .auth import DEFAULT_TOKEN_PATH, OAuthConfig, build_oauth_provider
from .capabilities import (
    ACCOUNT_READ_TOOLS,
    MARKET_READ_TOOLS,
    allowed_read_tools,
    is_blocked,
)

_T = TypeVar("_T")


class RobinhoodDependencyError(RuntimeError):
    pass


class RobinhoodAuthRequired(RuntimeError):
    pass


class RobinhoodToolError(RuntimeError):
    pass


def normalize_result(result: object) -> JSONValue:
    """Convert SDK model objects and MCP content into JSON-like values."""
    if result is None or isinstance(result, (str, int, float, bool)):
        return result
    if isinstance(result, dict):
        return {str(k): normalize_result(v) for k, v in result.items()}
    if isinstance(result, (list, tuple)):
        return [normalize_result(v) for v in result]
    if hasattr(result, "model_dump"):
        dumped: object = getattr(result, "model_dump")(exclude_none=True)
        return normalize_result(dumped)
    if hasattr(result, "dict"):
        dumped_dict: object = getattr(result, "dict")(exclude_none=True)
        return normalize_result(dumped_dict)
    if hasattr(result, "text"):
        text: object = getattr(result, "text")
        if text is None or isinstance(text, (str, int, float, bool)):
            return text
        return normalize_result(text)
    return {k: normalize_result(v) for k, v in vars(result).items() if not k.startswith("_")}


def normalize_tools(result: object) -> list[dict[str, object]]:
    value: object = normalize_result(getattr(result, "tools", result))
    candidates: object = value.get("tools", []) if isinstance(value, dict) else value
    if not isinstance(candidates, list):
        return []
    rows: list[dict[str, object]] = []
    for item in candidates:
        if isinstance(item, dict):
            rows.append(item)
        else:
            rows.append({"name": str(item)})
    return rows


class RobinhoodClient:
    def __init__(self, server_url: str, *, oauth: OAuthConfig | None = None,
                 token_path: Path = DEFAULT_TOKEN_PATH,
                 market_tools: frozenset[str] | None = None,
                 account_tools: frozenset[str] | None = None,
                 allowed_tools: set[str] | None = None,
                 transport_factory: Callable[..., object] | None = None):
        if allowed_tools is not None:
            # Legacy generic configuration still cannot add capabilities:
            # classify it against the canonical registry before construction.
            configured = frozenset(allowed_tools)
            unknown = configured - MARKET_READ_TOOLS - ACCOUNT_READ_TOOLS
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"Unknown read-only tool configuration: {names}")
            market_tools = configured & MARKET_READ_TOOLS
            account_tools = configured & ACCOUNT_READ_TOOLS
        if oauth is not None and server_url != oauth.server_url:
            raise ValueError(
                "Robinhood MCP transport URL must match the OAuth server URL"
            )
        self.server_url = server_url
        self.oauth = oauth
        self.token_path = token_path
        self.market_tools = market_tools
        self.account_tools = account_tools
        self.allowed_tools = allowed_tools
        self.permitted_tools = allowed_read_tools(
            market=market_tools, account=account_tools
        )
        self.transport_factory = transport_factory

    def list_tools(self) -> list[dict[str, object]]:
        return self._run(self._list_tools())

    def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> JSONValue:
        self._check_tool(name)
        return self._run(self._call_tool(name, arguments or {}))

    def run_readonly(self, calls: list[tuple[str, dict[str, object]]]) -> list[JSONValue]:
        """Run read-only calls in one authenticated MCP session."""
        for name, _ in calls:
            self._check_tool(name)
        return self._run(self._run_readonly(calls))

    def _check_tool(self, name: str) -> None:
        if not isinstance(name, str) or not name or is_blocked(name):
            raise RobinhoodToolError(f"Tool is not permitted (read-only policy): {name!r}")
        if name not in self.permitted_tools:
            raise RobinhoodToolError(f"Tool is not in the configured allowlist: {name}")

    def _run(self, coroutine: Coroutine[object, object, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("RobinhoodClient synchronous methods cannot run inside an active event loop")

    async def _list_tools(self) -> list[dict[str, object]]:
        async with self._session() as session:
            list_tools = getattr(session, "list_tools")
            result: object = await list_tools()
            return normalize_tools(result)

    async def _call_tool(self, name: str, arguments: dict[str, object]) -> JSONValue:
        async with self._session() as session:
            call_tool = getattr(session, "call_tool")
            result: object = await call_tool(name, arguments)
            if getattr(result, "is_error", getattr(result, "isError", False)):
                raise RobinhoodToolError("Robinhood MCP tool returned an error")
            return normalize_result(result)

    async def _run_readonly(self, calls: list[tuple[str, dict[str, object]]]) -> list[JSONValue]:
        results: list[JSONValue] = []
        async with self._session() as session:
            call_tool = getattr(session, "call_tool")
            for name, arguments in calls:
                result: object = await call_tool(name, arguments)
                if getattr(result, "is_error", getattr(result, "isError", False)):
                    raise RobinhoodToolError("Robinhood MCP tool returned an error")
                results.append(normalize_result(result))
        return results

    def _session(self) -> _SessionContext | _HttpSessionContext:
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RobinhoodDependencyError("Install the optional 'mcp' package for Robinhood support") from exc
        auth = build_oauth_provider(self.oauth, self.token_path) if self.oauth else None
        if self.transport_factory:
            transport: object = self.transport_factory(self.server_url, auth=auth)
            return _SessionContext(transport, Client)
        return _HttpSessionContext(self.server_url, auth, streamable_http_client, Client, httpx2)


class _SessionContext:
    def __init__(self, transport: object, session_type: Callable[..., object]) -> None:
        self.transport = transport
        self.session_type = session_type
        self.transport_context: object | None = None
        self.session_context: object | None = None

    async def __aenter__(self) -> object:
        self.transport_context = self.transport
        client: object = self.session_type(self.transport_context)
        self.session_context = client
        enter = getattr(client, "__aenter__")
        result: object = await enter()
        return result

    async def __aexit__(self, *args: object) -> bool | None:
        if not self.session_context:
            return None
        exit_method = getattr(self.session_context, "__aexit__")
        result: object = await exit_method(*args)
        return True if result else None


class _HttpSessionContext:
    def __init__(self, url: str, auth: object | None, transport_factory: Callable[..., object], client_type: Callable[..., object], httpx_module: object) -> None:
        self.url = url
        self.auth = auth
        self.transport_factory = transport_factory
        self.client_type = client_type
        self.httpx_module = httpx_module
        self.http_client: object | None = None
        self.transport_context: object | None = None
        self.client_context: object | None = None

    async def __aenter__(self) -> object:
        async_client_factory = getattr(self.httpx_module, "AsyncClient")
        http_client: object = async_client_factory(
            auth=self.auth, follow_redirects=False
        )
        self.http_client = http_client
        enter_http = getattr(http_client, "__aenter__")
        await enter_http()
        transport: object = self.transport_factory(
            self.url, http_client=self.http_client, terminate_on_close=False
        )
        self.transport_context = transport
        client: object = self.client_type(self.transport_context)
        self.client_context = client
        enter_client = getattr(client, "__aenter__")
        result: object = await enter_client()
        return result

    async def __aexit__(self, *args: object) -> bool | None:
        if self.client_context:
            exit_client = getattr(self.client_context, "__aexit__")
            await exit_client(*args)
        if self.http_client:
            exit_http = getattr(self.http_client, "__aexit__")
            result: object = await exit_http(*args)
            return True if result else None
        return None
