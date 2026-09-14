# Identity Lineage v2

Identity Lineage v2 makes recovery-policy history portable and independently verifiable.

## Why v2 exists

Lineage v1 retained only the current recovery policy. That was sufficient for a single recovery, but it could not verify a long-lived identity after the recovery policy had been replaced and an older recovery transition depended on the previous policy revision.

Lineage v2 preserves every recovery-policy statement created after the v2 history migration. Each statement carries:

- policy revision
- identity sequence at which the policy was authorized
- active controller key that authorized it
- recovery public key
- exact signed policy payload
- active-key signature
- recovery-key possession signature

Every recovery transition references the exact policy revision that authorized it.

## Verification

A verifier reconstructs the controller chain from the sovereign root and verifies that:

1. each identity transition is contiguous;
2. each recovery policy is signed by the controller active at its recorded identity sequence;
3. each recovery transition references an available policy revision;
4. the recovery signature matches that policy's recovery key;
5. the replacement controller proves possession of its key; and
6. the final controller and sequence match the lineage envelope.

This supports identities with repeated policy replacement and recovery, for example:

```text
K0 + recovery policy R1
        ↓ recovery using R1
K1 + recovery policy R2
        ↓ recovery using R2
K2
```

Both R1 and R2 remain part of the portable proof.

## Backward compatibility

The verifier continues to accept lineage v1 packages.

When upgrading an existing installation, migration `0010_recovery_policy_history` preserves the currently configured recovery policy as the first available immutable history row. Policies that were already superseded before the upgrade cannot be reconstructed cryptographically because the old server never retained them. If an existing recovery transition depends on such missing evidence, export continues to fail closed rather than manufacturing history.

## Security boundary

Policy history proves what was signed and how controller authority changed. It does not provide global consensus between disconnected Agent Commons servers. A verifier can validate the evidence presented to it, but it cannot know about a newer conflicting lineage that it has never observed.
