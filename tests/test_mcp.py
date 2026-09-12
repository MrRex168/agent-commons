import asyncio

import httpx
import pytest

from agent_commons.mcp_server import CommonsAPI, mcp


def test_mcp_exposes_core_agent_tools() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}

    expected = {
        "register_agent",
        "get_identity",
        "list_spaces",
        "create_space",
        "join_space",
        "add_private_member",
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
        assert request.headers["Authorization"] == "Bearer ac_test_key"
        return httpx.Response(200, json={"name": "atlas"})

    api = CommonsAPI(
        "http://agent-commons.test",
        api_key="ac_test_key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(api.request("GET", "/agents/me", require_auth=True))
    assert result == {"name": "atlas"}


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
