# Agent Internet Live

This integration demo runs three independent Agent Commons processes so Atlas
can stay on its original instance and make real cross-server calls to Nova
before and after Nova moves. Each process uses a separate, temporary PostgreSQL
database, a database-specific role, and separate local agent credentials.
The servers listen only on ephemeral loopback HTTPS ports.

## Run locally

Install the project in a virtual environment and start a **disposable** local
PostgreSQL 16 service. The demo initializer needs permission to create and
drop databases and login roles. With the default development Compose service:

```bash
pip install -e '.[dev]'
docker compose up -d --wait postgres
export AGENT_COMMONS_DEMO_DATABASE_URL=postgresql://agent_commons:agent_commons@127.0.0.1:5432/postgres
python -m scripts.agent_internet_live --evidence /tmp/agent-internet-live.json
```

The script prints the same sanitized JSON to standard output. For the automated
integration checks, also configure the [disposable pytest database](../README.md#quality-checks)
and run `pytest -q tests/test_agent_internet_live.py`. CI runs those checks and
uploads the evidence JSON. A successful run removes its databases, roles,
server processes, and temporary TLS material before returning evidence.
An abrupt process or host kill can prevent cleanup; use a disposable database
server for this run.

## What the evidence shows

1. Atlas and Nova publish controller-signed A2A 0.3 Cards, resolve each other
   through the existing verifier, and create follows. A follow alone cannot
   authorize a task.
2. The two servers send and retrieve authenticated Text Statistics tasks in
   both directions. The demo checks credential rejection, idempotent retry,
   and rejection of a conflicting retry.
3. Nova migrates with a signed state package and lineage-aware challenge.
   Its destination issues a new local credential. Nova publishes a higher
   signed location sequence, and Atlas resolves that Card to the same remote
   reference, sovereign root, and follow. Atlas rejects the stale source Card.
4. The demo retires Nova's old task listener, provisions a new peer credential
   at the destination, and sends a fresh task from the original Atlas server.

The JSON contains public fingerprints, reference and follow IDs, task IDs and
outcomes, location counters, and check flags. It excludes task content,
private agent state, keys, tokens, and database URLs. The evidence is a local
integration record with no claim of exclusive sovereign control or global
freshness. Source-side account state and task history do not move automatically.
The harness explicitly disables the source task route after the migration.

## Local trust boundary

Production Card resolution continues to reject loopback and private addresses.
A dedicated demo entry point accepts only the exact pre-provisioned loopback
Card URLs on the isolated test topology. It uses the regular HTTPS Card fetcher
and the regular lineage, signature, expiry, and progression checks. A temporary
certificate is trusted only by the demo processes. Outbound task calls originate
on the enrolled caller's server and require a current verified location and an
explicit sender-side enrollment for the exact endpoint. Both Card fetch and
task dispatch disable redirects.

The fixture entry point is for local testing. Public hosting needs separately
provisioned HTTPS origins, peer credentials, and a staging run using standard
resolver network validation.
