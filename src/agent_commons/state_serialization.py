import json

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
