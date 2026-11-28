"""Small synchronous client for the versioned Agent Commons REST API."""

import hashlib
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic, sleep
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import base58
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, Field, TypeAdapter

from agent_commons import a2a_native, outbound_https
from agent_commons.collaboration import SkillScopes, _unique_object, validate_typed_request
from agent_commons.endpoint_authorization import (
    NATIVE_ENDPOINT_EXTENSION_URI,
    SOVEREIGN_ENDPOINT_EXTENSION_URI,
    EndpointAuthorization,
    NativeEndpointAuthorization,
    SignedEndpointAuthorization,
    SignedNativeEndpointAuthorization,
    canonical_endpoint_url,
)
from agent_commons.identity_crypto import ED25519_MULTICODEC_PREFIX, identity_fingerprint
from agent_commons.outbound_a2a import BoundGrant, DestinationApproval, TaskStatistics, statistics
from agent_commons.peer_protocol import (
    CollaborationMethod,
    EnrollmentChallenge,
    IssuedGrant,
    Method,
    SkillEnrollmentChallenge,
    SkillIssuedGrant,
    https_audience,
)
from agent_commons.security import REGISTRATION_INVITATION_HEADER
from agent_commons.typed_client_models import (
    TERMINAL_STATES,
    CollaborationObservation,
    CollaborationPage,
    KnownPeerPage,
    NativeTaskPage,
    TypedTask,
    TypedTaskRef,
    decode_task,
)


class CommonsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class EndpointVerificationError(CommonsError):
    """The approved endpoint could not be verified for this call."""


class OutboundResponseError(CommonsError):
    """The peer returned an invalid or oversized response."""


class TypedTaskError(CommonsError):
    def __init__(self, reason: str, message: str, *, status: int | None = None):
        super().__init__(message, status)
        self.reason = reason


class HistoryWriteError(CommonsError):
    """The peer responded, but the caller's history write failed."""

    def __init__(self, task: TypedTask):
        super().__init__(
            "Remote task response verified, but local history persistence could not be confirmed"
        )
        self.task = task


