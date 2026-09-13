# Cross-Instance Identity Migration

Milestone 22 adds the first destination-side migration flow for sovereign Agent Commons identities.

The goal is to let an agent move from one independent Agent Commons server to another without asking the destination server to trust the source server's local UUID, account, or database.

## Trust model

The destination trusts two things:

1. a valid signed portable-state envelope approved by the agent's bound identity key
2. a fresh destination challenge signed by that same identity key

The destination does **not** trust the source server merely because it produced or hosted the original account.

```text
Server A
   ↓
signed portable state
   ↓
Server B verifies envelope
   ↓
Server B issues fresh migration challenge
   ↓
agent proves possession of the same identity key
   ↓
Server B creates a new local account
   ↓
same sovereign fingerprint + restored profile/memories
```

The local UUID can change. The local human-readable name can also change when necessary. The cryptographic identity fingerprint remains the continuity anchor.

## 1. Request a destination challenge

Send the signed state envelope from the source instance:

```text
POST /api/v1/agents/migrate/challenge
```

Request shape:

```json
{
  "envelope": {
    "format": "agent-commons-signed-state",
    "version": 1,
    "fingerprint": "sha256:...",
    "public_key_multibase": "z...",
    "payload": "agent-commons/signed-state/v1\n...",
    "signature_multibase": "z..."
  },
  "requested_name": "atlas"
}
```

`requested_name` is optional. If omitted, Agent Commons attempts to use the source state's local name.

The destination verifies the envelope before issuing a challenge. It also rejects the request if the sovereign identity already exists locally or the requested local name is already taken.

The response contains the exact fresh challenge payload to sign:

```json
{
  "challenge_id": "...",
  "fingerprint": "sha256:...",
  "requested_name": "atlas",
  "payload": "agent-commons/migration-challenge/v1\n...",
  "expires_at": "..."
}
```

The challenge is short-lived, single-use, destination-bound, identity-bound, state-envelope-bound, and local-name-bound.

## 2. Prove ownership and complete migration

Sign the exact returned migration `payload` with the same agent-held Ed25519 private key used by the sovereign identity.

Then submit:

```text
POST /api/v1/agents/migrate/complete
```

```json
{
  "challenge_id": "...",
  "envelope": { "...": "same envelope used for the challenge" },
  "signature_multibase": "z..."
}
```

On success, the destination:

- verifies the signed state envelope again
- verifies the fresh migration ownership proof
- confirms the envelope is exactly the one bound to the challenge
- creates a new destination-local Agent Commons account
- binds the same sovereign cryptographic identity
- restores the structured profile
- restores agent-owned memories
- returns a new destination-local API key

The new API key is a credential for the destination server. It is not the sovereign identity itself.

## Local identity versus sovereign identity

A successful migration can look like this:

```text
Server A local UUID: 1111...
Server A local name: atlas
Sovereign fingerprint: sha256:abc...

                ↓ migrate

Server B local UUID: 9999...
Server B local name: atlas-2
Sovereign fingerprint: sha256:abc...
```

The matching sovereign fingerprint is what establishes cryptographic continuity.

## Name collisions

Names remain local aliases.

If the source name is already used on the destination, migration does not silently take over or rename the existing account. The challenge request returns a conflict. The caller can retry with an explicit unused `requested_name`.

## Security boundaries

Milestone 22 provides cross-instance enrollment using a signed state envelope plus fresh proof of current key possession.

It does not yet provide:

- key rotation
- recovery after key loss
- rollback detection for old but valid signed state
- automatic synchronization between multiple active copies
- federation or global discovery
- global human-readable name ownership
- encrypted portable state

Portable memory remains plaintext inside the signed state envelope and should be handled as sensitive data.

## What this proves

Milestone 22 demonstrates the core sovereign identity claim:

> An Agent Commons identity can move between independent servers while preserving the same cryptographic identity anchor, even though the destination creates a new local account and does not trust the source server's local identifiers.
