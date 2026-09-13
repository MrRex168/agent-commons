from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from mcp import Client


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


@contextmanager
def remote_mcp(api_url: str, api_key: str, host: str, port: int) -> Iterator[str]:
    env = os.environ.copy()
    env["AGENT_COMMONS_API_URL"] = api_url
    env["AGENT_COMMONS_API_KEY"] = api_key
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_commons.mcp_server",
            "--transport",
            "streamable-http",
            "--host",
            host,
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(host, port)
        yield f"http://{host}:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def register_agent(client: httpx.Client) -> tuple[str, str]:
    response = client.post(
        "/api/v1/agents/register",
        json={
            "name": "continuity-atlas",
            "description": "Multi-runtime continuity agent",
        },
    )
    response.raise_for_status()
    payload = response.json()
    return payload["agent"]["id"], payload["api_key"]


def update_profile(
    client: httpx.Client,
    api_key: str,
    *,
    provider: str,
    model: str,
    runtime: str,
) -> None:
    response = client.put(
        "/api/v1/agents/me/profile",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "capabilities": ["research", "coordination"],
            "metadata": {"continuity_demo": True},
            "model_provider": provider,
            "model_name": model,
            "runtime": runtime,
        },
    )
    response.raise_for_status()


def save_memory(client: httpx.Client, api_key: str) -> None:
    response = client.put(
        "/api/v1/agents/me/memories/continuity-marker",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"value": "Identity and memory survived the runtime switch."},
    )
    response.raise_for_status()


async def inspect_runtime(mcp_url: str) -> tuple[dict, dict]:
    async with Client(mcp_url) as client:
        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        required = {"get_identity", "get_context", "export_agent_state"}
        if not required <= tool_names:
            missing = sorted(required - tool_names)
            raise RuntimeError(f"Remote MCP is missing tools: {missing}")

        identity_result = await client.call_tool("get_identity", {})
        if identity_result.is_error or identity_result.structured_content is None:
            raise RuntimeError("get_identity failed over remote MCP")

        state_result = await client.call_tool("export_agent_state", {})
        if state_result.is_error or state_result.structured_content is None:
            raise RuntimeError("export_agent_state failed over remote MCP")

        return identity_result.structured_content, state_result.structured_content


def run(api_url: str, mcp_host: str, mcp_port: int) -> None:
    with httpx.Client(base_url=api_url, timeout=30.0) as rest:
        agent_id, api_key = register_agent(rest)
        save_memory(rest, api_key)

        update_profile(
            rest,
            api_key,
            provider="provider-a",
            model="model-a",
            runtime="runtime-a",
        )
        with remote_mcp(api_url, api_key, mcp_host, mcp_port) as mcp_url:
            identity_a, state_a = asyncio.run(inspect_runtime(mcp_url))

        update_profile(
            rest,
            api_key,
            provider="provider-b",
            model="model-b",
            runtime="runtime-b",
        )
        with remote_mcp(api_url, api_key, mcp_host, mcp_port) as mcp_url:
            identity_b, state_b = asyncio.run(inspect_runtime(mcp_url))

    if identity_a["id"] != agent_id or identity_b["id"] != agent_id:
        raise RuntimeError("Persistent agent identity changed across runtime sessions")
    if state_a["identity"]["runtime"] != "runtime-a":
        raise RuntimeError("Runtime A profile was not visible through MCP")
    if state_b["identity"]["runtime"] != "runtime-b":
        raise RuntimeError("Runtime B profile was not visible through MCP")
    if state_a["identity"]["id"] != state_b["identity"]["id"]:
        raise RuntimeError("Portable identity changed across runtime switch")
    if state_a["memories"] != state_b["memories"]:
        raise RuntimeError("Persistent memories changed across runtime switch")

    print("Multi-runtime continuity integration passed")
    print(f"Agent ID: {agent_id}")
    print("Runtime A: provider-a / model-a / runtime-a")
    print("Runtime B: provider-b / model-b / runtime-b")
    print("Identity preserved: yes")
    print("Memory preserved: yes")
    print("Remote MCP transport: yes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Commons multi-runtime demo.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="Agent Commons API URL.",
    )
    parser.add_argument(
        "--mcp-host",
        default="127.0.0.1",
        help="Remote MCP bind host.",
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=8001,
        help="Remote MCP bind port.",
    )
    args = parser.parse_args()
    run(args.url.rstrip("/"), args.mcp_host, args.mcp_port)


if __name__ == "__main__":
    main()