def _remaining(end: float) -> float:
    remaining = end - monotonic()
    if remaining <= 0:
        raise TypedTaskError("polling_timeout", "Task polling deadline exceeded")
    return remaining


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
        end: float | None = None,
    ) -> dict:
        headers = {}
        if authenticated:
            if not self._key:
                raise CommonsError("Register an account before using this operation")
            headers["Authorization"] = f"Bearer {self._key}"
        if invitation is not None:
            headers[REGISTRATION_INVITATION_HEADER] = invitation
        try:
            if end is None:
                response = self._http.request(method, path, json=body, headers=headers)
            else:
                headers["Accept-Encoding"] = "identity"
                with self._http.stream(
                    method,
                    path,
                    json=body,
                    headers=headers,
                    timeout=min(2, _remaining(end)),
                ) as stream:
                    if not stream.is_success:
                        raise CommonsError(
                            f"{action} failed (HTTP {stream.status_code})", stream.status_code
                        )
                    if stream.headers.get("content-encoding", "identity") != "identity":
                        raise OutboundResponseError(f"{action} returned unsupported encoding")
                    raw = bytearray()
                    for chunk in stream.iter_bytes():
                        _remaining(end)
                        if len(raw) + len(chunk) > 1024 * 1024:
                            raise CommonsError(f"{action} returned an oversized response")
                        raw.extend(chunk)
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError
                    return result
        except (httpx.HTTPError, ValueError, RecursionError):
            if end is not None:
                _remaining(end)
            raise CommonsError(f"{action} could not contact the configured server") from None
        if not response.is_success:
            raise CommonsError(
                f"{action} failed (HTTP {response.status_code})", response.status_code
            )
        try:
            result = response.json()
        except ValueError:
            raise OutboundResponseError(f"{action} returned an invalid response") from None
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
        native_card_url: str | None = None,
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
        native_metadata = {}
        if native_card_url is not None:
            if (
                transport != "JSONRPC"
                or endpoint != self.origin + "/a2a"
                or not native_card_url.startswith(self.origin + "/api/v1/agents/")
                or not native_card_url.endswith("/agent-card/v1")
                or canonical_endpoint_url(native_card_url) != native_card_url
                or "?" in native_card_url
            ):
                raise CommonsError("Use the exact native Card and JSON-RPC task URL")
            native = NativeEndpointAuthorization.model_validate(
                {
                    **claim.model_dump(),
                    "card_url": native_card_url,
                    "legacy_digest": claim.digest(),
                    "protocol_version": "1.0",
                    "recipient_agent_id": profile["id"],
                }
            )
            native_metadata["nativeEndpointAuthorization"] = {
                "authorization": native.model_dump(),
                "signature_multibase": _sign(key, native.signing_payload()),
            }

        self._request(
            "Card publication",
            "PUT",
            "agents/me/profile",
            body={
                "metadata": {
                    **metadata,
                    "a2a": {
                        **{k: v for k, v in a2a.items() if k != "nativeEndpointAuthorization"},
                        **native_metadata,
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

    def enroll_peer(
        self,
        key: Ed25519PrivateKey,
        *,
        approval_id: UUID,
        caller_root: str,
        recipient_agent_id: UUID,
        scopes: list[Method],
    ) -> IssuedGrant:
        def exchange(action: str, path: str, body: dict) -> dict:
            return self._request(action, "POST", path, authenticated=False, body=body)

        return self._enroll(
            key,
            approval_id,
            caller_root,
            recipient_agent_id,
            scopes,
            https_audience(self.origin),
            exchange,
        )

    def _enroll(
        self,
        key: Ed25519PrivateKey,
        approval_id: UUID,
        caller_root: str,
        recipient_agent_id: UUID,
        scopes: list[CollaborationMethod],
        audience: str,
        exchange: Callable[[str, str, dict], dict],
        skills: list[str] | None = None,
    ) -> IssuedGrant:
        path = f"peers/{UUID(str(approval_id))}"
        fields = (audience, caller_root, recipient_agent_id, sorted(set(scopes)))
        allowed = {"message/send", "tasks/get"}
        if skills is not None:
            allowed.update(("tasks/cancel", "tasks/list", "a2a/1.0"))
        if not scopes or not set(scopes) <= allowed:
            raise CommonsError("Specify the expected task method scopes")
        if skills is not None:
            try:
                skills = sorted(TypeAdapter(SkillScopes).validate_python(skills))
                if len(set(skills)) != len(skills):
                    raise ValueError
            except ValueError:
                raise CommonsError("Specify distinct versioned skill scopes") from None
        response = exchange("Peer challenge", path + "/challenge", {})
        try:
            challenge = (
                EnrollmentChallenge.model_validate(response)
                if skills is None
                else SkillEnrollmentChallenge.model_validate(response)
            )
            if (
                isinstance(challenge, SkillEnrollmentChallenge)
                and sorted(challenge.skills) != skills
            ):
                raise ValueError
            now = datetime.now(UTC)
            if (
                challenge.approval_id != approval_id
                or challenge.controller != public_multikey(key)
                or (
                    challenge.audience,
                    challenge.caller_root,
                    challenge.recipient_agent_id,
                    sorted(challenge.scopes),
                )
                != fields
                or not now < challenge.expires_at <= now + timedelta(minutes=5, seconds=30)
            ):
                raise ValueError
        except ValueError:
            raise CommonsError("Peer challenge did not match the expected approval") from None
        response = exchange(
            "Peer redemption",
            path + "/redeem",
            {
                "challenge_id": str(challenge.challenge_id),
                "signature_multibase": _sign(key, challenge.signing_payload()),
            },
        )
        try:
            grant = (
                IssuedGrant.model_validate(response)
                if skills is None
                else SkillIssuedGrant.model_validate(response)
            )
            if isinstance(grant, SkillIssuedGrant) and sorted(grant.skills) != skills:
                raise ValueError
            pattern = r"a2a_[A-Za-z0-9_-]{43}" if skills is None else r"a2c_[A-Za-z0-9_-]{43}"
            if (
                (grant.audience, grant.caller_root, grant.recipient_agent_id, sorted(grant.scopes))
                != fields
                or not re.fullmatch(pattern, grant.token.get_secret_value())
                or not now < grant.expires_at <= datetime.now(UTC) + timedelta(hours=24, seconds=30)
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise CommonsError("Peer redemption returned an invalid grant") from None
        return grant

    def _verified_destination(
        self, approved: DestinationApproval, *, end: float | None = None
    ) -> EndpointAuthorization:
        try:
            result = self._request(
                "Peer refresh",
                "POST",
                f"agents/remote/{approved.reference_id}/refresh",
                **({"end": end} if end is not None else {}),
            )
            card = result["card_snapshot"]
            if (
                not result["identity_verified"]
                or not result["lineage_verified"]
                or result["endpoint_authorization_status"] != "authorized"
                or UUID(result["id"]) != approved.reference_id
                or result["root_fingerprint"] != approved.peer_root
                or result["current_controller_public_key"] != approved.controller
                or result["a2a_url"] != approved.endpoint
                or result["preferred_transport"] != approved.transport
                or card["protocolVersion"] != "0.3.0"
            ):
                raise ValueError
            (params,) = [
                ext["params"]
                for ext in card["capabilities"]["extensions"]
                if ext["uri"] == SOVEREIGN_ENDPOINT_EXTENSION_URI
            ]
            signed = SignedEndpointAuthorization.model_validate(params["endpointAuthorization"])
            signed.verify(
                root=approved.peer_root,
                controller=approved.controller,
                identity_sequence=result["identity_sequence"],
                card_url=result["card_url"],
                a2a_url=approved.endpoint,
                transport=approved.transport,
                now=datetime.now(UTC),
            )
        except TypedTaskError:
            raise
        except (KeyError, TypeError, ValueError, CommonsError) as exc:
            raise EndpointVerificationError(
                "Approved sovereign destination could not be verified. Check the Card, "
                "controller, and current location",
                getattr(exc, "status", None),
            ) from None
        return signed.authorization

    def _verify_native_destination(
        self,
        approved: DestinationApproval,
        current: EndpointAuthorization,
        *,
        end: float | None = None,
    ) -> NativeEndpointAuthorization:
        url = approved.native_card_url
        if url is None:
            raise EndpointVerificationError("No native Card was approved")
        expiry = current.expiration()
        if end is not None:
            expiry = min(expiry, datetime.now(UTC) + timedelta(seconds=_remaining(end)))
        try:
            raw = outbound_https.request(url, response_limit=512 * 1024, valid_until=expiry)
            card = json.loads(raw, object_pairs_hook=_unique_object)
            if card["supportedInterfaces"] != [
                {"url": approved.endpoint, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ]:
                raise ValueError
            (params,) = [
                extension["params"]
                for extension in card["capabilities"]["extensions"]
                if extension["uri"] == NATIVE_ENDPOINT_EXTENSION_URI
            ]
            proof = SignedNativeEndpointAuthorization.model_validate(
                params["endpointAuthorization"]
            )
            proof.verify_binding(
                current,
                card_url=url,
                recipient=str(approved.recipient_agent_id),
                now=datetime.now(UTC),
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            outbound_https.TransferError,
        ) as exc:
            raise EndpointVerificationError(
                "Approved native interface could not be verified", getattr(exc, "status", None)
            ) from None
        return proof.authorization

    def enroll_dispatch_peer(
        self,
        key: Ed25519PrivateKey,
        *,
        destination: DestinationApproval,
        approval_id: UUID,
        caller_root: str,
        scopes: list[CollaborationMethod],
        skills: list[str] | None = None,
    ) -> BoundGrant:
        """Verify a caller-approved reference and redeem a fresh, endpoint-bound credential."""
        claim = self._verified_destination(destination)
        if destination.native_card_url is not None:
            if skills is None or "a2a/1.0" not in scopes:
                raise CommonsError("Native dispatch requires explicit typed 1.0 enrollment")
            self._verify_native_destination(destination, claim)
        audience = destination.audience

        def exchange(action: str, path: str, body: dict) -> dict:
            return _outbound_json(action, audience + "/api/v1/" + path, body)

        grant = self._enroll(
            key,
            approval_id,
            caller_root,
            destination.recipient_agent_id,
            scopes,
            audience,
            exchange,
            skills,
        )
        return BoundGrant(
            destination=destination,
            home_origin=self.origin,
            grant=grant,
            expires_at=grant.expires_at,
            identity_sequence=claim.identity_sequence,
            location_sequence=claim.location_sequence,
        )

    def send_task(self, grant: BoundGrant, text: str, *, message_id: str) -> TaskStatistics:
        try:
            valid_text = len(text.encode("utf-8")) <= 4096
        except UnicodeEncodeError:
            valid_text = False
        if not valid_text or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", message_id):
            raise CommonsError("Send requires up to 4096 UTF-8 bytes and a valid message ID")
        return self._task(
            grant,
            "message/send",
            {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": message_id,
                    "parts": [{"kind": "text", "text": text}],
                },
            },
        )

    def get_task(self, grant: BoundGrant, task_id: UUID) -> TaskStatistics:
        task = self._task(grant, "tasks/get", {"id": str(task_id)})
        if task.id != task_id:
            raise CommonsError("A2A server returned a different task ID")
        return task

    def _task(self, grant: BoundGrant, method: Method, params: dict) -> TaskStatistics:
        if isinstance(grant.grant, SkillIssuedGrant):
            raise CommonsError("Typed collaboration execution is unavailable")
        claim = self._dispatch_authorization(grant, method)
        request_id = str(uuid4())
        response = _outbound_json(
            "A2A task request",
            grant.destination.endpoint,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            bearer=grant.grant.token.get_secret_value(),
            valid_until=min(grant.expires_at, claim.expiration()),
        )
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise CommonsError("A2A server returned an invalid response")
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if code == -32001:
                raise CommonsError("Task not found")
            if code == -32010:
                raise CommonsError("Message ID was already used with different text")
            raise CommonsError("A2A server rejected the task request")
        try:
            return statistics(response["result"])
        except (KeyError, TypeError, ValueError):
            raise CommonsError("A2A server returned an invalid task result") from None

    def _dispatch_authorization(
        self, grant: BoundGrant, method: CollaborationMethod, *, end: float | None = None
    ) -> EndpointAuthorization:
        if grant.home_origin != self.origin:
            raise CommonsError("Credential belongs to a different Agent Commons home server")
        if method not in grant.grant.scopes:
            raise CommonsError("Peer credential does not permit this A2A method")
        if grant.expires_at <= datetime.now(UTC):
            raise CommonsError("Peer credential has expired")
        claim = (
            self._verified_destination(grant.destination)
            if end is None
            else self._verified_destination(grant.destination, end=end)
        )
        if (
            claim.identity_sequence != grant.identity_sequence
            or claim.location_sequence != grant.location_sequence
        ):
            raise EndpointVerificationError(
                "Peer location or identity sequence conflicts with enrollment"
            )
        if grant.expires_at <= datetime.now(UTC):
            raise CommonsError("Peer credential has expired")
        return claim

    def discover_known(
        self, *, skill: str, verified_only: bool = True, cursor: str | None = None
    ) -> KnownPeerPage:
        params = {"skill_exact": skill, "verified_only": str(verified_only).lower()}
        if cursor is not None:
            params["cursor"] = cursor
        response = self._request(
            "Known-peer discovery", "GET", "agents/discovery/remote?" + urlencode(params)
        )
        try:
            return KnownPeerPage.model_validate(response)
        except ValueError:
            raise OutboundResponseError("Discovery returned invalid records") from None

    def delegate(
        self, *, grant: BoundGrant, skill: str, data: dict, message_id: str, deadline: datetime
    ) -> TypedTask:
        try:
            argument = {
                "skill": skill,
                "input": data,
                "message_id": message_id,
                "deadline": deadline.isoformat(),
            }
            validate_typed_request(
                json.dumps(argument, ensure_ascii=False).encode(),
                now=datetime.now(UTC),
                check_deadline=False,
            )
        except (AttributeError, TypeError, ValueError, RecursionError):
            raise TypedTaskError("invalid_input", "Invalid typed task input") from None
        params = {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": message_id,
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "skill": skill,
                            "input": data,
                            "deadline": argument["deadline"],
                        },
                    }
                ],
            }
        }
        return self._typed_task(grant, "message/send", params, skill=skill)

    def get_task_status(self, task: TypedTaskRef, *, grant: BoundGrant) -> TypedTask:
        return self._task_for_reference(task, grant, "tasks/get")

    def cancel_task(self, task: TypedTaskRef, *, grant: BoundGrant) -> TypedTask:
        return self._task_for_reference(task, grant, "tasks/cancel")

    def wait_for_task(
        self, task: TypedTaskRef, *, grant: BoundGrant, timeout: float = 30, interval: float = 7
    ) -> TypedTask:
        if not (math.isfinite(timeout) and 0 < timeout <= 120 and 1 <= interval <= 30):
            raise ValueError("Use a polling timeout up to 120 seconds and interval of 1–30 seconds")
        end = monotonic() + timeout
        while True:
            _remaining(end)
            result = self._task_for_reference(task, grant, "tasks/get", end=end)
            if result.status in TERMINAL_STATES:
                return result
            sleep(min(interval, _remaining(end)))

    def list_collaboration_history(self, *, cursor: UUID | None = None) -> CollaborationPage:
        path = "agents/collaboration/history"
        if cursor is not None:
            path += "?" + urlencode({"cursor": str(cursor)})
        response = self._request("Collaboration history", "GET", path)
        try:
            return CollaborationPage.model_validate(response)
        except ValueError:
            raise OutboundResponseError("History returned invalid records") from None

    def _task_for_reference(
        self,
        task: TypedTaskRef,
        grant: BoundGrant,
        method: CollaborationMethod,
        *,
        end: float | None = None,
    ) -> TypedTask:
        if (
            task.peer_root != grant.destination.peer_root
            or task.endpoint != grant.destination.endpoint
            or task.identity_sequence != grant.identity_sequence
            or task.location_sequence != grant.location_sequence
        ):
            raise EndpointVerificationError("Task belongs to a different execution location")
        return self._typed_task(
            grant, method, {"id": str(task.id)}, skill=task.skill, reference=task, end=end
        )

    def _typed_rpc(
        self,
        grant: BoundGrant,
        method: CollaborationMethod,
        params: dict,
        *,
        end: float | None = None,
    ) -> tuple[EndpointAuthorization, dict]:
        if (
            not isinstance(grant.grant, SkillIssuedGrant)
            or method not in grant.grant.scopes
            or self.origin != grant.home_origin
        ):
            raise TypedTaskError("scope_denied", "Typed grant does not authorize this request")
        if grant.expires_at <= datetime.now(UTC):
            raise TypedTaskError("credential_unavailable", "Peer credential has expired")
        end = min(end, monotonic() + 20) if end is not None else monotonic() + 20
        try:
            identity = self._request("Caller identity", "GET", "agents/me/identity", end=end)
        except TypedTaskError:
            raise
        except CommonsError as exc:
            raise TypedTaskError(
                "home_unavailable",
                "Could not verify the caller at its home server",
                status=exc.status,
            ) from None
        if identity.get("fingerprint") != grant.grant.caller_root:
            raise TypedTaskError("scope_denied", "Local account does not own the caller identity")
        try:
            claim = self._dispatch_authorization(grant, method, end=end)
        except EndpointVerificationError as exc:
            if exc.status == 429:
                raise TypedTaskError(
                    "rate_limited", "Home resolver quota exceeded", status=429
                ) from None
            raise
        expiry = min(grant.expires_at, claim.expiration())
        native = grant.destination.native_card_url is not None
        if native:
            if "a2a/1.0" not in grant.grant.scopes:
                raise TypedTaskError("scope_denied", "Native dispatch requires a 1.0 grant")
            binding = self._verify_native_destination(grant.destination, claim, end=end)
            expiry = min(expiry, binding.expiration())
        expiry = min(expiry, datetime.now(UTC) + timedelta(seconds=_remaining(end)))
        request_id = str(uuid4())
        try:
            reply = _outbound_json(
                "Typed task request",
                grant.destination.endpoint,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": a2a_native.WIRE_METHODS[method] if native else method,
                    "params": a2a_native.encode_request(method, params) if native else params,
                },
                bearer=grant.grant.token.get_secret_value(),
                valid_until=expiry,
                **({"native": True} if native else {}),
            )
        except OutboundResponseError:
            raise
        except CommonsError as exc:
            if end is not None:
                _remaining(end)
            if exc.status in (401, 403):
                raise TypedTaskError(
                    "credential_unavailable" if exc.status == 401 else "scope_denied",
                    "Peer denied the credential or method",
                    status=exc.status,
                ) from None
            if exc.status == 429:
                raise TypedTaskError(
                    "rate_limited", "Peer task quota exceeded", status=429
                ) from None
            raise TypedTaskError(
                "request_uncertain",
                "Peer request failed. Retry a submission with its original ID and body",
                status=exc.status,
            ) from None
        if reply.get("jsonrpc") != "2.0" or reply.get("id") != request_id:
            raise OutboundResponseError("Peer returned an invalid RPC envelope")
        if "error" in reply:
            error = reply["error"]
            if not isinstance(error, dict) or "result" in reply:
                raise OutboundResponseError("Peer returned an invalid RPC error")
            code = error.get("code")
            if type(code) is not int:
                raise OutboundResponseError("Peer returned an invalid RPC error")
            reason, message = {
                -32001: ("task_missing", "Task is unknown or expired at its execution server"),
                -32002: ("not_cancelable", "Task cannot be canceled in its current state"),
                -32010: ("message_conflict", "Message ID was used for a different request"),
            }.get(code, ("rejected", "Peer declined the typed task request"))
            raise TypedTaskError(reason, message)
        if "result" not in reply:
            raise OutboundResponseError("Peer returned an invalid RPC result")
        return claim, reply

    def list_native_tasks(
        self, grant: BoundGrant, *, page_size: int = 20, cursor: UUID | None = None
    ) -> NativeTaskPage:
        if grant.destination.native_card_url is None:
            raise EndpointVerificationError("Task listing requires an approved native Card")
        if type(page_size) is not int or not 1 <= page_size <= 20:
            raise ValueError("Use a task page size between 1 and 20")
        params = {"pageSize": page_size}
        if cursor is not None:
            params["pageToken"] = str(cursor)
        _, reply = self._typed_rpc(grant, "tasks/list", params)
        try:
            page = NativeTaskPage.model_validate(reply["result"])
            if page.pageSize != len(page.tasks) or page.pageSize > page_size:
                raise ValueError
        except (TypeError, ValueError):
            raise OutboundResponseError("Peer returned an invalid native task page") from None
        return page

    def _typed_task(
        self,
        grant: BoundGrant,
        method: CollaborationMethod,
        params: dict,
        *,
        skill: str,
        reference: TypedTaskRef | None = None,
        end: float | None = None,
    ) -> TypedTask:
        if not isinstance(grant.grant, SkillIssuedGrant) or skill not in grant.grant.skills:
            raise TypedTaskError("scope_denied", "Typed grant does not authorize this request")
        end = min(end, monotonic() + 20) if end is not None else monotonic() + 20
        claim, reply = self._typed_rpc(grant, method, params, end=end)
        native = grant.destination.native_card_url is not None
        try:
            result = reply["result"]
            if native:
                result = a2a_native.decode_result(method, result, skill=skill)
            identifier, state, artifact = decode_task(result, skill=skill)
            if reference is not None and identifier != reference.id:
                raise ValueError
            task = TypedTask(
                reference=reference
                or TypedTaskRef(
                    id=identifier,
                    skill=skill,
                    peer_root=grant.destination.peer_root,
                    endpoint=grant.destination.endpoint,
                    identity_sequence=claim.identity_sequence,
                    location_sequence=claim.location_sequence,
                ),
                status=state,
                artifact=artifact,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise OutboundResponseError("Peer returned an invalid typed task or artifact") from None
        observation = CollaborationObservation(
            reference_id=grant.destination.reference_id,
            caller_root=grant.grant.caller_root,
            task=task.reference,
            status=task.status,
            artifact_digest=hashlib.sha256(
                json.dumps(
                    artifact,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if artifact is not None
            else None,
        )
        try:
            self._request(
                "Collaboration history",
                "POST",
                "agents/collaboration/history",
                body=observation.model_dump(mode="json"),
                # A verified response still needs recording when the poll budget is spent.
                end=monotonic() + 2,
            )
        except CommonsError:
            raise HistoryWriteError(task) from None
        return task

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


def _outbound_json(
    action: str,
    url: str,
    body: dict,
    *,
    bearer: str | None = None,
    valid_until: datetime | None = None,
    native: bool = False,
) -> dict:
    try:
        raw = outbound_https.request(
            url,
            response_limit=16_384,
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            bearer=bearer,
            valid_until=valid_until,
            **({"native": True} if native else {}),
        )
        result = json.loads(
            raw, object_pairs_hook=_unique_object, parse_float=Decimal if native else None
        )
        if not isinstance(result, dict):
            raise ValueError
        return result
    except outbound_https.DestinationError:
        raise CommonsError(f"{action} refused an unsafe HTTPS destination") from None
    except outbound_https.UnexpectedStatus as exc:
        reason = {
            401: "credential expired, revoked, or unrecognized",
            403: "scope or recipient denied",
        }.get(exc.status, f"HTTP {exc.status}")
        raise CommonsError(f"{action} failed ({reason})", exc.status) from None
    except outbound_https.ResponseTooLarge:
        raise OutboundResponseError(f"{action} returned an oversized response") from None
    except outbound_https.TransferError as exc:
        raise CommonsError(f"{action} failed. {exc}") from None
    except (ValueError, RecursionError, InvalidOperation):
        raise OutboundResponseError(f"{action} returned an invalid response") from None
