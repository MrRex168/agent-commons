"""Run a disposable local demonstration and emit sanitized JSON evidence."""

import argparse
import hashlib
import json
import os
import secrets
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_commons.a2a_identity import SOVEREIGN_ENDPOINT_EXTENSION_URI
from agent_commons.endpoint_authorization import EndpointAuthorization
from agent_commons.security import REGISTRATION_INVITATION_HEADER
from scripts.agent_internet_live_support import Server, isolated_databases, make_tls
from scripts.cross_instance_migration_demo import _auth, _keypair, _signature

AGENTS = "/api/v1/agents"
REMOTE = AGENTS + "/remote"
FOLLOWS = AGENTS + "/relationships"


@dataclass
class Identity:
    name: str
    agent_id: str
    root: str
    key: Ed25519PrivateKey = field(repr=False)
    api_key: str = field(repr=False)


def require(condition, check: str) -> None:
    if not condition:
        raise RuntimeError(check)


def request(
    client: httpx.Client,
    verb: str,
    server: Server,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    status: int = 200,
    invitation: str | None = None,
):
    headers = _auth(token) if token else {}
    if invitation is not None:
        headers[REGISTRATION_INVITATION_HEADER] = invitation
    response = client.request(verb, server.url + path, headers=headers, json=body)
    require(
        response.status_code == status,
        f"{verb} {path}: expected {status}, received {response.status_code}",
    )
    return response.json()


def register(client: httpx.Client, server: Server, name: str) -> Identity:
    account = request(client, "POST", server, f"{AGENTS}/register", body={"name": name}, status=201)
    token = account["api_key"]
    private, public = _keypair()
    identity_path = f"{AGENTS}/me/identity"
    challenge = request(
        client,
        "POST",
        server,
        identity_path + "/challenge",
        token=token,
        body={"public_key_multibase": public},
    )
    result = request(
        client,
        "POST",
        server,
        identity_path + "/verify",
        token=token,
        body={
            "challenge_id": challenge["challenge_id"],
            "signature_multibase": _signature(private, challenge["payload"]),
        },
    )
    return Identity(name, account["agent"]["id"], result["fingerprint"], private, token)


def card_path(name: str) -> str:
    return f"{AGENTS}/{name}/agent-card"


def publish(
    client: httpx.Client,
    node: Server,
    agent: Identity,
    counter: int,
    *,
    invalid: bool = False,
) -> None:
    lineage = request(client, "GET", node, f"{AGENTS}/me/identity/lineage", token=agent.api_key)
    now = int(datetime.now(UTC).timestamp())
    claim = EndpointAuthorization(
        version=1,
        root_fingerprint=agent.root,
        controller_public_key_multibase=lineage["current_public_key_multibase"],
        identity_sequence=lineage["sequence"],
        location_sequence=counter,
        card_url=node.url + card_path(agent.name),
        a2a_url=node.url + "/a2a",
        transport="JSONRPC",
        issued_at=now,
        expires_at=now + 1800,
    )
    signer = _keypair()[0] if invalid else agent.key
    request(
        client,
        "PUT",
        node,
        f"{AGENTS}/me/profile",
        token=agent.api_key,
        body={
            "metadata": {
                "a2a": {
                    "url": claim.a2a_url,
                    "preferredTransport": "JSONRPC",
                    "publishSovereignIdentity": True,
                    "securitySchemes": {"peer": {"type": "http", "scheme": "bearer"}},
                    "security": [{"peer": []}],
                    "skills": [
                        {
                            "id": "text-statistics",
                            "name": "Text Statistics",
                            "description": "Counts UTF-8 bytes and words",
                            "tags": ["text"],
                        }
                    ],
                    "endpointAuthorization": {
                        "authorization": claim.model_dump(),
                        "signature_multibase": _signature(signer, claim.signing_payload()),
                    },
                }
            },
        },
    )


def resolve(
    client: httpx.Client,
    observer: Server,
    agent: Identity,
    provider: Server,
    provider_name: str,
    *,
    status: int = 200,
) -> dict:
    return request(
        client,
        "POST",
        observer,
        REMOTE + "/resolve",
        token=agent.api_key,
        body={"agent_card_url": provider.url + card_path(provider_name)},
        status=status,
    )


