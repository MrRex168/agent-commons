import hashlib

import base58
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ED25519_MULTICODEC_PREFIX = b"\xed\x01"


def decode_ed25519_multikey(public_key_multibase: str) -> bytes:
    """Decode a W3C-style Ed25519 Multikey publicKeyMultibase value."""
    if not public_key_multibase.startswith("z"):
        raise ValueError("publicKeyMultibase must use base58btc multibase encoding")
    try:
        decoded = base58.b58decode(public_key_multibase[1:])
    except ValueError as exc:
        raise ValueError("Invalid base58btc public key") from exc
    if not decoded.startswith(ED25519_MULTICODEC_PREFIX):
        raise ValueError("Only Ed25519 Multikey public keys are supported")
    raw_key = decoded[len(ED25519_MULTICODEC_PREFIX) :]
    if len(raw_key) != 32:
        raise ValueError("Invalid Ed25519 public key length")
    return raw_key


def identity_fingerprint(public_key_multibase: str) -> str:
    """Return a stable fingerprint for the canonical Multikey representation."""
    raw_key = decode_ed25519_multikey(public_key_multibase)
    digest = hashlib.sha256(ED25519_MULTICODEC_PREFIX + raw_key).hexdigest()
    return f"sha256:{digest}"


def decode_signature_multibase(signature_multibase: str) -> bytes:
    if not signature_multibase.startswith("z"):
        raise ValueError("Signature must use base58btc multibase encoding")
    try:
        signature = base58.b58decode(signature_multibase[1:])
    except ValueError as exc:
        raise ValueError("Invalid base58btc signature") from exc
    if len(signature) != 64:
        raise ValueError("Invalid Ed25519 signature length")
    return signature


def verify_identity_signature(
    public_key_multibase: str,
    payload: str,
    signature_multibase: str,
) -> bool:
    raw_key = decode_ed25519_multikey(public_key_multibase)
    signature = decode_signature_multibase(signature_multibase)
    public_key = Ed25519PublicKey.from_public_bytes(raw_key)
    try:
        public_key.verify(signature, payload.encode("utf-8"))
    except InvalidSignature:
        return False
    return True
