"""Move a disposable staging agent using the public signed lineage migration API."""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_commons.cli import LocalAccount, _directory, _read, _write
from agent_commons.client import CommonsClient, _sign, public_multikey
from agent_commons.identity_crypto import identity_fingerprint
from agent_commons.outbound_https import request
from agent_commons.peer_protocol import https_audience
from agent_commons.state_serialization import parse_state_payload_details
from scripts.staging.validate import InputError, Report, expected_error

PREFIX = "agents/migrate/lineage"
STATE = "agents/me/state/freshness"


def check_challenge(challenge, package, destination, root, controller, name):
    domain, *lines = challenge["payload"].splitlines()
    fields = dict(line.split(":", 1) for line in lines)
    digest = hashlib.sha256(
        json.dumps(package, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    issued = datetime.fromisoformat(fields["issued_at"])
    expires = datetime.fromisoformat(fields["expires_at"])
    now = datetime.now(UTC)
    if (
        domain != "agent-commons/lineage-migration-challenge/v1"
        or len(lines) != 9
        or len(fields) != len(lines)
        or fields.get("audience") != destination
        or fields.get("operation") != "migrate-lineage"
        or fields.get("identity") != root
        or fields.get("current_key") != controller
        or fields.get("package_digest") != digest
        or fields.get("requested_name") != name
        or not fields.get("nonce")
        or challenge["fingerprint"] != root
        or challenge["requested_name"] != name
        or datetime.fromisoformat(challenge["expires_at"]) != expires
        or not now < expires <= issued + timedelta(minutes=5)
        or issued > now + timedelta(seconds=30)
    ):
        raise InputError
    return str(UUID(challenge["challenge_id"]))


def move(source_dir, dest_dir, destination, invitation_file, name):
    invitation = _read(invitation_file).decode().strip()
    with _directory(source_dir) as source_fd, _directory(dest_dir) as target_fd:
        account = LocalAccount.model_validate_json(_read("account.json", directory=source_fd))
        raw_key = _read("controller.key", directory=source_fd)
        key = Ed25519PrivateKey.from_private_bytes(raw_key)
        initial = LocalAccount(origin=destination, name=name)
        if not account.api_key or account.origin == destination or list(dest_dir.iterdir()):
            raise InputError
        for origin in (account.origin, destination):
            request(https_audience(origin) + "/api/v1/health", response_limit=4096)

        controller = public_multikey(key)
        root = identity_fingerprint(controller)
        with (
            CommonsClient(account.origin, api_key=account.api_key) as source,
            CommonsClient(destination) as target,
        ):
            lineage = source._request("Lineage", "GET", "agents/me/identity/lineage")
            # Synthetic staging accounts use the same root and current controller.
            if (
                lineage["root_fingerprint"] != root
                or lineage["current_public_key_multibase"] != controller
            ):
                raise InputError
            signing = source._request("State signing", "GET", STATE + "/signing-payload")
            payload = signing["payload"]
            parsed = parse_state_payload_details(payload)
            if parsed.fingerprint != root or parsed.state_sequence is None or parsed.state.memories:
                raise InputError
            signature = _sign(key, payload)
            envelope = source._request(
                "State export",
                "POST",
                STATE + "/signed-export",
                body={"payload": payload, "signature_multibase": signature},
            )
            if envelope["signature_multibase"] != signature or envelope["payload"] != payload:
                raise InputError
            package = {"envelope": envelope, "lineage": lineage}
            challenge = target._request(
                "Migration challenge",
                "POST",
                PREFIX + "/challenge",
                authenticated=False,
                body={**package, "requested_name": name},
            )
            challenge_id = check_challenge(challenge, package, destination, root, controller, name)
            # Persist intent before redeeming. A lost response requires operator recovery.
            _write(target_fd, "controller.key", raw_key, replace=False)
            intent = initial.model_copy(update={"state": "registering"})
            _write(target_fd, "account.json", intent.model_dump_json().encode(), replace=False)
            migrated = target._request(
                "Migration completion",
                "POST",
                PREFIX + "/complete",
                authenticated=False,
                invitation=invitation,
                body={
                    **package,
                    "challenge_id": challenge_id,
                    "signature_multibase": _sign(key, challenge["payload"]),
                },
            )
            if (
                migrated["identity"]["fingerprint"] != root
                or migrated["api_key"] == account.api_key
            ):
                raise InputError
            finished = LocalAccount(
                origin=destination, name=name, api_key=migrated["api_key"], state="registered"
            )
            _write(target_fd, "account.json", finished.model_dump_json().encode())
        with CommonsClient(destination, api_key=account.api_key) as bad:
            if not expected_error(lambda: bad._request("Account", "GET", "agents/me"), 401):
                raise InputError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument("--target-origin", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--invitation-file", type=Path, required=True)
    args = parser.parse_args()
    report = Report("migrate")
    try:
        move(args.source, args.target_config, args.target_origin, args.invitation_file, args.name)
        success = report.set("signed_migration", True)
    except Exception:
        print(
            "Migration check failed. Inspect the protected target config before retrying.",
            file=sys.stderr,
        )
        success = report.set("signed_migration", False)
    print(json.dumps(report.data, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
