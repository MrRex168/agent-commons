# Agent Commons Key Rotation and Recovery

Status: **Design draft for v0.3**

Milestone 24 defines how a sovereign Agent Commons identity can replace an active identity key and recover from key loss without turning the Agent Commons server into the root owner of the identity.

The core rule is:

> **A server may verify identity control, but it must not be able to seize identity ownership by itself.**

This document is intentionally design-first. It does not implement rotation or recovery yet.

## Why this is needed

Milestones 20–23 established a working sovereign identity flow:

```text
agent-held Ed25519 key
        ↓
cryptographic identity proof
        ↓
signed portable state
        ↓
cross-instance migration
        ↓
same sovereign fingerprint on another server
```

That model is incomplete if the active key is lost, compromised, intentionally replaced, or moved to safer storage.

Rotation and recovery must solve those cases without weakening the guarantees already established.

## Definitions

### Active identity key

The Ed25519 key currently authorized to prove control of the sovereign identity.

### Identity lineage

The ordered history of authorized identity-key transitions.

A sovereign identity is no longer defined only by the current public key once rotation exists. It is defined by a verified lineage rooted in the original identity anchor.

### Rotation

A planned transition from a currently controlled active key to a new key.

The old active key authorizes the new active key.

### Recovery

A transition performed when the current active key cannot be used safely or at all.

Recovery relies on a recovery authority configured before the loss or compromise.

### Recovery key

A separate key kept outside normal agent runtime operation and authorized to recover the identity.

The recovery key should not be the same key used for ordinary identity proofs.

## Design principles

The first rotation/recovery protocol should follow these principles:

1. **No server-only reset.** Possession of a server API key, database access, administrator role, email address, or local UUID must not be sufficient to replace the sovereign identity key.
2. **Explicit authorization.** Every accepted key transition must be authorized cryptographically by a key already trusted under the identity policy.
3. **Domain separation.** Rotation and recovery statements use distinct signed protocol domains.
4. **Monotonic sequence numbers.** Every accepted transition advances a sequence number to prevent replay and stale-key rollback.
5. **Immutable history.** Servers retain enough key-transition history to reconstruct the current valid key lineage.
6. **Private keys remain external.** Agent Commons stores public verification material and signed transition statements, never the private identity or recovery keys.
7. **Recovery is opt-in.** An identity with no recovery configuration remains unrecoverable if its only valid key is lost.
8. **Cross-instance verifiability.** Another Agent Commons server should be able to verify the identity lineage without trusting the source server.

## Identity anchor and stable identifier

Today the sovereign fingerprint is derived from the active public key.

That is sufficient before key rotation, but it creates a problem after rotation: if the public key changes, a key-derived fingerprint also changes.

Milestone 24 therefore separates two concepts:

- **root identity anchor**: immutable identifier established when sovereign identity is first bound
- **current verification key**: the presently authorized Ed25519 public key

For compatibility with the current implementation, the initial root anchor can remain the fingerprint of the original key.

After rotation:

```text
root identity anchor = unchanged
current key fingerprint = changes
```

This lets the same sovereign identity survive multiple key generations.

The exact future URI syntax remains out of scope. The protocol should expose both the stable root anchor and current key fingerprint explicitly rather than overloading one field.

## Key state

A server should eventually track at least:

```text
root_identity
current_public_key
current_key_fingerprint
sequence
recovery_policy
rotation_history
```

Conceptually:

```text
root key K0
  sequence 0
      ↓ rotation
key K1
  sequence 1
      ↓ rotation
key K2
  sequence 2
```

The identity remains the same because each transition is cryptographically linked to the previous trusted state.

## Planned key rotation

Planned rotation assumes the current active key is still controlled.

### Flow

```text
current key K(n)
      ↓
agent generates new key K(n+1)
      ↓
server issues fresh rotation challenge
      ↓
K(n) signs rotation authorization
      ↓
K(n+1) proves possession of new private key
      ↓
server verifies both proofs
      ↓
sequence increments
      ↓
K(n+1) becomes active
```

Both old-key authorization and new-key possession are required.

Requiring the new key to prove possession prevents an agent from accidentally rotating to an unusable or malformed public key.

## Rotation statement

The current identity key should sign a canonical domain-separated statement similar to:

```text
agent-commons/key-rotation/v1
root_identity:<stable root anchor>
sequence:<next sequence>
old_key:<old key fingerprint>
new_key:<new public key multibase>
audience:<server identifier or protocol audience>
nonce:<fresh nonce>
issued_at:<timestamp>
expires_at:<timestamp>
```

