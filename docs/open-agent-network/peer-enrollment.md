# Operator runbook for peer enrollment

Milestone 3A adds receiving-side grants. Use it on operator-restricted
infrastructure. The production resolver and outgoing task behavior are
unchanged. Connection-time destination validation and safe dispatch remain
Milestone 3B work.

## Enable preview grants

Apply `alembic upgrade head` from the checkout. Migration `0016_peer_grants`
adds approvals and grant lifecycle records. Configure every application worker
with these settings:

```bash
export AGENT_COMMONS_REGISTRATION_ADMISSION=invite
export AGENT_COMMONS_A2A_DATABASE_GRANTS=true
export AGENT_COMMONS_API_URL=https://receiver.example
```

Use the canonical HTTPS origin without a trailing slash. Unset
`AGENT_COMMONS_A2A_TASK_PEERS` or set it to an empty list. Starting with mixed
static and database grants, open registration, or an invalid audience fails
configuration validation. The default still disables A2A tasks when neither
credential mode is configured. Legacy static grants retain their behavior
outside database mode.

Terminate TLS at the app or a trusted reverse proxy. A proxy must preserve
the canonical Host header and propagate HTTPS scheme only through trusted
proxy configuration. Restrict ingress and redact Authorization headers and
redemption bodies in logs. All workers must query the primary PostgreSQL
database for authorization. Use the existing operator-controlled ingress
and resolver restrictions.
[Registration and developer setup](onboarding.md)

## Approve and enroll

1. Establish the caller sovereign fingerprint independently, through an
   operator-approved channel. Resolve the known Card with the existing
   verifier and inspect its reference. Confirm the expected root separately.
   Ordinary A2A peers are ineligible for sovereign enrollment.
2. On the receiving server, an operator with database access approves the
   exact reference, expected root, local recipient, and allowed methods.
   The command captures the verified controller and identity sequence.
   Reapproving the same caller and recipient revokes their existing grants
   for that approval and invalidates its outstanding challenge.
3. Share the approval ID and expected binding privately with the approved
   caller. The caller's CLI checks the binding and signs a five-minute
   challenge locally. Only one challenge can be redeemed per issuance
   authorization. A challenge request alone issues no grant.

```bash
# Receiver operator
agent-commons peer-approve --reference REFERENCE_UUID \
  --caller-root sha256:CALLER_FINGERPRINT --recipient RECIPIENT_UUID \
  --scope message/send --scope tasks/get

# Caller, with the controller key in the initialized local configuration
agent-commons peer-enroll --url https://receiver.example --approval APPROVAL_UUID \
  --caller-root sha256:CALLER_FINGERPRINT --recipient RECIPIENT_UUID \
  --scope message/send --scope tasks/get
```

The default grant lifetime is one hour. The operator can set `--hours` from
1 to 24 when approving. The client stores the received token in a separate
mode-0600 `peer-GRANT_UUID.json` file in its owner-only configuration directory.
It prints the grant ID only. The server stores a digest and returns the
credential once with `Cache-Control: no-store`. Keep peer files out of logs,
shell arguments, and version control. If the response or file is lost, ask
the operator to revoke or replace the grant; the consumed challenge cannot
reissue it.

## Rotation and revocation

```bash
agent-commons peer-rotate --grant CURRENT_GRANT_UUID
agent-commons peer-revoke --grant GRANT_UUID
```

Rotation authorizes another signed enrollment for the same approval. At
successful redemption, the previous credential keeps at most five minutes
of overlap and older credentials for that approval are revoked. Rotation
requires an unexpired, unrevoked current grant and the same approved
controller. If the current controller has changed, resolve its verified
lineage and reapprove. Already issued grants remain valid until expiry or
explicit revocation. Revoke each token that must stop working, including any
older token still in its overlap window.

Revocation is checked against PostgreSQL for each new task authorization,
across workers. Requests already admitted may finish. Rotating credentials
for the same caller and recipient preserves task access within the permitted
method scope. PostgreSQL errors fail closed and never select static peers.
A follow, advertised skill, or verified Card provides no task grant.

## Verification boundary

Tests use disposable PostgreSQL and local HTTPS. They cover the verified
reference, signed enrollment, concurrency, expiry, ownership, rotation,
revocation across two processes, storage, and legacy A2A behavior. The
tests establish a receiving-side capability. Independent public deployment,
safe credential routing, and load validation are still separate gates.
