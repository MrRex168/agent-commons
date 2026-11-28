"""Controller-signed authorization for the primary sovereign A2A location."""

import hashlib
import json
from datetime import UTC, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from agent_commons.identity_crypto import verify_identity_signature

SOVEREIGN_ENDPOINT_EXTENSION_URI = "urn:agent-commons:extension:sovereign-identity:v2"
NATIVE_ENDPOINT_EXTENSION_URI = "urn:agent-commons:extension:native-interface:v1"
ENDPOINT_AUTHORIZATION_DOMAIN = "agent-commons:sovereign-endpoint:v1"
MAX_VALIDITY_SECONDS = 7 * 24 * 60 * 60
FUTURE_TOLERANCE_SECONDS = 5 * 60
MAX_IDENTITY_SEQUENCE = 2**31 - 1
# Keep JSON integers exactly representable by common non-Python signers.
MAX_LOCATION_SEQUENCE = 2**53 - 1


def canonical_endpoint_url(value: str) -> str:
    url = HttpUrl(value)
    if url.scheme != "https" or url.username is not None or url.password is not None:
        raise ValueError("Sovereign endpoint URLs must use HTTPS without credentials")
    if url.fragment is not None:
        raise ValueError("Sovereign endpoint URLs must omit fragments")
    canonical = str(url)
    if len(canonical) > 500:
        raise ValueError("Sovereign endpoint URL is too long")
    return canonical


class EndpointAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    signing_domain: ClassVar[str] = ENDPOINT_AUTHORIZATION_DOMAIN

    version: int = Field(ge=1, le=1)
    root_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    controller_public_key_multibase: str = Field(min_length=2, max_length=128)
    identity_sequence: int = Field(ge=0, le=MAX_IDENTITY_SEQUENCE)
    location_sequence: int = Field(ge=1, le=MAX_LOCATION_SEQUENCE)
    card_url: str = Field(max_length=500)
    a2a_url: str = Field(max_length=500)
    transport: Literal["JSONRPC", "GRPC", "HTTP+JSON"]
    issued_at: int = Field(ge=0, le=253402300799)
    expires_at: int = Field(ge=0, le=253402300799)

    def signing_payload(self) -> str:
        canonical = json.dumps(
            self.model_dump(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return f"{self.signing_domain}\n{canonical}"

    def digest(self) -> str:
        return hashlib.sha256(self.signing_payload().encode("utf-8")).hexdigest()

    def expiration(self) -> datetime:
        return datetime.fromtimestamp(self.expires_at, UTC)

    def verify_time(self, now: datetime) -> None:
        if not 0 < self.expires_at - self.issued_at <= MAX_VALIDITY_SECONDS:
            raise ValueError("Invalid endpoint authorization validity period")
        if self.issued_at > now.timestamp() + FUTURE_TOLERANCE_SECONDS:
            raise ValueError("Endpoint authorization was issued too far in the future")
        if self.expiration() <= now:
            raise ValueError("Endpoint authorization has expired")


class SignedEndpointAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    authorization: EndpointAuthorization
    signature_multibase: str = Field(min_length=2, max_length=256)

    def verify(
        self,
        *,
        root: str,
        controller: str,
        identity_sequence: int,
        card_url: str,
        a2a_url: str,
        transport: str,
        now: datetime,
    ) -> None:
        claim = self.authorization
        if (
            claim.root_fingerprint != root
            or claim.controller_public_key_multibase != controller
            or claim.identity_sequence != identity_sequence
        ):
            raise ValueError("Endpoint authorization does not match verified lineage")
        if (
            claim.card_url != canonical_endpoint_url(card_url)
            or claim.a2a_url != canonical_endpoint_url(a2a_url)
            or claim.transport != transport
        ):
            raise ValueError("Endpoint authorization does not match the Card location")
        claim.verify_time(now)
        if not verify_identity_signature(
            controller, claim.signing_payload(), self.signature_multibase
        ):
            raise ValueError("Invalid endpoint authorization signature")


class NativeEndpointAuthorization(EndpointAuthorization):
    signing_domain: ClassVar[str] = "agent-commons:native-interface:v1"

    transport: Literal["JSONRPC"]
    protocol_version: Literal["1.0"]
    legacy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SignedNativeEndpointAuthorization(SignedEndpointAuthorization):
    authorization: NativeEndpointAuthorization

    def verify_binding(self, legacy: EndpointAuthorization, *, card_url: str, now: datetime):
        self.verify(
            root=legacy.root_fingerprint,
            controller=legacy.controller_public_key_multibase,
            identity_sequence=legacy.identity_sequence,
            card_url=card_url,
            a2a_url=legacy.a2a_url,
            transport=legacy.transport,
            now=now,
        )
        claim = self.authorization
        if (
            claim.location_sequence != legacy.location_sequence
            or claim.legacy_digest != legacy.digest()
        ):
            raise ValueError("Native interface does not match the current sovereign location")
