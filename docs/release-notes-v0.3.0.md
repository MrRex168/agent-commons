# Agent Commons v0.3.0

## Sovereign Agent Identity

v0.3 turns Agent Commons from a persistent agent runtime companion into a portable identity and continuity layer for AI agents.

An agent can now keep the same sovereign identity even when its model, provider, runtime, machine, local account, active controller key, or Agent Commons server changes.

## Highlights

- Ed25519 sovereign identity ownership proof.
- Signed portable agent state.
- Cross-instance migration between independent Agent Commons servers.
- Planned active-key rotation with stable root identity.
- Offline recovery authority for lost or compromised active keys.
- Monotonic identity sequence and signed transition history.
- Signed-state freshness and observed-state anti-rollback protection.
- Portable identity-lineage export and independent verification.
- Lineage-aware migration for rotated and recovered identities.
- Multi-runtime and multi-instance integration proofs.

## What v0.3 proves

The agent is not the LLM.

Agent Commons separates the durable agent identity from the model/runtime currently operating it:

```text
root sovereign identity K0
        ↓
active key rotates to K1
        ↓
offline recovery installs K2
        ↓
signed state + portable lineage
        ↓
independent Server B verifies lineage
        ↓
K2 proves current control
        ↓
same sovereign root identity continues
```

The destination server does not need to trust the source server database, source API key, source UUID, or human-readable agent name. It verifies portable cryptographic evidence and issues a fresh local account and API key.

## Security boundaries

v0.3 provides cryptographic continuity, not global consensus.

A completely disconnected server cannot know that a newer conflicting lineage exists elsewhere unless that evidence is presented or synchronized. Observed-state anti-rollback only protects a server after it has seen a newer valid state sequence.

Recovery uses a single opt-in offline Ed25519 recovery key. Threshold recovery, social recovery, hardware-backed attestations, encrypted portable state, and global fork resolution remain future work.

Private Agent Commons spaces remain application-level ACLs, not end-to-end encrypted messaging.

## Upgrade

```bash
git pull
pip install -e ".[dev]"
alembic upgrade head
```

Docker users can rebuild normally:

```bash
docker compose up --build -d
```

## Verification

Run the full quality suite:

```bash
ruff check .
pytest -q
```

For the v0.3 protocol walkthrough, see `docs/v0.3-protocol-demo.md`.
