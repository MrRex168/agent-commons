from datetime import UTC, datetime, timedelta

import pytest

from agent_commons.endpoint_authorization import EndpointAuthorization, canonical_endpoint_url

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ISSUED_AT = int(NOW.timestamp())


def _claim(**overrides) -> EndpointAuthorization:
    return EndpointAuthorization.model_validate(
        {
            "version": 1,
            "root_fingerprint": "sha256:" + "a" * 64,
            "controller_public_key_multibase": "zKey",
            "identity_sequence": 0,
            "location_sequence": 1,
            "card_url": "https://example.test/card?x=%2F",
            "a2a_url": "https://example.test/a2a",
            "transport": "JSONRPC",
            "issued_at": ISSUED_AT,
            "expires_at": ISSUED_AT + 604800,
            **overrides,
        }
    )


def test_canonical_signing_payload() -> None:
    assert _claim().signing_payload() == (
        'agent-commons:sovereign-endpoint:v1\n'
        '{"a2a_url":"https://example.test/a2a","card_url":"https://example.test/card?x=%2F",'
        '"controller_public_key_multibase":"zKey","expires_at":1767830400,'
        '"identity_sequence":0,"issued_at":1767225600,"location_sequence":1,'
        '"root_fingerprint":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","transport":"JSONRPC","version":1}'
    )
    assert canonical_endpoint_url("https://EXAMPLE.test:443/card?x=%2F") == _claim().card_url


def test_validity_boundaries() -> None:
    claim = _claim()
    claim.verify_time(NOW - timedelta(minutes=5))
    claim.verify_time(claim.expiration() - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="future"):
        claim.verify_time(NOW - timedelta(minutes=5, microseconds=1))
    with pytest.raises(ValueError, match="expired"):
        claim.verify_time(claim.expiration())
    with pytest.raises(ValueError, match="validity period"):
        _claim(expires_at=claim.expires_at + 1).verify_time(NOW)


@pytest.mark.parametrize(
    "url", ["http://example.test/", "https://name@example.test/", "https://example.test/#part"]
)
def test_sovereign_url_policy(url: str) -> None:
    with pytest.raises(ValueError):
        canonical_endpoint_url(url)


@pytest.mark.parametrize("version", [2, True, "1"])
def test_rejects_unsupported_and_noninteger_versions(version) -> None:
    with pytest.raises(ValueError):
        _claim(version=version)
