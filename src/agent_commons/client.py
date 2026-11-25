"""Small synchronous client for the versioned Agent Commons REST API."""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import base58
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, Field

from agent_commons.endpoint_authorization import EndpointAuthorization, canonical_endpoint_url
from agent_commons.identity_crypto import ED25519_MULTICODEC_PREFIX, identity_fingerprint
from agent_commons.security import REGISTRATION_INVITATION_HEADER


class CommonsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PeerVerification(BaseModel):
    id: UUID
    root_fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    identity_sequence: int | None
    lineage_verified: bool
    identity_verified: bool
    endpoint_authorization_status: Literal["authorized", "pending", "expired", "non_sovereign"]


def server_origin(value: str) -> str:
    raw = urlsplit(value)
    url = httpx.URL(value)
    if (
        not url.host
        or raw.username is not None
        or raw.password is not None
        or "?" in value
        or "#" in value
        or url.path.rstrip("/") not in ("", "/api/v1")
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.host in ("127.0.0.1", "::1", "localhost"))
        )
    ):
        raise ValueError("Use an HTTPS server origin or a local loopback development origin")
    return str(url.copy_with(path="/")).rstrip("/")


def public_multikey(key: Ed25519PrivateKey) -> str:
    return "z" + base58.b58encode(
        ED25519_MULTICODEC_PREFIX + key.public_key().public_bytes_raw()
    ).decode("ascii")


def _sign(key: Ed25519PrivateKey, payload: str) -> str:
    return "z" + base58.b58encode(key.sign(payload.encode("utf-8"))).decode("ascii")


