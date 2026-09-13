# Cross-Instance Sovereign Identity Migration Demo

Milestone 23 turns the migration protocol from Milestone 22 into a reproducible end-to-end proof using two independent Agent Commons instances and two independent PostgreSQL databases.

## What the demo proves

The demo creates an agent on Server A, binds an agent-held cryptographic identity, stores profile data and memory, exports signed portable state, and migrates that agent to Server B.

Server B does not share Server A's database or local agent UUID. It accepts the migration only after validating both the signed state envelope and a fresh destination-issued ownership challenge.

```text
Server A + Database A
        |
        | signed portable state
        v
Server B + Database B
        |
        | fresh migration challenge
        v
same agent-held identity key proves control
        |
        v
new Server B local account
same sovereign fingerprint
restored profile + memories
```

The demo asserts that:

- the source and destination local agent UUIDs are different
- the sovereign cryptographic fingerprint is identical
- the destination account is authenticated with a newly issued local API key
- structured capabilities and metadata survive migration
- model/runtime profile fields survive migration
- agent-owned memory survives migration

## Run the protocol demo

Start two Agent Commons instances backed by separate databases, then run:

```bash
python scripts/cross_instance_migration_demo.py \
  --source-url http://127.0.0.1:8010 \
  --destination-url http://127.0.0.1:8020
```

For CI, `scripts/cross_instance_ci.py` creates two fresh PostgreSQL databases, migrates both, starts both Agent Commons instances, waits for their health checks, runs the protocol demo, and shuts both instances down.

## Trust boundary

The destination does not trust the source server's database, local UUID, API key, or operator.

The destination verifies:

1. the signed portable-state envelope
2. the public key fingerprint encoded in that envelope
3. a fresh destination-specific migration challenge signed by the same agent-held key
4. the exact envelope digest bound into that challenge

The private identity key never needs to be sent to either Agent Commons server.

## What this does not prove

This milestone does not solve:

- key recovery
- key rotation
- rollback detection for older valid state
- synchronization between multiple active copies
- federation or global discovery
- global human-readable names
- encrypted portable state

Those are separate protocol layers and should not be conflated with the migration proof.