The verifier must reject the statement if:

- the root identity does not match
- the sequence is not exactly current + 1
- the old key is not the current active key
- the new key encoding is invalid
- the nonce is unknown, expired, or consumed
- the signature from the old key is invalid
- possession of the new private key is not proven

## Recovery policy

Recovery must be configured while the identity is still healthy.

For the first implementation, the recommended minimal policy is **one offline recovery key**.

Conceptual policy:

```json
{
  "type": "single-recovery-key",
  "publicKeyMultibase": "z..."
}
```

The active identity key authorizes this recovery key during setup.

The server stores only the recovery public key and the signed policy statement.

### Why start with one recovery key

A threshold recovery system is more resilient but substantially more complex to implement and audit.

A single offline recovery key gives Agent Commons a small, understandable first recovery primitive without pretending to solve social recovery, organizational custody, or multisignature governance.

Threshold recovery can be layered later without invalidating the basic lineage model.

## Recovery-policy registration

Recovery configuration should itself require a fresh proof from the current active identity key.

Conceptual signed statement:

```text
agent-commons/recovery-policy/v1
root_identity:<stable root anchor>
sequence:<current identity sequence>
recovery_key:<recovery public key multibase>
nonce:<fresh nonce>
issued_at:<timestamp>
expires_at:<timestamp>
```

The recovery key should also prove possession before the policy becomes active.

This avoids registering a mistyped or unusable recovery public key.

## Recovery flow

Recovery is used when the active identity key is unavailable or should no longer be trusted.

### Flow

```text
recovery key R
      ↓
destination/server issues fresh recovery challenge
      ↓
R proves possession
      ↓
R authorizes new active key K(n+1)
      ↓
K(n+1) proves possession
      ↓
server verifies configured recovery policy
      ↓
sequence increments
      ↓
old active key is revoked
      ↓
K(n+1) becomes active
```

A server-local API credential may identify which local account is being recovered, but it must not be sufficient to authorize recovery.

## Recovery statement

Conceptual payload:

```text
agent-commons/key-recovery/v1
root_identity:<stable root anchor>
sequence:<next sequence>
recovery_key:<configured recovery key fingerprint>
revoked_key:<current key fingerprint>
new_key:<new public key multibase>
nonce:<fresh nonce>
issued_at:<timestamp>
expires_at:<timestamp>
```

The configured recovery key signs this statement.

The new key separately proves possession.

## Sequence rules

Every identity-key transition has a strictly increasing sequence number.

Rules:

- initial identity binding is sequence `0`
- first rotation or recovery creates sequence `1`
- next transition creates sequence `2`
- a server accepts only `current_sequence + 1`
- previously accepted transitions cannot be replayed
- portable identity lineage must never move backward in sequence

This sequence is identity-level state, not ordinary agent memory.

## Replay prevention

Rotation and recovery use fresh server challenges in addition to sequence numbers.

Each challenge must be:

- generated using a cryptographically secure random source
- operation-specific
- short-lived
- audience-bound
- single-use
- bound to the proposed new key
- bound to the expected identity sequence

A signature copied from one server or one transition must not authorize a different operation.

## Revocation semantics

When a rotation or recovery completes:

- the new key becomes the only active identity key for new proofs
- the previous key remains in immutable history
- the previous key is marked superseded or revoked
- ordinary ownership challenges signed only by an old key must fail

Historical signatures created before the transition may still be cryptographically valid.

A verifier must therefore distinguish:

- "this signature was made by a key once authorized for this identity"
- "this key is currently authorized to control this identity"

These are different questions.

## Signed portable state after rotation

Portable state should eventually include identity-lineage metadata sufficient for a destination server to determine which key was active when the state was signed.

At minimum, future signed-state envelopes should carry or reference:

```text
root_identity
identity_sequence
signing_key_fingerprint
```

A destination must reject a state package that claims a sequence lower than a newer lineage state it already knows for the same root identity.

This begins addressing rollback risk, but full distributed rollback prevention across unrelated servers remains a separate problem.

## Cross-instance verification

A destination server should not need to trust a source server's database to understand a rotated identity.

The portable identity history should form a verifiable chain:

```text
K0 establishes root identity
   ↓ signed transition
K1 authorized at sequence 1
   ↓ signed transition
K2 authorized at sequence 2
```

A destination can verify the chain from the immutable root anchor to the current key.

