# External preview security architecture

The preview admits operator-enrolled peers only. Receiving-side grants and
direct client dispatch are implemented. Independent staging remains pending.
[Peer enrollment runbook](peer-enrollment.md) · [Outbound A2A guide](outbound-a2a.md)

## Trust boundaries

| Layer | Evidence and authority |
| --- | --- |
| Sovereign identity | Verify the existing Ed25519 lineage for controller continuity. Controller keys stay with agents. |
| Endpoint authorization | Verify the current controller’s signed location, exact primary Card and task URLs, transport, epoch, progression, and expiry. This authorizes the location. |
| Peer enrollment | The recipient operator decides which caller root may connect to a local recipient. The signed enrollment challenge proves possession of the expected current controller key. |
| Task permission | An active bearer grant permits only its enrolled recipient and methods. Task owners retain read isolation. A follow or advertised skill grants no authority. |

Operators control their instance and database. A verified peer can still send
malicious input or conduct abusive behavior. First contact can accept an older
unexpired signed location. Existing rollback checks apply to observed state;
they provide neither global freshness nor proof of exclusive control.
[Existing endpoint boundary](../sovereign-endpoint-authorization.md)
· [Current task limits](../a2a-task-runtime.md#deployment-limits)

## Enrollment and credential lifecycle

1. Require operator-issued, short-lived registration invitations on
   preview-facing deployments. Consume invitations atomically on registration,
   with audit events that contain IDs and outcomes only. Apply admission policy
   to versioned REST, any legacy registration alias, and registration through
   MCP. Preserve development behavior when preview mode is disabled.
2. The recipient operator independently approves a relationship between the
   expected caller root, one recipient’s local account, and specific methods.
   Establish the expected root out of band and verify its Card through the
   production resolver. An enrollment request does not create a grant.
3. Issue a domain-separated, canonical challenge bound to a random ID, the
   recipient HTTPS audience, caller root and verified controller epoch, local
   recipient, method scopes, and expiry. Target five minutes. The controller
   signs locally. At redemption, recheck the expected controller and approval,
   verify the signature, and conditionally consume the challenge and create the
   grant in one transaction. Concurrent redemption returns at most one success.
4. Issue an independent high-entropy bearer over TLS and display it once.
   Store only its digest, owner and scope, audience, expiry, and lifecycle
   timestamps. Never reuse a local API key. Use a one-hour default and 24-hour
   maximum for the preview. Follow the existing task ownership tuple so
   rotating a token for the same caller and recipient preserves legal reads.
5. Rotate by issuing a replacement for the same binding with a bounded overlap
   of at most five minutes. Revoke or expire independently. An explicit
   revocation takes effect on all workers for new authorization decisions
   after commit. Requests authorized before revocation can finish.
   Use PostgreSQL as the authoritative grant source in preview mode and do
   not cache positive authorization decisions. Preserve legacy static grants
   outside preview mode. Reject mixing static grants and database-backed grants
   in preview mode so switching credential sources cannot bypass revocation.
   Controller changes require reapproval for new grants. Operators must revoke
   already-issued grants when a previously approved controller loses authority.

## Safe transport and migration

- **Card fetching and task dispatch:** Both paths use PycURL/libcurl for
  public HTTPS. Validate all DNS answers, then pin the connection to a vetted
  numeric address with `CONNECT_TO`. Preserve TLS SNI and hostname verification
  for the original hostname. Disable redirects and ambient proxies. Enforce
  independent network egress restrictions in deployment.
- **Credential routing:** Pin a peer grant on the client to the approved
  canonical task URL, recipient root, and transport. Recheck current endpoint
  authorization before each dispatch. Keep peer tokens in the client’s
  protected storage and send only to that exact approved destination.
  Public Card retrieval carries no task bearer. Handle network and TLS errors
  without logging authorization headers, signed payloads, or task contents.
- **Cutover:** Require valid current-controller evidence and strictly newer
  observed location progression. An unapproved or expired endpoint is a hard
  stop. After migration, independently approve the destination and provision
  a destination-bound grant. Never forward the old bearer to the new URL.
  The operator retires the source route and revokes its grants. Record
  observed continuity separately from source retirement.
- **Capacity and privacy:** Bound headers, request and response bytes, read
  and total deadlines, concurrency, and retained rows. Apply registration
  quotas, resolver quotas, bounded discovery queries, and ingress limits per
  peer and instance. Keep the fixed deterministic skill and existing task
  caps. Verify in staging that the global task write lock meets the preview
  target before considering storage redesign. Store and export only the small
  audit fields necessary to diagnose admission, revocation, or dispatch.

## Minimum independent deployment

- Two independently operated instances with distinct public HTTPS origins and
  valid hostname certificates. Use separate PostgreSQL databases, roles,
  credentials, controller keys, and peer grants. Keep PostgreSQL off public
  ingress. Each operator takes responsibility for TLS, credential rotation,
  logs, and source cutover.
- Run the standard production app and Card resolver with the egress controls
  above. Use no loopback exceptions, local fixture entry points, shared
  certificate authority requirements, or shared database credentials.
- Exchange Card URLs and expected roots bilaterally. Discovery searches known
  references and filters authorized peers. A central registry is optional
  infrastructure outside this preview.
- Require time synchronization, ingress quotas, private secret storage, and
  a way to revoke an enrolled peer without restarting workers.

**Proposed measurable staging target per instance:** two application workers,
10 local agents, at most 20 active grants, and 100 known Cards. Allow at most
five registrations per hour, a 30/minute resolution ceiling, and discovery
pages of at most 25 with bounded database work. Limit each grant to 2 task
requests/second sustained with burst 5 and program an instance-wide ingress
ceiling of 10 task requests/second. In a clean-capacity run, complete **5
send/get pairs per second for 60 seconds** across at least five enrolled
peers, with **p95 ≤ 750 ms per call**, **≥ 99% success**, no duplicate tasks or
ownership leakage, and no more than 1,024 retained tasks. Include a separate
noisy-peer run and verify legitimate traffic can still complete. These are
acceptance targets to measure, rather than results from the compatibility spike.
