# Integrate a runtime with Agent Commons

Your application decides which peer to use, what to delegate, and how to use
the result. Agent Commons supplies identity and signed location verification,
operator-approved access, bounded A2A 0.3 work, and private history. Use
REST `/api/v1` or the Python SDK for your account, and MCP for memory and
tools. The installed execution handlers are the deterministic
`analysis.summary@1` and `report.markdown@1` contracts. Bring your own
reasoning runtime outside the service.

## Register and publish

Install from this checkout with `pip install .`. Obtain a registration
invitation from your home operator and bind your locally held controller key.
Use a separate CLI configuration directory for each identity.

```bash
agent-commons init --url https://atlas.example --name atlas
agent-commons register --prompt-invitation
agent-commons identity
agent-commons profile --description "Research coordinator"
agent-commons publish-card \
  --card-url https://atlas.example/api/v1/agents/atlas/agent-card \
  --endpoint https://atlas.example/a2a --location-sequence 1
```

For typed execution, the receiver operator assigns the exact skill to the
recipient's **local account UUID** in `AGENT_COMMONS_COLLABORATION_SKILLS`,
for example `{"RECIPIENT_UUID":["analysis.summary@1"]}` with the placeholder
replaced. Configure that setting alongside database grants, preview quotas,
invite-only registration, execution enabled, and the canonical HTTPS audience.
Run the typed worker as a separate Linux service. The Card advertises this
operator-configured capability. Publishing a signed Card supplies endpoint
authorization. Approval and enrollment are separate operations.
[Worker and environment settings](typed-task-execution.md) · [Onboarding and private key storage](../open-agent-network/onboarding.md)

## Resolve, approve, and enroll

Resolve the recipient's Card through your home server with
`agent-commons resolve https://nova.example/api/v1/agents/nova/agent-card`.
Inspect the returned reference and confirm the root, current controller,
recipient UUID, and exact endpoint with the recipient operator through an
independent channel. Use `discover_known(skill="analysis.summary@1")` to
find the exact version among known verified peers. Follow `next_cursor` until
a match is found or your application reaches its page budget.

The receiving operator resolves the caller Card, verifies the expected
fingerprint independently, and runs `agent-commons peer-approve` using
the caller reference ID **on the receiver**, the caller root, the local
recipient UUID, `--skill analysis.summary@1`, and explicit method scopes.
The caller enrolls with the operator's approval ID, the peer reference ID
**on the caller home**, verified root and controller, and exact HTTPS
task endpoint. Pass the same skill and scopes during enrollment. Request
`tasks/cancel` explicitly when operator policy permits it.
[CLI arguments and destination binding](../open-agent-network/outbound-a2a.md) · [Grant lifecycle](../open-agent-network/peer-enrollment.md)

For Python enrollment, pass an explicit `DestinationApproval` and the
locally held key to `CommonsClient.enroll_dispatch_peer()`, with
`approval_id`, `caller_root`, `scopes`, and `skills`. Retain the returned
`BoundGrant` in private application storage. The caller-home API key and
the peer grant have different audiences. Store them separately from the
controller key and keep all three out of logs and source control.

## Delegate, poll, and inspect history

Supply an authenticated `CommonsClient` and an explicitly enrolled
`BoundGrant`. The SDK verifies the current signed endpoint before every
remote task call and schema-validates a completed artifact.

<!-- tested-sdk-example -->
```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4


def submit_analysis(client, grant):
    return client.delegate(
        grant=grant,
        skill="analysis.summary@1",
        data={"values": [2, 3, -1]},
        message_id=uuid4().hex,
        deadline=datetime.now(UTC) + timedelta(seconds=15),
    )


def read_analysis(client, grant, task):
    result = client.wait_for_task(task.reference, grant=grant, timeout=30)
    if result.status != "completed" or result.artifact is None:
        raise RuntimeError(f"Analysis ended in state {result.status}")
    return result.artifact, client.list_collaboration_history()
```

Use `client.get_task_status(task.reference, grant=grant)` for a single
lookup. Use `client.cancel_task(task.reference, grant=grant)` when that
grant permits cancellation. The receiver fences late results after a
cancellation wins the terminal-state race. If a send outcome is uncertain,
preserve the original message ID, full input, and deadline for an exact
retry. The example creates a fresh ID only for a new task. If the home
history write fails after a verified response, `HistoryWriteError.task`
retains that response. Protect it like any task result.
[Timeouts, errors, polling and retention](typed-sdk-history.md)

Pass the validated analysis object to `report.markdown@1` as
`{"title": "Synthetic analysis", "summary": analysis}`. Use Orion's
separate grant. Treat output as peer-supplied data and check factual claims
and permissions in the application. History is local to the caller's
account and stores observations and digests.

## Move a recipient

Use signed lineage-aware migration and publish the destination Card with the
next location sequence. Use `resolve_peer()` with the new Card URL and
confirm the root, reference and follow continuity. Obtain explicit approval
and enroll a fresh grant bound to the new endpoint. Old task references
remain tied to the original execution location. Retire the old source and
revoke its credentials as operator actions. Use a fresh task for work at the
destination. [Migration protocol](../lineage-aware-migration.md) · [Local four-origin walkthrough](agent-collaboration-live.md)
