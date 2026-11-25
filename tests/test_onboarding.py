import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update
from test_cross_instance_migration import _signature

from agent_commons import cli, mcp_server, remote_resolution
from agent_commons.a2a import A2AAgentCard
from agent_commons.client import CommonsClient, CommonsError, public_multikey, server_origin
from agent_commons.config import Settings, settings
from agent_commons.db import Base, SessionLocal, engine
from agent_commons.main import app
from agent_commons.models import Agent
from agent_commons.registration import RegistrationInvitation, issue_invitation
from agent_commons.security import REGISTRATION_INVITATION_HEADER, hash_api_key

ORIGIN = "http://127.0.0.1:8000"
ATLAS_CARD = "https://atlas.example/api/v1/agents/atlas/agent-card"


@pytest.fixture
def server(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(settings, "registration_admission", "invite")
    monkeypatch.setattr(settings, "api_url", ORIGIN)
    with TestClient(app) as client:
        yield client


def invite(*, expired=False):
    with SessionLocal.begin() as db:
        token = issue_invitation(db)
        db.flush()
        if expired:
            db.execute(
                update(RegistrationInvitation)
                .where(RegistrationInvitation.token_digest == hash_api_key(token))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    return token


def headers(token):
    return {REGISTRATION_INVITATION_HEADER: token}


def transport_for(server, seen=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        response = server.request(
            request.method, request.url.path, content=request.content, headers=request.headers
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    return httpx.MockTransport(handle)


def test_invitation_consumption_and_registration_rollback(server):
    assert server.post("/agents/register", json={"name": "no-invite"}).status_code == 403
    expired = invite(expired=True)
    assert (
        server.post(
            "/api/v1/agents/register", json={"name": "expired"}, headers=headers(expired)
        ).status_code
        == 403
    )
    first = invite()
    valid = server.post("/agents/register", json={"name": "atlas"}, headers=headers(first))
    assert valid.status_code == 201
    assert (
        server.post(
            "/api/v1/agents/register", json={"name": "replay"}, headers=headers(first)
        ).status_code
        == 403
    )

    second = invite()
    assert (
        server.post(
            "/api/v1/agents/register", json={"name": "atlas"}, headers=headers(second)
        ).status_code
        == 409
    )
    assert (
        server.post(
            "/api/v1/agents/register", json={"name": "nova"}, headers=headers(second)
        ).status_code
        == 201
    )
    with SessionLocal() as db:
        stored = db.get(RegistrationInvitation, hash_api_key(second))
        assert stored.consumed_at is not None
        assert all(row.token_digest != second for row in db.scalars(select(RegistrationInvitation)))


def test_concurrent_invitation_redemption_across_aliases(server):
    token = invite()
    start = Barrier(2)

    def register(path, name):
        start.wait(timeout=5)
        return server.post(path, json={"name": name}, headers=headers(token)).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(register, "/agents/register", "first")
        two = pool.submit(register, "/api/v1/agents/register", "second")
        assert sorted([one.result(timeout=10), two.result(timeout=10)]) == [201, 403]
    with SessionLocal() as db:
        assert len(db.scalars(select(Agent)).all()) == 1


@pytest.mark.parametrize("lineage_aware", [False, True])
def test_migration_completion_cannot_bypass_preview_admission(server, monkeypatch, lineage_aware):
    monkeypatch.setattr(settings, "registration_admission", "open")
    key = Ed25519PrivateKey.generate()
    with CommonsClient(ORIGIN, transport=transport_for(server)) as client:
        token = client.register("atlas")
        client.bind_identity(key)
    auth = {"Authorization": f"Bearer {token}"}
    state = "/api/v1/agents/me/state"
    signing = server.get(state + "/signing-payload", headers=auth).json()["payload"]
    result = server.post(
        state + "/signed-export",
        headers=auth,
        json={
            "payload": signing,
            "signature_multibase": _signature(key, signing),
        },
    )
    assert result.status_code == 200
    body = {"envelope": result.json()}
    path = "/api/v1/agents/migrate"
    if lineage_aware:
        lineage = server.get("/api/v1/agents/me/identity/lineage", headers=auth)
        assert lineage.status_code == 200
        body["lineage"] = lineage.json()
        path += "/lineage"
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(settings, "registration_admission", "invite")
    challenge = server.post(path + "/challenge", json=body)
    assert challenge.status_code == 200
    challenge = challenge.json()
    submission = {
        **body,
        "challenge_id": challenge["challenge_id"],
        "signature_multibase": _signature(key, challenge["payload"]),
    }
    assert server.post(path + "/complete", json=submission).status_code == 403
    assert (
        server.post(path + "/complete", json=submission, headers=headers(invite())).status_code
        == 200
    )


def test_mcp_registration_obeys_gate_without_accepting_secret_as_tool_argument(server, monkeypatch):
    api = mcp_server.CommonsAPI(ORIGIN, None, transport=transport_for(server))
    monkeypatch.setattr(mcp_server, "api", api)
    with pytest.raises(RuntimeError, match="403"):
        asyncio.run(mcp_server.register_agent("mcp-denied"))
    token = invite()
    monkeypatch.setattr(settings, "mcp_registration_invitation", token)
    result = asyncio.run(mcp_server.register_agent("mcp-admitted"))
    assert result["agent"]["name"] == "mcp-admitted"
    with pytest.raises(RuntimeError, match="403") as error:
        asyncio.run(mcp_server.register_agent("mcp-replay"))
    assert token not in str(error.value)


def test_config_rejects_unknown_admission_mode(monkeypatch):
    monkeypatch.setenv("AGENT_COMMONS_REGISTRATION_ADMISSION", "preview")
    with pytest.raises(ValidationError):
        Settings()


def test_sdk_publishes_verified_card_and_inspects_peer(server, monkeypatch):
    seen = []
    transport = transport_for(server, seen)
    atlas_key = Ed25519PrivateKey.generate()
    with CommonsClient(ORIGIN, transport=transport) as atlas:
        atlas.register("atlas", invitation=invite())
        root = atlas.bind_identity(atlas_key)
        assert atlas.bind_identity(atlas_key) == root
        atlas.update_profile(description="Atlas", capabilities=["text-statistics"])
        assert (
            atlas.publish_card(
                atlas_key,
                card_url=ATLAS_CARD,
                endpoint="https://atlas.example/a2a",
                location_sequence=1,
            )
            == root
        )
        with pytest.raises(CommonsError, match="location sequence"):
            atlas.publish_card(
                atlas_key,
                card_url=ATLAS_CARD,
                endpoint="https://atlas.example/a2a",
                location_sequence=1,
            )
    published = server.get("/api/v1/agents/atlas/agent-card")
    assert published.status_code == 200
    card = A2AAgentCard.model_validate(published.json())
    monkeypatch.setattr(remote_resolution, "_fetch_agent_card", lambda _: card)
    with CommonsClient(ORIGIN, transport=transport) as nova:
        nova.register("nova", invitation=invite())
        verification = nova.resolve_peer(ATLAS_CARD)
        assert nova.inspect_peer(verification.id) == verification
    assert verification.root_fingerprint == root
    assert verification.lineage_verified and verification.identity_verified
    assert verification.endpoint_authorization_status == "authorized"
    assert all(atlas_key.private_bytes_raw() not in req.content for req in seen)
    signing = next(req for req in seen if req.url.path.endswith("/identity/challenge"))
    assert json.loads(signing.content) == {"public_key_multibase": public_multikey(atlas_key)}


def test_sdk_refuses_redirects_and_does_not_echo_remote_error():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(
            307, headers={"Location": "https://different.example/"}, text="server-echoed-secret"
        )

    with CommonsClient("https://local.example", transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(CommonsError, match="HTTP 307") as error:
            client.register("atlas", invitation="private-token")
    assert len(requests) == 1
    assert "private-token" not in str(error.value)
    assert "server-echoed-secret" not in str(error.value)
    assert requests[0].url.host == "local.example"


@pytest.mark.parametrize(
    "value",
    [
        "http://outside.example",
        "https://user:pass@origin.example",
        "https://origin.example?token=private",
    ],
)
def test_sdk_origin_rejects_unsafe_inputs(value):
    with pytest.raises(ValueError, match="HTTPS server origin"):
        server_origin(value)


def test_cli_saves_secrets_privately_and_refuses_existing_configuration(
    server, tmp_path, monkeypatch, capsys
):
    config = tmp_path / "atlas"

    def run(*args):
        cli.run(cli._parser().parse_args(["--config", str(config), *args]))

    run("init", "--url", ORIGIN, "--name", "atlas")
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    assert stat.S_IMODE((config / "controller.key").stat().st_mode) == 0o600
    controller = (config / "controller.key").read_bytes()
    with pytest.raises(CommonsError, match="already exists"):
        run("init", "--url", ORIGIN, "--name", "another")
    assert (config / "controller.key").read_bytes() == controller

    invitation_file = tmp_path / "invitation"
    cli._operator_invite(invitation_file)
    token = invitation_file.read_text().strip()
    assert stat.S_IMODE(invitation_file.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        cli._operator_invite(invitation_file)
    monkeypatch.setattr(
        cli,
        "CommonsClient",
        lambda url, api_key: CommonsClient(url, api_key=api_key, transport=transport_for(server)),
    )
    run("register", "--invitation-file", str(invitation_file))
    run("identity")
    account = json.loads((config / "account.json").read_text())
    assert stat.S_IMODE((config / "account.json").stat().st_mode) == 0o600
    assert account["state"] == "registered"
    output = capsys.readouterr()
    assert "Verified sovereign fingerprint" in output.out
    assert all(secret not in output.out + output.err for secret in (account["api_key"], token))


def test_cli_fails_closed_on_ambiguous_registration_and_unsafe_files(tmp_path, monkeypatch):
    config = tmp_path / "atlas"

    def run(*args):
        cli.run(cli._parser().parse_args(["--config", str(config), *args]))

    run("init", "--url", ORIGIN, "--name", "atlas")
    account = config / "account.json"
    requests = []

    def interrupted(request):
        requests.append(request)
        raise httpx.ReadTimeout("sensitive failure")

    monkeypatch.setattr(
        cli,
        "CommonsClient",
        lambda url, api_key: CommonsClient(
            url, api_key=api_key, transport=httpx.MockTransport(interrupted)
        ),
    )
    account.chmod(0o644)
    with pytest.raises(CommonsError, match="0600"):
        run("register")
    account.chmod(0o600)
    for _ in range(2):
        with pytest.raises(CommonsError, match="outcome is unknown"):
            run("register")
    assert len(requests) == 1
    assert json.loads(account.read_text())["state"] == "registering"


def test_sdk_refuses_to_sign_a_challenge_for_a_different_audience(server):
    requests = []
    genuine = transport_for(server)

    def altered(request):
        requests.append(request.url.path)
        response = genuine.handle_request(request)
        if request.url.path.endswith("/identity/challenge"):
            challenge = response.json()
            challenge["payload"] = challenge["payload"].replace(ORIGIN, "https://other.example")
            return httpx.Response(200, json=challenge)
        return response

    with CommonsClient(ORIGIN, transport=httpx.MockTransport(altered)) as client:
        client.register("atlas", invitation=invite())
        with pytest.raises(CommonsError, match="expected audience"):
            client.bind_identity(Ed25519PrivateKey.generate())
    assert "/api/v1/agents/me/identity/verify" not in requests
