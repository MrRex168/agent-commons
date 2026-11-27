# Typed skill contract and authorization (Milestone 7.1)

Milestone 7.1 adds typed contracts, discovery, enrollment, and internal
admission validation. Milestone 7.2 provides opt-in execution for the two
built-in contracts. See the execution runbook for the separate worker and
required isolation settings. [Execution runbook](typed-task-execution.md)

## Local registry and discovery

`agent_commons.collaboration.registry` provides the contracts
`analysis.summary@1` and `report.markdown@1`. Each has an exact ID and version,
strict local input and output models, media types, size limits, and a maximum
execution time. Inspect each model's JSON Schema with `model_json_schema()`.
Schema URNs identify immutable contracts and do not instruct clients to fetch
anything. Changing a contract requires a new version. Register custom models
in trusted local startup code before loading app settings.

An operator opts in per local recipient UUID using
`AGENT_COMMONS_COLLABORATION_SKILLS='{"RECIPIENT_UUID":["analysis.summary@1"]}'`.
Unknown and duplicate entries fail configuration validation. A Card that
publishes controller-signed endpoint authorization for the configured agent
includes `urn:agent-commons:extension:collaboration:v1`. Existing Card skills
and media types remain available as before. User-supplied profile metadata
cannot publish extensions under this reserved namespace.

Search already resolved Cards with
`GET /api/v1/agents/discovery/remote?skill_exact=analysis.summary%401&verified_only=true`.
The exact filter matches both skill ID and schema version in the advertised
extension. It uses the same materialized candidate window, page cap, cursor,
and timeout as existing discovery. Use `skill` for the existing substring
search. Results describe advertisements. A signed endpoint proves where
a controller authorized a peer to connect, and a receiving operator's
grant supplies separate execution permission.

## Enrollment and compatibility

With database-backed grants enabled, the receiving operator adds
`--skill analysis.summary@1` to `agent-commons peer-approve`. Use the matching
`--skill` on `peer-enroll` with all existing destination-binding options, or
pass `skills=["analysis.summary@1"]` to `CommonsClient.enroll_dispatch_peer`.
The client verifies the expected list before signing a
`agent-commons/peer-enrollment/v2` challenge. The challenge also binds the
caller root and controller, recipient, methods, HTTPS audience and expiry.
Typed bearers have the `a2c_` prefix and are stored with their exact scopes.
Rotation preserves these scopes. Reapproval revokes existing grants before
issuing different permissions.

Legacy challenges keep their v1 signature domain and payload. Legacy
`a2a_` credentials continue to authorize only the existing Text Statistics
path. Skill-scoped credentials are rejected by that path. The prior managed
and static peer configurations continue to work with the feature disabled.

Apply migration `0018_collaboration_skill_scopes` before upgrading workers and
upgrade all workers before enabling skills. The database rejects issuance
when a grant's stored skills differ from its approval, including attempted
legacy issuance against a typed approval. To downgrade, stop workers and
disable typed configuration first. The downgrade deliberately removes typed
approvals and grants before discarding their discriminator, while retaining
legacy approvals and grants. Reenroll typed peers after re-upgrading.

## Internal admission contract

The internal `admit_typed_task` preflight accepts bounded UTF-8 JSON with four
fields:

```json
{
  "skill": "analysis.summary@1",
  "message_id": "stable-request-id",
  "deadline": "2026-11-26T12:00:20Z",
  "input": {"values": [2, 3, 7]}
}
```

Provide a timezone-aware future deadline within the skill's time budget and
grant lifetime. The SHA-256 digest covers the validated input, exact skill,
message ID, and UTC-normalized deadline with a dedicated domain. Duplicate
JSON keys, invalid types, unrecognized versions, oversized inputs, unexpected
fields, unavailable grants, and wrong receiving context fail closed. Output
models and byte bounds are available independently for the execution layer.
The digest is a canonical admission input. Task reservation, replay storage,
and execution do not occur in this milestone.

## Local spike and next gate

The local checks verify opt-in Card advertisement and exact discovery,
typed enrollment, signed scope tampering, rotation, wrong receiver bindings,
and legacy task isolation. Independent deployment and v0.6 release acceptance
remain separate gates, including the existing 297/300 capacity requirement.

Milestone 7.2 should add authenticated ingress and an atomic task reservation
with the authoritative quota and ownership checks in the admission path.
Derive the receiver and HTTPS audience from trusted routing, never the task
body. Recheck grants and deadlines before execution and fence late results.
Start with isolated side-effect-free handlers with no ambient credentials or
network access, and add explicit cancellation and bounded result retention.