def follow(client: httpx.Client, node: Server, agent: Identity, reference: dict) -> dict:
    return request(
        client,
        "POST",
        node,
        FOLLOWS,
        token=agent.api_key,
        status=201,
        body={"remote_reference_id": reference["id"]},
    )


def grant(caller: Identity, recipient: Identity, *, expired: bool = False) -> tuple[str, dict]:
    token = "a2a_" + secrets.token_urlsafe(32)
    retry_window = timedelta(hours=-1 if expired else 1)
    return token, {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "caller_root": caller.root,
        "recipient_agent_id": recipient.agent_id,
        "scopes": ["message/send", "tasks/get"],
        "expires_at": (datetime.now(UTC) + retry_window).isoformat(),
    }


def outbound(caller: Identity, recipient: Identity, node: Server, token: str) -> dict:
    return {
        "caller_id": caller.agent_id,
        "recipient_root": recipient.root,
        "endpoint": node.url + "/a2a",
        "token": token,
    }


def rpc(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": uuid4().hex, "method": method, "params": params}


def send_message(message_id: str, content: str) -> dict:
    return rpc(
        "message/send",
        {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": message_id,
                "parts": [{"kind": "text", "text": content}],
            },
        },
    )


def dispatch(
    client: httpx.Client,
    node: Server,
    agent: Identity,
    reference: dict,
    call: dict,
    *,
    status: int = 200,
) -> dict:
    response = request(
        client,
        "POST",
        node,
        "/__demo/dispatch",
        token=agent.api_key,
        body={"reference_id": reference["id"], "rpc": call},
        status=status,
    )
    if status == 200:
        require(response["id"] == call["id"], "Task response correlation failed")
    return response


def exchange(
    client: httpx.Client,
    origin: Server,
    remote: Server,
    caller: Identity,
    reference: dict,
    *,
    phase: str,
    source_label: str,
    target_label: str,
    check_retries: bool = False,
) -> dict:
    require(origin.url != remote.url, "Task exchange requires different servers")
    require(reference["a2a_url"] == remote.url + "/a2a", "Unexpected verified task destination")
    text = "Hello from Agent Commons"
    message_id = uuid4().hex
    sent = dispatch(client, origin, caller, reference, send_message(message_id, text))["result"]
    fetched = dispatch(client, origin, caller, reference, rpc("tasks/get", {"id": sent["id"]}))
    require(fetched["result"] == sent, "Retrieved task differed from completed task")
    require(sent["kind"] == "task" and sent["status"]["state"] == "completed", "Incomplete task")
    require(
        sent["artifacts"][0]["parts"] == [{"kind": "text", "text": "bytes: 24\nwords: 4"}],
        "Unexpected Text Statistics result",
    )
    if check_retries:
        duplicate = dispatch(client, origin, caller, reference, send_message(message_id, text))
        require(duplicate["result"] == sent, "Idempotent retry changed task")
        conflicting = dispatch(
            client, origin, caller, reference, send_message(message_id, "Different text")
        )
        require(conflicting["error"]["code"] == -32010, "Conflicting retry was accepted")
    return {
        "phase": phase,
        "source": source_label,
        "destination": target_label,
        "task_id": sent["id"],
        "state": sent["status"]["state"],
        "retrieved": True,
    }


def migrate(
    client: httpx.Client,
    source: Server,
    destination: Server,
    agent: Identity,
    *,
    invitation: str | None = None,
) -> Identity:
    state = f"{AGENTS}/me/state/freshness"
    signing = request(client, "GET", source, state + "/signing-payload", token=agent.api_key)
    envelope = request(
        client,
        "POST",
        source,
        state + "/signed-export",
        token=agent.api_key,
        body={
            "payload": signing["payload"],
            "signature_multibase": _signature(agent.key, signing["payload"]),
        },
    )
    lineage = request(
        client,
        "GET",
        source,
        f"{AGENTS}/me/identity/lineage",
        token=agent.api_key,
    )
    package = {"envelope": envelope, "lineage": lineage}
    path = f"{AGENTS}/migrate/lineage"
    challenge = request(client, "POST", destination, path + "/challenge", body=package)
    result = request(
        client,
        "POST",
        destination,
        path + "/complete",
        invitation=invitation,
        body={
            **package,
            "challenge_id": challenge["challenge_id"],
            "signature_multibase": _signature(agent.key, challenge["payload"]),
        },
    )
    require(result["identity"]["fingerprint"] == agent.root, "Nova's sovereign root changed")
    require(result["agent"]["id"] != agent.agent_id, "Migration reused the source local account")
    require(result["api_key"] != agent.api_key, "Migration reused the source local credential")
    request(client, "GET", destination, f"{AGENTS}/me", token=agent.api_key, status=401)
    return Identity(agent.name, result["agent"]["id"], agent.root, agent.key, result["api_key"])


