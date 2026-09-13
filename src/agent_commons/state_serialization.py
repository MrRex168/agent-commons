import json
from dataclasses import dataclass

from pydantic import ValidationError

from agent_commons.schemas import PortableAgentState

STATE_ENVELOPE_DOMAIN_V1 = "agent-commons/signed-state/v1"
STATE_ENVELOPE_DOMAIN_V2 = "agent-commons/signed-state/v2"


@dataclass(frozen=True)
class ParsedStatePayload:
    fingerprint: str
    state: PortableAgentState
    state_sequence: int | None
    version: int


def canonical_state_payload(
    state: PortableAgentState,
    fingerprint: str,
    state_sequence: int | None = None,
) -> str:
    """Return deterministic UTF-8 JSON text for portable-state approval."""
    if state_sequence is None:
        document = {
            "fingerprint": fingerprint,
            "format": "agent-commons-signed-state",
            "state": state.model_dump(mode="json"),
            "version": 1,
        }
        domain = STATE_ENVELOPE_DOMAIN_V1
    else:
        if state_sequence < 1:
            raise ValueError("State sequence must be at least 1")
        document = {
            "fingerprint": fingerprint,
            "format": "agent-commons-signed-state",
            "state": state.model_dump(mode="json"),
            "state_sequence": state_sequence,
            "version": 2,
        }
        domain = STATE_ENVELOPE_DOMAIN_V2

    canonical = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{domain}\n{canonical}"


def parse_state_payload_details(payload: str) -> ParsedStatePayload:
    """Parse a signed state payload while preserving its freshness metadata."""
    if payload.startswith(f"{STATE_ENVELOPE_DOMAIN_V1}\n"):
        domain = STATE_ENVELOPE_DOMAIN_V1
        version = 1
    elif payload.startswith(f"{STATE_ENVELOPE_DOMAIN_V2}\n"):
        domain = STATE_ENVELOPE_DOMAIN_V2
        version = 2
    else:
        raise ValueError("Invalid signed state payload domain")

    prefix = f"{domain}\n"
    try:
        document = json.loads(payload[len(prefix) :])
    except json.JSONDecodeError as exc:
        raise ValueError("Signed state payload is not valid JSON") from exc

    if not isinstance(document, dict):
        raise ValueError("Signed state payload must contain a JSON object")

    expected_fields = {"fingerprint", "format", "state", "version"}
    if version == 2:
        expected_fields.add("state_sequence")
    if set(document) != expected_fields:
        raise ValueError("Signed state payload contains unsupported fields")
    if document["format"] != "agent-commons-signed-state" or document["version"] != version:
        raise ValueError("Unsupported signed state format or version")

    fingerprint = document["fingerprint"]
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("Signed state payload fingerprint is invalid")

    state_sequence: int | None = None
    if version == 2:
        state_sequence = document["state_sequence"]
        if not isinstance(state_sequence, int) or isinstance(state_sequence, bool) or state_sequence < 1:
            raise ValueError("Signed state sequence is invalid")

    try:
        state = PortableAgentState.model_validate(document["state"])
    except ValidationError as exc:
        raise ValueError("Signed state payload contains invalid portable state") from exc

    expected = canonical_state_payload(state, fingerprint, state_sequence)
    if payload != expected:
        raise ValueError("Signed state payload is not canonically serialized")

    return ParsedStatePayload(
        fingerprint=fingerprint,
        state=state,
        state_sequence=state_sequence,
        version=version,
    )


def parse_state_payload(payload: str) -> tuple[str, PortableAgentState]:
    """Backward-compatible parser returning the original two-value tuple."""
    parsed = parse_state_payload_details(payload)
    return parsed.fingerprint, parsed.state
