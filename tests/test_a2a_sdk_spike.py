"""Wire interoperability with upstream's unmodified client over local HTTPS."""

import asyncio
import os
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest

from scripts.agent_internet_live import card_path, grant, publish, register
from scripts.agent_internet_live_support import Server, isolated_databases, make_tls

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_COMMONS_A2A_SDK_SPIKE") != "1",
    reason="Install the spike requirements and opt in with AGENT_COMMONS_A2A_SDK_SPIKE=1",
)


@pytest.fixture
def sdk_server():
    admin_url = os.environ.get("AGENT_COMMONS_DEMO_DATABASE_URL")
    if not admin_url:
        pytest.fail("Set AGENT_COMMONS_DEMO_DATABASE_URL to a disposable PostgreSQL admin URL")
    with isolated_databases(admin_url, ("atlas",)) as databases, ExitStack() as resources:
        directory = resources.enter_context(TemporaryDirectory(prefix="a2a-sdk-spike-"))
        cert, key, tls = make_tls(Path(directory))
        node = Server.provision(resources, databases["atlas"])
        client = resources.enter_context(
            httpx.Client(verify=tls, trust_env=False, follow_redirects=False, timeout=5)
        )
        entrypoint = "agent_commons.main:app"
        node.start(client, cert, key, [], app_entrypoint=entrypoint)
        atlas = register(client, node, "atlas")
        nova = register(client, node, "nova")
        publish(client, node, atlas, 1)
        token, peer = grant(nova, atlas)
        wrong_peer_token, wrong_peer = grant(atlas, atlas)
        expired_token, expired_peer = grant(nova, atlas, expired=True)
        node.start(
            client,
            cert,
            key,
            [],
            peers=[peer, wrong_peer, expired_peer],
            app_entrypoint=entrypoint,
        )
        yield node.url, tls, token, wrong_peer_token, expired_token


def test_upstream_sdk_0_3_compatibility_and_native_1_0_boundary(sdk_server):
    from a2a.client import A2ACardResolver, A2AClientError
    from a2a.client.transports.jsonrpc import JsonRpcTransport
    from a2a.compat.v0_3.jsonrpc_transport import CompatJsonRpcTransport
    from a2a.types import a2a_pb2 as pb
    from a2a.utils.errors import MethodNotFoundError, TaskNotFoundError

    assert version("a2a-sdk") == "1.3.1"
    origin, tls, token, wrong_peer, expired_token = sdk_server
    endpoint = origin + "/a2a"
    message = pb.SendMessageRequest(
        message=pb.Message(
            message_id=uuid4().hex,
            role=pb.ROLE_USER,
            parts=[pb.Part(text="hello world")],
        )
    )

    async def exercise():
        def client_for(credential=None):
            headers = {"Authorization": f"Bearer {credential}"} if credential else {}
            return httpx.AsyncClient(
                verify=tls, trust_env=False, follow_redirects=False, timeout=5, headers=headers
            )

        async with client_for() as public:
            card = await A2ACardResolver(public, origin, card_path("atlas")).get_agent_card()
            (primary,) = card.supported_interfaces
            assert (primary.url, primary.protocol_binding, primary.protocol_version) == (
                endpoint,
                "JSONRPC",
                "0.3.0",
            )
            assert card.security_schemes["peer"].http_auth_security_scheme.scheme == "bearer"
            assert [skill.id for skill in card.skills] == ["text-statistics"]
            assert any(
                ext.uri.endswith(":sovereign-identity:v2") for ext in card.capabilities.extensions
            )
            anonymous = CompatJsonRpcTransport(public, card, endpoint)
            with pytest.raises(A2AClientError, match="401"):
                await anonymous.send_message(message)

        async with client_for(token) as enrolled:
            compat = CompatJsonRpcTransport(enrolled, card, endpoint)
            response = await compat.send_message(message)
            task = response.task
            assert task.id and task.context_id
            assert task.status.state == pb.TASK_STATE_COMPLETED
            (artifact,) = task.artifacts
            assert artifact.artifact_id
            assert [part.text for part in artifact.parts] == ["bytes: 11\nwords: 2"]
            get = pb.GetTaskRequest(id=task.id)
            assert await compat.get_task(get) == task
            assert (await compat.send_message(message)).task == task

            different = pb.SendMessageRequest()
            different.CopyFrom(message)
            different.message.parts[0].text = "Other sample"
            with pytest.raises(A2AClientError, match="different task content"):
                await compat.send_message(different)

            with pytest.raises(TaskNotFoundError):
                await compat.get_task(pb.GetTaskRequest(id=str(uuid4())))
            for wire, code in [
                (b"{", -32700),
                (b'{"jsonrpc":"2.0","id":2,"method":"message/send","params":{}}', -32602),
            ]:
                error = await enrolled.post(
                    endpoint, content=wire, headers={"Content-Type": "application/json"}
                )
                assert error.status_code == 200
                assert error.json()["error"]["code"] == code

            native = JsonRpcTransport(enrolled, card, endpoint)
            with pytest.raises(MethodNotFoundError):
                await native.send_message(message)
            with pytest.raises(MethodNotFoundError):
                await native.get_task(get)

        async with client_for(wrong_peer) as other:
            with pytest.raises(TaskNotFoundError):
                await CompatJsonRpcTransport(other, card, endpoint).get_task(get)
        async with client_for(expired_token) as expired:
            with pytest.raises(A2AClientError, match="401"):
                await CompatJsonRpcTransport(expired, card, endpoint).send_message(message)

    asyncio.run(exercise())