def location_sequence(reference: dict) -> int:
    extensions = reference["card_snapshot"]["capabilities"]["extensions"]
    (claim,) = (
        item["params"]["endpointAuthorization"]["authorization"]
        for item in extensions
        if item["uri"] == SOVEREIGN_ENDPOINT_EXTENSION_URI
    )
    return claim["location_sequence"]


def demonstrate(client: httpx.Client, nodes: dict, cert: Path, key: Path) -> dict:
    atlas_node, nova_source, nova_target = (nodes[label] for label in ("atlas", "source", "target"))
    cards = [
        atlas_node.url + card_path("atlas"),
        nova_source.url + card_path("nova"),
        nova_target.url + card_path("nova"),
    ]
    for node in nodes.values():
        node.start(client, cert, key, cards)

    atlas = register(client, atlas_node, "atlas")
    nova = register(client, nova_source, "nova")
    request(client, "GET", nova_source, f"{AGENTS}/me", token=atlas.api_key, status=401)
    request(client, "GET", atlas_node, f"{AGENTS}/me", token=nova.api_key, status=401)
    publish(client, atlas_node, atlas, 1)
    publish(client, nova_source, nova, 1, invalid=True)
    request(client, "GET", nova_source, card_path("nova"), status=409)
    resolve(client, atlas_node, atlas, nova_source, "nova", status=502)
    publish(client, nova_source, nova, 1)
    nova_reference = resolve(client, atlas_node, atlas, nova_source, "nova")
    atlas_reference = resolve(client, nova_source, nova, atlas_node, "atlas")
    for reference, expected_root in ((nova_reference, nova.root), (atlas_reference, atlas.root)):
        require(
            reference["identity_verified"] and reference["root_fingerprint"] == expected_root,
            "Unexpected verified sovereign identity",
        )

    atlas_follow = follow(client, atlas_node, atlas, nova_reference)
    nova_follow = follow(client, nova_source, nova, atlas_reference)
    before = send_message(uuid4().hex, "Follow does not grant task access")
    dispatch(client, atlas_node, atlas, nova_reference, before, status=403)
    atlas_to_nova, nova_grant = grant(atlas, nova)
    nova_to_atlas, atlas_grant = grant(nova, atlas)
    expired_token, expired_grant = grant(atlas, nova, expired=True)
    atlas_to_source = outbound(atlas, nova, nova_source, atlas_to_nova)
    atlas_node.start(client, cert, key, cards, peers=[atlas_grant], links=[atlas_to_source])
    nova_source.start(
        client,
        cert,
        key,
        cards,
        peers=[nova_grant, expired_grant],
        links=[outbound(nova, atlas, atlas_node, nova_to_atlas)],
    )
    for token in (atlas.api_key, nova_to_atlas, expired_token):
        request(client, "POST", nova_source, "/a2a", token=token, body=before, status=401)

    exchanges = [
        exchange(
            client,
            atlas_node,
            nova_source,
            atlas,
            nova_reference,
            phase="before",
            source_label="atlas",
            target_label="nova-source",
            check_retries=True,
        ),
        exchange(
            client,
            nova_source,
            atlas_node,
            nova,
            atlas_reference,
            phase="before",
            source_label="nova-source",
            target_label="atlas",
        ),
    ]

    relocated = migrate(client, nova_source, nova_target, nova)
    publish(client, nova_target, relocated, 2)
    atlas_to_target, target_grant = grant(atlas, relocated)
    nova_target.start(client, cert, key, cards, peers=[target_grant])
    # Keep the old Card to exercise rollback rejection, but retire its task endpoint.
    nova_source.start(client, cert, key, cards)
    request(client, "POST", nova_source, "/a2a", token=atlas_to_nova, body=before, status=404)
    updated = resolve(client, atlas_node, atlas, nova_target, "nova")
    require(
        updated["identity_verified"]
        and updated["root_fingerprint"] == nova.root
        and updated["id"] == nova_reference["id"],
        "Remote reference continuity failed",
    )
    resolve(client, atlas_node, atlas, nova_source, "nova", status=409)
    before_sequence = location_sequence(nova_reference)
    after_sequence = location_sequence(updated)
    require((before_sequence, after_sequence) == (1, 2), "Unexpected signed location sequences")
    retained = request(client, "GET", atlas_node, FOLLOWS, token=atlas.api_key)
    require(
        len(retained) == 1
        and retained[0]["id"] == atlas_follow["id"]
        and retained[0]["remote_reference_id"] == nova_reference["id"]
        and retained[0]["remote_root_fingerprint"] == nova.root
        and retained[0]["remote_identity_verified"]
        and retained[0]["remote_a2a_url"] == nova_target.url + "/a2a",
        "Atlas follow did not retain authorized continuity",
    )
    dispatch(client, atlas_node, atlas, updated, before, status=403)  # Old outbound grant only.
    request(client, "POST", nova_target, "/a2a", token=atlas_to_nova, body=before, status=401)
    atlas_node.start(
        client,
        cert,
        key,
        cards,
        peers=[atlas_grant],
        links=[outbound(atlas, relocated, nova_target, atlas_to_target)],
    )
    exchanges.append(
        exchange(
            client,
            atlas_node,
            nova_target,
            atlas,
            updated,
            phase="after",
            source_label="atlas",
            target_label="nova-target",
        )
    )
    require(len({item["task_id"] for item in exchanges}) == 3, "Task IDs are not distinct")
    return {
        "schema_version": 1,
        "topology": "isolated-loopback-https",
        "nodes": ["atlas", "nova-source", "nova-target"],
        "identities": {"atlas": atlas.root, "nova": nova.root},
        "exchanges": exchanges,
        "migration": {
            "root_fingerprint": relocated.root,
            "reference_id_before": nova_reference["id"],
            "reference_id_after": updated["id"],
            "follow_id_before": atlas_follow["id"],
            "follow_id_after": retained[0]["id"],
            "reverse_follow_id": nova_follow["id"],
            "location_sequence_before": before_sequence,
            "location_sequence_after": after_sequence,
            "endpoint_authorization_before": nova_reference["endpoint_authorization_status"],
            "endpoint_authorization_after": updated["endpoint_authorization_status"],
            "old_task_endpoint_retired": True,
            "exclusive_control_verified": False,
        },
        "checks": {
            "database_isolation": True,
            "bad_credentials_rejected": True,
            "wrong_peer_rejected": True,
            "expired_credential_rejected": True,
            "invalid_signature_rejected": True,
            "stale_location_rejected": True,
            "idempotent_retry": True,
            "conflicting_retry_rejected": True,
            "follow_alone_denied": True,
            "moved_location_requires_new_enrollment": True,
        },
    }


