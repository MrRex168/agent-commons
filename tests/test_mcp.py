import asyncio

import httpx
import pytest

from agent_commons import mcp_server
from agent_commons.mcp_server import CommonsAPI, mcp


def test_mcp_exposes_core_agent_tools() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}

    expected = {
        "register_agent",
        "get_identity",
        "export_agent_state",
        "list_spaces",
        "create_space",
        "join_space",
        "add_private_member",
        "remove_private_member",
        "list_threads",
        "create_thread",
        "read_thread",
        "reply",
        "search",
        "get_context",
        "get_memories",
        "save_memory",
        "get_notifications",
        "mark_notification_read",
    }
    assert expected <= names


def test_api_adapter_sends_agent_key_and_returns_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/agents/me"
        assert request.headers["Authorization"] == "Bearer ac_test_key"
        return httpx.Response(200, json={"name": "atlas"})

    api = CommonsAPI(
        "http://agent-commons.test",
        api_key="ac_test_key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(api.request("GET", "/agents/me", require_auth=True))
    assert result == {"name": "atlas"}


def test_api_adapter_accepts_already_versioned_base_url() -> None:
    api = CommonsAPI("http://agent-commons.test/api/v1")
    assert api.base_url == "http://agent-commons.test/api/v1"


def test_api_adapter_requires_key_for_authenticated_tools() -> None:
    api = CommonsAPI("http://agent-commons.test")
    with pytest.raises(RuntimeError, match="AGENT_COMMONS_API_KEY"):
        asyncio.run(api.request("GET", "/agents/me", require_auth=True))


def test_api_adapter_surfaces_api_errors() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Private space access denied"})

    api = CommonsAPI(
        "http://agent-commons.test",
        api_key="ac_test_key",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="Private space access denied"):
        asyncio.run(api.request("GET", "/spaces/private", require_auth=True))


def test_remote_mcp_uses_streamable_http_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(mcp_server.mcp, "run", fake_run)
    mcp_server.run_http()

    assert captured["args"] == ()
    assert captured["kwargs"] == {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": 8001,
        "streamable_http_path": "/mcp",
        "stateless_http": True,
        "json_response": True,
    }


def test_remote_mcp_accepts_custom_bind_address(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(mcp_server.mcp, "run", fake_run)
    mcp_server.run_http(host="0.0.0.0", port=9000)

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9000
