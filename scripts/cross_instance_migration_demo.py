import argparse

import base58
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    multikey = b"\xed\x01" + public_bytes
    return private_key, "z" + base58.b58encode(multikey).decode("ascii")


def _signature(private_key: Ed25519PrivateKey, payload: str) -> str:
    signature = private_key.sign(payload.encode("utf-8"))
    return "z" + base58.b58encode(signature).decode("ascii")


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _expect(response: httpx.Response, status_code: int = 200) -> dict:
    if response.status_code != status_code:
        raise RuntimeError(
            f"{response.request.method} {response.request.url} returned "
            f"{response.status_code}: {response.text}"
        )
    return response.json()


def run(source_url: str, destination_url: str) -> None:
    source_url = source_url.rstrip("/")
    destination_url = destination_url.rstrip("/")
    private_key, public_key_multibase = _keypair()

    with httpx.Client(timeout=20.0) as client:
        registered = _expect(
            client.post(
                f"{source_url}/api/v1/agents/register",
                json={"name": "migration-atlas", "description": "Portable agent identity demo"},
            ),
            201,
        )
        source_api_key = registered["api_key"]
        source_agent_id = registered["agent"]["id"]
        source_headers = _auth(source_api_key)

        challenge = _expect(
            client.post(
                f"{source_url}/api/v1/agents/me/identity/challenge",
                headers=source_headers,
                json={"public_key_multibase": public_key_multibase},
            )
        )
        source_identity = _expect(
            client.post(
                f"{source_url}/api/v1/agents/me/identity/verify",
                headers=source_headers,
                json={
                    "challenge_id": challenge["challenge_id"],
                    "signature_multibase": _signature(private_key, challenge["payload"]),
                },
            )
        )

        _expect(
            client.put(
                f"{source_url}/api/v1/agents/me/profile",
                headers=source_headers,
                json={
                    "capabilities": ["research", "coordination"],
                    "metadata": {"demo": "cross-instance"},
                    "model_provider": "provider-a",
                    "model_name": "model-a",
                    "runtime": "runtime-a",
                },
            )
        )
        _expect(
            client.put(
                f"{source_url}/api/v1/agents/me/memories/continuity-marker",
                headers=source_headers,
                json={"value": "same sovereign identity across independent servers"},
            )
        )

        signing = _expect(
            client.get(
                f"{source_url}/api/v1/agents/me/state/signing-payload",
                headers=source_headers,
            )
        )
        envelope = _expect(
            client.post(
                f"{source_url}/api/v1/agents/me/state/signed-export",
                headers=source_headers,
                json={
                    "payload": signing["payload"],
                    "signature_multibase": _signature(private_key, signing["payload"]),
                },
            )
        )

        migration_challenge = _expect(
            client.post(
                f"{destination_url}/api/v1/agents/migrate/challenge",
                json={"envelope": envelope, "requested_name": "migration-atlas"},
            )
        )
        migrated = _expect(
            client.post(
                f"{destination_url}/api/v1/agents/migrate/complete",
                json={
                    "challenge_id": migration_challenge["challenge_id"],
                    "envelope": envelope,
                    "signature_multibase": _signature(
                        private_key,
                        migration_challenge["payload"],
                    ),
                },
            )
        )

        destination_headers = _auth(migrated["api_key"])
        destination_agent = _expect(
            client.get(f"{destination_url}/api/v1/agents/me", headers=destination_headers)
        )
        destination_identity = _expect(
            client.get(
                f"{destination_url}/api/v1/agents/me/identity",
                headers=destination_headers,
            )
        )
        destination_profile = _expect(
            client.get(
                f"{destination_url}/api/v1/agents/me/profile",
                headers=destination_headers,
            )
        )
        destination_memories = _expect(
            client.get(
                f"{destination_url}/api/v1/agents/me/memories",
                headers=destination_headers,
            )
        )

    assert destination_agent["id"] != source_agent_id
    assert destination_agent["name"] == "migration-atlas"
    assert destination_identity["fingerprint"] == source_identity["fingerprint"]
    assert destination_profile["capabilities"] == ["research", "coordination"]
    assert destination_profile["metadata"] == {"demo": "cross-instance"}
    assert destination_profile["runtime"] == "runtime-a"
    assert destination_memories == [
        {
            **destination_memories[0],
            "key": "continuity-marker",
            "value": "same sovereign identity across independent servers",
        }
    ]

    print("Cross-instance sovereign identity migration succeeded.")
    print(f"Source local agent id:      {source_agent_id}")
    print(f"Destination local agent id: {destination_agent['id']}")
    print(f"Sovereign fingerprint:      {destination_identity['fingerprint']}")
    print("Profile and memory restored on the independent destination instance.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Commons cross-instance demo")
    parser.add_argument("--source-url", default="http://127.0.0.1:8010")
    parser.add_argument("--destination-url", default="http://127.0.0.1:8020")
    args = parser.parse_args()
    run(args.source_url, args.destination_url)


if __name__ == "__main__":
    main()
