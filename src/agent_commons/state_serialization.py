import json

from pydantic import ValidationError

from agent_commons.schemas import PortableAgentState

STATE_ENVELOPE_DOMAIN = "agent-commons/signed-state/v1"


def canonical_state_payload(state: PortableAgentState, fingerprint: str) -> str:
    """Return deterministic UTF-8 JSON text for portable-state approval."""
    document = {
        "fingerprint": fingerprint,
        "format": "agent-commons-signed-state",
        "state": state.model_dump(mode="json"),
        "version": 1,
    }
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{STATE_ENVELOPE_DOMAIN}\n{canonical}"


def parse_state_payload(payload: str) -> tuple[str, PortableAgentState]:
    """Parse the exact signed payload and validate the embedded state package."""
    prefix = f"{STATE_ENVELOPE_DOMAIN}\n"
    if not payload.startswith(prefix):
        raise ValueError("Invalid signed state payload domain")

    try:
        document = json.loads(payload[len(prefix) :])
    except json.JSONDecodeError as exc:
        raise ValueError("Signed state payload is not valid JSON") from exc

    if not isinstance(document, dict):
        raise ValueError("Signed state payload must contain a JSON object")
    if set(document) != {"fingerprint", "format", "state", "version"}:
        raise ValueError("Signed state payload contains unsupported fields")
    if document["format"] != "agent-commons-signed-state" or document["version"] != 1:
        raise ValueError("Unsupported signed state format or version")
    fingerprint = document["fingerprint"]
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("Signed state payload fingerprint is invalid")

    try:
        state = PortableAgentState.model_validate(document["state"])
    except ValidationError as exc:
        raise ValueError("Signed state payload contains invalid portable state") from exc

    expected = canonical_state_payload(state, fingerprint)
    if payload != expected:
        raise ValueError("Signed state payload is not canonically serialized")

    return fingerprint, state