def run(admin_dsn: str) -> dict:
    with TemporaryDirectory(prefix="agent-internet-live-") as directory:
        cert, key, tls = make_tls(Path(directory))
        with isolated_databases(admin_dsn, ("atlas", "source", "target")) as databases:
            with ExitStack() as resources:
                client = resources.enter_context(
                    httpx.Client(verify=tls, trust_env=False, follow_redirects=False, timeout=10)
                )
                nodes = {
                    label: Server.provision(resources, database)
                    for label, database in databases.items()
                }
                evidence = demonstrate(client, nodes, cert, key)
    evidence["checks"]["cleanup_completed"] = True
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="Also write sanitized JSON to this file")
    args = parser.parse_args()
    admin_dsn = os.environ.get("AGENT_COMMONS_DEMO_DATABASE_URL")
    if not admin_dsn:
        parser.error(
            "Set AGENT_COMMONS_DEMO_DATABASE_URL to a disposable local PostgreSQL admin URL"
        )
    try:
        payload = json.dumps(run(admin_dsn), indent=2) + "\n"
    except Exception as exc:
        # Avoid printing HTTP/Pydantic/DB internals, which could contain secrets.
        detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        raise SystemExit(f"Agent Internet Live demo failed: {detail}") from None
    if args.evidence:
        args.evidence.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
