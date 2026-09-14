# Current-controller signed portable state v3

Agent Commons v0.4 begins by removing the remaining requirement that new portable state be signed by the original sovereign root key.

The sovereign root identity remains stable, but the currently verified controller key signs new state.

```text
root identity K0
    ↓
rotate/recover
    ↓
current controller K1/K2/...
    ↓
signs portable state v3
    ↓
portable lineage proves that controller legitimately descends from K0
```

## Why v3 exists

Signed-state v1 and v2 are rooted directly in the original bound identity key. That is sufficient while the root private key remains available, but it creates a recovery gap: after a legitimate recovery to a new controller key, an agent should not need the lost root private key to create future portable state.

v3 closes that gap.

## Signed payload

The v3 payload is domain-separated with:

```text
agent-commons/signed-state/v3
```

It binds these fields into one canonical signature:

- `root_fingerprint`
- `controller_public_key_multibase`
- `identity_sequence`
- `state_sequence`
- `lineage_digest`
- portable agent state

The `lineage_digest` is the SHA-256 digest of the exact canonical portable identity lineage used to establish the current controller.

## API

Create a v3 payload for the authenticated agent:

```text
GET /api/v1/agents/me/state/controller/signing-payload
```

Sign the returned `payload` with the private key corresponding to `controller_public_key_multibase`, then submit:

```text
POST /api/v1/agents/me/state/controller/signed-export
```

Independent verification accepts both the signed state envelope and the portable lineage:

```text
POST /api/v1/agents/state/controller/verify
```

The verifier first verifies the lineage from the sovereign root to the claimed current controller, then verifies that the state signature was produced by that current controller.

## Verification rules

A v3 state package is accepted only when:

1. the portable lineage is cryptographically valid;
2. the lineage root matches the signed state's `root_fingerprint`;
3. the lineage's verified current key matches `controller_public_key_multibase`;
4. the lineage identity sequence matches `identity_sequence`;
5. the canonical lineage digest matches `lineage_digest`;
6. the state signature verifies under the current controller key; and
7. the payload is canonical and its state sequence is valid.

## Security boundary

v3 proves that the state was signed by the current controller established by the supplied portable lineage. It does not provide global consensus about whether a conflicting newer lineage exists elsewhere.

Observed-state anti-rollback remains a separate server-side property. Integration of v3 state into lineage-aware migration is a later v0.4 milestone.

## Compatibility

Signed-state v1 and v2 remain available for backward compatibility. v3 is a new path rather than a breaking replacement.