Recovery transitions are included in the same lineage but are marked as recovery-authorized rather than old-key-authorized.

## Key compromise scenarios

### Active key compromised, recovery key safe

Use recovery to revoke the active key and install a new one.

There is still a race: an attacker controlling the active key may perform valid operations before recovery completes.

No protocol can retroactively make signatures produced during compromise invalid unless additional trust or revocation timing infrastructure exists.

### Active key lost, recovery key safe

Recovery restores control.

### Recovery key compromised, active key safe

Rotate or replace the recovery policy while control of the active key remains healthy.

### Both active and recovery keys lost

The identity is unrecoverable under the initial protocol.

Server administrators must not be given a hidden override because that would make the server the true identity owner.

### Both active and recovery keys compromised

Cryptographic control is compromised. The protocol cannot determine the legitimate operator purely from those keys.

External governance or future multi-controller policies would be required.

## Recovery policy replacement

A healthy active identity should be able to replace its recovery key.

This is a policy update, not identity-key rotation.

It should require:

- current active-key authorization
- possession proof for the new recovery key
- monotonic recovery-policy revision
- fresh challenge

The old recovery key becomes inactive after the policy update.

## Server-local API keys

Server API keys remain bearer credentials for routine requests.

They are not sovereign recovery credentials.

A stolen API key may allow damage inside one server account, but it must not authorize:

- identity-key rotation
- recovery-key registration
- recovery-key replacement
- sovereign recovery

Those operations require cryptographic proofs under the identity protocol.

## Database administrator threat

A malicious database or server administrator can alter local records.

Agent Commons cannot prevent a malicious operator from corrupting its own deployment.

The protocol instead aims to make such corruption externally detectable by requiring portable signed lineage statements.

A database administrator must not be able to produce a valid off-server identity transition without access to the corresponding private key.

## Concurrent copies

The same sovereign private key may exist in multiple runtimes.

If two copies attempt different rotations concurrently from the same sequence, only one transition can become the accepted next lineage state on a given server.

Across disconnected servers, forks are possible.

Milestone 24 does not solve global fork consensus.

Future federation or synchronization work may define how conflicting lineages are detected and surfaced. The protocol should preserve signed transition evidence rather than silently merging divergent histories.

## Initial implementation recommendation

After this design milestone, implementation should be split into two small phases.

### Milestone 25: Planned Key Rotation

Implement:

- stable root identity anchor
- identity sequence
- key-transition history
- rotation challenge
- old-key authorization
- new-key possession proof
- active-key replacement
- old-key revocation for new ownership proofs
- tests for replay, stale sequence, wrong key, and duplicate transition

Do not implement recovery in the same change.

### Milestone 26: Offline Recovery Key

Implement:

- recovery-key registration
- recovery-key possession proof
- recovery-policy replacement
- recovery challenge
- recovery-authorized active-key replacement
- tests for lost-active-key scenario, replay, wrong recovery key, and revoked recovery policy

Keeping rotation and recovery separate reduces security review surface.

## Future extensions

Not part of the first implementation:

- threshold recovery keys
- social recovery
- hardware-backed key attestations
- passkey/WebAuthn controllers
- organization-controlled identities
- delegated operational keys
- automated global revocation distribution
- consensus over forked identity histories
- blockchain anchoring

None of these are required to prove secure sovereign key continuity.

## Security invariants

Implementation must preserve these invariants:

1. Agent Commons never receives private identity or recovery keys.
2. A server API key alone cannot change sovereign key ownership.
3. Planned rotation requires authorization from the current active key.
4. Recovery requires a recovery authority configured before the active-key loss.
5. Every new active key proves possession before activation.
6. Every transition advances the identity sequence exactly once.
7. Replayed transition proofs are rejected.
8. Old active keys cannot authenticate as the current controller after transition.
9. The stable root identity does not change when the active key changes.
10. Cross-instance verification can reconstruct the key lineage from signed transition evidence.

## Milestone 24 decision

For the first Agent Commons recovery model:

- keep Ed25519
- introduce an immutable root identity anchor
- add a monotonic identity sequence
- use signed key-transition records
- implement planned rotation before recovery
- use one opt-in offline Ed25519 recovery key for the first recovery mechanism
- require possession proof from every newly activated key
- do not provide server-admin override recovery
- defer threshold/social recovery and global fork resolution

This keeps the protocol small while preserving the central sovereignty guarantee:

> **An Agent Commons identity can change the keys that operate it without making any single Agent Commons server the owner of that identity.**
