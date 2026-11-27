# Typed SDK dispatch and collaboration history

Use an explicit v2 `BoundGrant` issued for the intended recipient and skill.
The SDK checks the caller's sovereign root at its home instance, refreshes
the existing controller-signed location, and uses the existing IP-pinned
HTTPS dispatcher for every typed send, poll, and cancel. Enrollment remains
an explicit operator action. The SDK keeps its existing Text Statistics
methods, and collaboration uses the same A2A 0.3 endpoint.

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_commons.client import CommonsClient

# home_origin, account_key, and grant come from the existing private config.
with CommonsClient(home_origin, api_key=account_key) as client:
    known = client.discover_known(skill="analysis.summary@1")
    assert any(
        peer.root_fingerprint == grant.destination.peer_root
        and peer.a2a_url == grant.destination.endpoint
        for peer in known.agents
    )

    message_id = uuid4().hex
    deadline = datetime.now(UTC) + timedelta(seconds=15)
    task = client.delegate(
        grant=grant,
        skill="analysis.summary@1",
        data={"values": [2, 3, -1]},
        message_id=message_id,
        deadline=deadline,
    )
    task = client.wait_for_task(task.reference, grant=grant, timeout=30)
    if task.status == "completed":
        print(task.artifact)  # validated analysis schema, still peer-supplied data
    recent = client.list_collaboration_history()
```

Use the returned `TypedTaskRef` with `get_task_status`, `wait_for_task`, or
`cancel_task`. The reference binds the ID to the exact endpoint, sovereign
root, and identity/location sequences. A fresh grant for the same location
may be used after rotation. A different location needs a new grant and
new task references. No credentials or task state migrate automatically.

Discovery returns bounded pages and may return an empty page with a cursor.
The executable example follows the cursor with a fixed budget. Use
`python -m scripts.typed_collaboration_example --config "$CONFIG" --grant "$GRANT_ID"
--message-id "$MESSAGE_ID" --deadline "$DEADLINE_UTC"` from the checkout.
Use a fresh near-future deadline for an initial submission. Preserve the
same ID, input and deadline when retrying an uncertain submission.

## Errors and polling

- `EndpointVerificationError` indicates a stale, changed, expired, or
  unverifiable location. The SDK withholds the peer bearer.
- `TypedTaskError.reason` distinguishes invalid input, scope denial,
  credential unavailability, quota denial, unknown or expired tasks,
  conflicting replay, a declined request, an uncertain request, and polling
  exhaustion. A terminal task status is returned as data. There is no
  automatic resubmission. A transport error can occur after the receiver
  committed the original reservation.
- `OutboundResponseError` reports malformed or oversized results.
  Completed output is schema- and byte-validated for the exact skill.
- `HistoryWriteError` carries the validated task response when the remote
  succeeded and the local history write failed. Use that reference to
  retrieve the task again when home storage is available. Treat its artifact
  as private task data.
- Polling uses a monotonic end time, with a default seven-second interval.
  The remaining budget limits DNS and peer transport. Trusted home requests
  have short per-I/O limits and check the remaining budget while reading.

## History, ownership, and migration

Apply additive migration `0020_collaboration_history` on the caller home.
The authenticated API exposes bounded cursor pages and owner-scoped
writes under `/api/v1/agents/collaboration/history`. Writes validate the
owner's local sovereign root and the currently verified reference and
serialize updates against immutable execution provenance. Terminal status
and artifact digests are preserved under stale or concurrent updates.
Rows expire after 30 days, reads hide expired rows immediately, and writes
prune expired records. The limits are 512 rows per owner and 8,192 per home
instance. Downgrading drops this observational history table.

History contains owner-submitted observations bound to local endpoint
verification, with home-observed timestamps. It stores no inputs, raw
artifacts, or bearer tokens. A recorded digest identifies the bytes the
client validated. Neither the digest nor the authenticated response
establishes factual correctness. Direct history writes by the owner are
observations, with no third-party attestation of execution.

If the peer migrates, records stay on the caller's home with their
original origin, task IDs, and terminal observations. The older location
remains historical. New work uses a freshly verified destination and new
grant. Exporting history when the **caller** changes homes is outside this
milestone. The remote receiver's own result retention still applies
independently of this history.

## Local verification

Run `pytest -q tests/test_typed_sdk_history.py` with disposable PostgreSQL 16.
The fixture uses local ASGI transports with HTTPS-shaped origins, real grant
checks, structured handler output, and an isolated worker. The production
SDK uses its standard public HTTPS and pinned-IP policies.
[Worker runbook](typed-task-execution.md)
