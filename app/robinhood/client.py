"""Synchronous, read-only facade over Robinhood's MCP server."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from .auth import DEFAULT_TOKEN_PATH, OAuthConfig, build_oauth_provider
from .capabilities import (
    ACCOUNT_READ_TOOLS,
    MARKET_READ_TOOLS,
    allowed_read_tools,
    is_blocked,
)


class RobinhoodDependencyError(RuntimeError):
    pass


class RobinhoodAuthRequired(RuntimeError):
    pass


class RobinhoodToolError(RuntimeError):
    pass


def normalize_result(result: Any) -> Any:
    """Convert SDK model objects and MCP content into JSON-like values."""
    if result is None or isinstance(result, (str, int, float, bool)):
        return result
    if isinstance(result, dict):
        return {str(k): normalize_result(v) for k, v in result.items()}
    if isinstance(result, (list, tuple)):
        return [normalize_result(v) for v in result]
    if hasattr(result, "model_dump"):
        return normalize_result(result.model_dump(exclude_none=True))
    if hasattr(result, "dict"):
        return normalize_result(result.dict(exclude_none=True))
    if hasattr(result, "text"):
        return result.text
    return {k: normalize_result(v) for k, v in vars(result).items() if not k.startswith("_")}


def normalize_tools(result: Any) -> list[dict[str, Any]]:
    value = normalize_result(getattr(result, "tools", result))
    if isinstance(value, dict):
        value = value.get("tools", [])
    return [v if isinstance(v, dict) else {"name": str(v)} for v in value]


class RobinhoodClient:
    def __init__(self, server_url: str, *, oauth: OAuthConfig | None = None,
                 token_path=DEFAULT_TOKEN_PATH,
                 market_tools: frozenset[str] | None = None,
                 account_tools: frozenset[str] | None = None,
                 allowed_tools: set[str] | None = None,
                 transport_factory: Callable[..., Any] | None = None):
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

    def list_tools(self) -> list[dict[str, Any]]:
        return self._run(self._list_tools())

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self._check_tool(name)
        return self._run(self._call_tool(name, arguments or {}))

    def run_readonly(self, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Run read-only calls in one authenticated MCP session."""
        for name, _ in calls:
            self._check_tool(name)
        return self._run(self._run_readonly(calls))

    def _check_tool(self, name: str) -> None:
        if not isinstance(name, str) or not name or is_blocked(name):
            raise RobinhoodToolError(f"Tool is not permitted (read-only policy): {name!r}")
        if name not in self.permitted_tools:
            raise RobinhoodToolError(f"Tool is not in the configured allowlist: {name}")

    def _run(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("RobinhoodClient synchronous methods cannot run inside an active event loop")

    async def _list_tools(self):
        async with self._session() as session:
            return normalize_tools(await session.list_tools())

    async def _call_tool(self, name, arguments):
        async with self._session() as session:
            result = await session.call_tool(name, arguments)
            if getattr(result, "is_error", getattr(result, "isError", False)):
                raise RobinhoodToolError("Robinhood MCP tool returned an error")
            return normalize_result(result)

    async def _run_readonly(self, calls):
        results = []
        async with self._session() as session:
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                if getattr(result, "is_error", getattr(result, "isError", False)):
                    raise RobinhoodToolError("Robinhood MCP tool returned an error")
                results.append(normalize_result(result))
        return results

    def _session(self):
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RobinhoodDependencyError("Install the optional 'mcp' package for Robinhood support") from exc
        auth = build_oauth_provider(self.oauth, self.token_path) if self.oauth else None
        if self.transport_factory:
            transport = self.transport_factory(self.server_url, auth=auth)
            return _SessionContext(transport, Client)
        return _HttpSessionContext(self.server_url, auth, streamable_http_client, Client, httpx2)


class _SessionContext:
    def __init__(self, transport, session_type):
        self.transport, self.session_type = transport, session_type
        self.transport_context = None
        self.session_context = None

    async def __aenter__(self):
        self.transport_context = self.transport
        client = self.session_type(self.transport_context)
        self.session_context = client
        return await self.session_context.__aenter__()

    async def __aexit__(self, *args):
        if self.session_context:
            return await self.session_context.__aexit__(*args)


class _HttpSessionContext:
    def __init__(self, url, auth, transport_factory, client_type, httpx_module):
        self.url = url
        self.auth = auth
        self.transport_factory = transport_factory
        self.client_type = client_type
        self.httpx_module = httpx_module
        self.http_client = None
        self.transport_context = None
        self.client_context = None

    async def __aenter__(self):
        self.http_client = self.httpx_module.AsyncClient(
            auth=self.auth, follow_redirects=False
        )
        await self.http_client.__aenter__()
        self.transport_context = self.transport_factory(
            self.url, http_client=self.http_client, terminate_on_close=False
        )
        self.client_context = self.client_type(self.transport_context)
        return await self.client_context.__aenter__()

    async def __aexit__(self, *args):
        if self.client_context:
            await self.client_context.__aexit__(*args)
        if self.http_client:
            return await self.http_client.__aexit__(*args)