class CommonsClient:
    """Use a context manager or call close() when done."""

    def __init__(
        self,
        origin: str,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.origin = server_origin(origin)
        self._key = api_key
        self._http = httpx.Client(
            base_url=self.origin + "/api/v1/",
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=15,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        action: str,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        authenticated: bool = True,
        invitation: str | None = None,
    ) -> dict:
        headers = {}
        if authenticated:
            if not self._key:
                raise CommonsError("Register an account before using this operation")
            headers["Authorization"] = f"Bearer {self._key}"
        if invitation is not None:
            headers[REGISTRATION_INVITATION_HEADER] = invitation
        try:
            response = self._http.request(method, path, json=body, headers=headers)
        except (httpx.HTTPError, ValueError):
            raise CommonsError(f"{action} could not contact the configured server") from None
        if not response.is_success:
            raise CommonsError(
                f"{action} failed (HTTP {response.status_code})", response.status_code
            )
        try:
            result = response.json()
        except ValueError:
            raise CommonsError(f"{action} returned an invalid response") from None
        if not isinstance(result, dict):
            raise CommonsError(f"{action} returned an invalid response")
        return result

    def register(self, name: str, *, invitation: str | None = None) -> str:
        """Return the one-time local API key. The caller is responsible for storing it."""
        result = self._request(
            "Registration",
            "POST",
            "agents/register",
            body={"name": name},
            authenticated=False,
            invitation=invitation,
        )
        api_key = result.get("api_key")
        if not isinstance(api_key, str) or not re.fullmatch(r"ac_[A-Za-z0-9_-]{43}", api_key):
            raise CommonsError("Registration returned an invalid API key")
        self._key = api_key
        return api_key

    def bind_identity(self, key: Ed25519PrivateKey) -> str:
        """Bind a local controller key, or confirm the same identity is already bound."""
        public_key = public_multikey(key)
        root = identity_fingerprint(public_key)
        try:
            identity = self._request("Identity inspection", "GET", "agents/me/identity")
        except CommonsError as exc:
            if exc.status != 404:
                raise
        else:
            if (
                identity.get("fingerprint") != root
                or identity.get("public_key_multibase") != public_key
            ):
                raise CommonsError("The account already uses a different sovereign root")
            return root

        challenge = self._request(
            "Identity challenge",
            "POST",
            "agents/me/identity/challenge",
            body={"public_key_multibase": public_key},
        )
        try:
            payload = challenge["payload"]
            domain, audience, operation, nonce, identity, issued, expires = payload.split("\n")
            issued_at = datetime.fromisoformat(issued.removeprefix("issued_at:"))
            expires_at = datetime.fromisoformat(expires.removeprefix("expires_at:"))
            now = datetime.now(UTC)
            if (
                domain != "agent-commons/identity-challenge/v1"
                or audience != f"audience:{self.origin}"
                or operation != "operation:bind"
                or identity != f"identity:{root}"
                or challenge["fingerprint"] != root
                or not re.fullmatch(r"nonce:[A-Za-z0-9_-]{43}", nonce)
                or not issued.startswith("issued_at:")
                or not expires.startswith("expires_at:")
                or not timedelta(0) < expires_at - issued_at <= timedelta(minutes=5)
                or issued_at > now + timedelta(minutes=1)
                or expires_at <= now
                or expires_at != datetime.fromisoformat(challenge["expires_at"])
            ):
                raise ValueError
            challenge_id = str(UUID(challenge["challenge_id"]))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise CommonsError(
                "Identity challenge did not match the expected audience and key"
            ) from None
        result = self._request(
            "Identity verification",
            "POST",
            "agents/me/identity/verify",
            body={"challenge_id": challenge_id, "signature_multibase": _sign(key, payload)},
        )
        if result.get("fingerprint") != root or result.get("public_key_multibase") != public_key:
            raise CommonsError("Identity verification did not confirm the expected root")
        return root

    def update_profile(
        self, *, description: str | None = None, capabilities: list[str] | None = None
    ):
        body = {"description": description, "capabilities": capabilities}
        self._request(
            "Profile update",
            "PUT",
            "agents/me/profile",
            body={key: value for key, value in body.items() if value is not None},
        )

    def publish_card(
        self,
        key: Ed25519PrivateKey,
        *,
        card_url: str,
        endpoint: str,
        location_sequence: int,
        transport: Literal["JSONRPC", "GRPC", "HTTP+JSON"] = "JSONRPC",
    ) -> str:
        card_url = canonical_endpoint_url(card_url)
        endpoint = canonical_endpoint_url(endpoint)
        lineage = self._request("Lineage inspection", "GET", "agents/me/identity/lineage")
        try:
            controller = public_multikey(key)
            root = identity_fingerprint(lineage["root_public_key_multibase"])
            if (
                root != lineage["root_fingerprint"]
                or controller != lineage["current_public_key_multibase"]
            ):
                raise ValueError
            identity_sequence = lineage["sequence"]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise CommonsError(
                "The local key does not match the account's current controller"
            ) from None

        now = int(datetime.now(UTC).timestamp())
        claim = EndpointAuthorization(
            version=1,
            root_fingerprint=root,
            controller_public_key_multibase=controller,
            identity_sequence=identity_sequence,
            location_sequence=location_sequence,
            card_url=card_url,
            a2a_url=endpoint,
            transport=transport,
            issued_at=now,
            expires_at=now + 86400,
        )
        profile = self._request("Profile inspection", "GET", "agents/me/profile")
        metadata = profile.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("a2a", {}), dict):
            raise CommonsError("The current profile has invalid A2A metadata")
        a2a = metadata.get("a2a", {})
        previous = a2a.get("endpointAuthorization")
        if previous is not None:
            try:
                prior = EndpointAuthorization.model_validate(previous["authorization"])
            except (KeyError, TypeError, ValueError):
                raise CommonsError("The existing Card authorization is invalid") from None
            if (
                prior.identity_sequence == identity_sequence
                and location_sequence <= prior.location_sequence
            ):
                raise CommonsError(
                    "Use a location sequence greater than the existing authorization"
                )
        self._request(
            "Card publication",
            "PUT",
            "agents/me/profile",
            body={
                "metadata": {
                    **metadata,
                    "a2a": {
                        **a2a,
                        "url": endpoint,
                        "preferredTransport": transport,
                        "publishSovereignIdentity": True,
                        "endpointAuthorization": {
                            "authorization": claim.model_dump(),
                            "signature_multibase": _sign(key, claim.signing_payload()),
                        },
                    },
                }
            },
        )
        return root

    def resolve_peer(self, card_url: str) -> PeerVerification:
        result = self._request(
            "Peer resolution",
            "POST",
            "agents/remote/resolve",
            body={"agent_card_url": canonical_endpoint_url(card_url)},
        )
        return _peer_verification(result)

    def inspect_peer(self, reference_id: UUID) -> PeerVerification:
        return _peer_verification(
            self._request("Peer inspection", "GET", f"agents/remote/{UUID(str(reference_id))}")
        )


def _peer_verification(response: dict) -> PeerVerification:
    try:
        return PeerVerification.model_validate(response)
    except ValueError:
        raise CommonsError("Peer endpoint returned invalid verification fields") from None
