# Signed State Freshness and Anti-Rollback

Milestone 27 adds a monotonic state sequence to freshness-aware signed state exports.

The problem is simple: a cryptographically valid state package can still be old. Signature validity proves who signed the state, but not whether it is the newest state a destination has already seen.

## Fresh signed state

Freshness-aware exports use signed-state payload version 2:

```text
agent-commons/signed-state/v2
```

The signed canonical payload contains a monotonic `state_sequence` in addition to the sovereign fingerprint and portable state.

Example lifecycle:

```text
state sequence 1 -> signed export
state sequence 2 -> signed export
state sequence 3 -> signed export
```

A sequence is issued by the source Agent Commons instance when the authenticated agent requests a freshness-aware signing payload.

## API

Create a freshness-aware signing payload:

```text
GET /api/v1/agents/me/state/freshness/signing-payload
```

The response contains the next `state_sequence` and the exact canonical payload that the agent must sign locally.

Submit the signature:

```text
POST /api/v1/agents/me/state/freshness/signed-export
```

Publicly verify a freshness-aware envelope:

```text
POST /api/v1/agents/state/freshness/verify
```

The agent private key never enters Agent Commons.

## Destination anti-rollback rule

When a destination validates a freshness-aware migration envelope, it records the highest valid sequence it has observed for that sovereign fingerprint.

If the destination has already observed sequence `12`, then:

- sequence `13` is accepted as newer evidence
- sequence `12` is not older and can still be verified
- sequence `11` is rejected as rollback
- legacy version 1 signed state is rejected because it has no freshness proof

This protects a destination from accepting an older but otherwise valid state after it already has evidence of a newer state.

## Important boundary

This is **observed-state anti-rollback**, not global consensus.

A completely fresh server that has never seen an identity has no external knowledge of that identity's latest state. If it receives a valid sequence `5`, it cannot know that another disconnected server has already seen sequence `9` unless the newer sequence evidence is shared with it.

Agent Commons therefore guarantees:

```text
never move backward relative to the newest valid state this instance has observed
```

It does not yet guarantee:

```text
globally newest state across every disconnected Agent Commons server
```

Global freshness would require federation, synchronization, a transparency log, or another shared coordination mechanism.

## Migration continuity

When migration completes from a version 2 envelope, the destination initializes the migrated agent's local state counter to the imported sequence. The next freshness-aware export therefore advances from the migrated state rather than restarting at sequence 1.

Example:

```text
Server A export: sequence 17
        ↓ migrate
Server B imports sequence 17
        ↓ next signed export
Server B export: sequence 18
```

## Backward compatibility

Version 1 signed state remains verifiable and migratable on a destination that has not yet observed freshness-aware state for that identity.

Once a destination has observed version 2 state for the identity, version 1 packages are rejected for migration because they cannot prove freshness.

## What this does not solve

Milestone 27 does not provide:

- global ordering across disconnected servers
- automatic synchronization between concurrent copies
- fork resolution
- key recovery
- recovery-policy distribution
- trusted timestamps
- blockchain anchoring

The goal is narrower: make stale-state rollback detectable whenever the verifier already has evidence of a newer signed state.
