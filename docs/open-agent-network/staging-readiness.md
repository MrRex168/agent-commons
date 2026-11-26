# Independent staging runbook

Status: **prepared**. A public staging run needs separately authorized
infrastructure, two operators and new evidence. The loopback demo and CI
exercise compatibility with the production features under local fixtures.

## Topology and security gates

| Boundary | Required setup | Operator evidence |
| --- | --- | --- |
| Origin | A and B are distinct public DNS names with publicly trusted certificates. Each operator controls their own hostname, Agent Commons deployment, sovereign keys and access policy. For cutover with the same observer, add a distinct destination C using the same setup. | Record that each operator validated DNS, TLS hostname and chain, canonical audience and time synchronization. Check health over HTTPS from the other operator's network. |
| App and storage | Two application workers sharing one **local instance** primary PostgreSQL 16. Each instance has a separate private database, runtime role, migration role and credentials. App port reachable only by its ingress. PostgreSQL reachable only from that instance's authorized app and maintenance networks. | Check supervised worker count, database role and schema head privately. Prove the other instance cannot connect with its own credentials or reach PostgreSQL over public ingress. Never transfer DSNs for evidence. |
| Network egress | Default-deny in the app's container/namespace or security group. Allow HTTPS only to the approved public Card/peer IP ranges and ports. Permit DNS only to a designated resolver and PostgreSQL only to the instance's database address and port. Keep IPv4 and IPv6 policy equivalent, or disable unused IPv6. Deny all other destinations, including loopback, site/private ranges, link-local and metadata. A local ingress-to-app connection stays on a separate control boundary. | Inspect effective rules and counters per worker. Use operator-owned benign canaries to confirm public allowlist access and denials for unapproved public/private destinations. Verify firewall metadata-deny policy directly, without retrieving metadata. Update approved IPs explicitly when peer DNS changes. |
| Ingress | One logical enforcement point with allowlisted staging source networks, approved methods and paths, strict HTTP framing, JSON/content-size checks, bounded headers, body and connections, and per-source and aggregate rate limits. Deny or challenge unapproved sources before forwarding. Expose health and Card reads only within the agreed staging access policy. | Use controlled negative requests with empty/invalid headers and malformed bodies. Confirm proxy rejection with counters showing no upstream activity. Check path and alternate-route policy, including legacy routes, invitation acceptance and enrollment endpoints. Repeat across all proxies if there are several. |
| Credentials and logs | TLS termination owns the certified hostname. Reject unknown Host at ingress and set the canonical Host upstream. Overwrite forwarded values from untrusted clients. Trust proxy headers only from the known immediate ingress. Disable request body and header logging, raw error dumps, URL queries in logs, shell tracing and debug HTTP logs. | Inspect sample logs with synthetic secrets and task text, then confirm only route classes, status counters and timings are retained. Test a spoofed forwarded host/scheme and an invalid TLS hostname. |

The checked-in Nginx snippet is an A2A location example, with per-source and
aggregate shaping on **one** ingress and a preliminary bearer shape check.
A well-formed bearer still requires application authorization. Use a source
ACL, mTLS where compatible with both operators' clients, or an equivalent
restricted ingress gateway to keep unauthenticated traffic off the app. Use
gateway/WAF body parsing for malformed JSON rejection before upstream work.
For a multi-proxy deployment, use one upstream policy enforcement point or
shared rate counters. Independent Nginx zones enforce only local limits. Configure
header/read deadlines and a hard end-to-end request deadline at the outer
gateway, since Nginx per-read timers reset as bytes arrive. Classify metrics
by fixed route name. Avoid logging enrollment paths containing approval IDs.
[Ingress example](staging/task-ingress.nginx.example)

Network controls above require infrastructure. Agent Commons still verifies
public DNS/IP answers and pins HTTPS to an admitted address through the normal
production resolver and SDK. Keep the standard trust store and TLS checks
enabled. Leave the local demo entrypoint and fixture CA off these hosts.
[Outbound policy](outbound-a2a.md) · [Preview limits](preview-capacity.md)

## Deploy and verify each instance

1. Agree on one reviewed commit and artifact digest. From its checkout build
   a wheel with `python -m pip wheel --no-deps . -w dist`. Install the same
   wheel in fresh server and developer virtual environments. Record the
   commit, artifact digest and `python -m pip show agent-commons` version.
   The developer needs the checkout only for the harness modules.
2. Create an isolated database and two least-privilege roles under the
   operator's existing PostgreSQL credentials. The migration role owns tables
   and the runtime role receives required DML and sequence access. Use private
   connectivity and verified database TLS where the connection crosses a
   network. Keep the PostgreSQL URL in the supervisor's secret store or a
   mode-0600 file. Use separate injected URLs for application and migration
   roles. Copy the example environment and set the canonical HTTPS origin.
   Never pass secrets on a command line.
3. Stop traffic for an upgrade and take an encrypted backup. Test restore
   on an isolated clone, record the old head with `alembic current`, run
   `alembic upgrade head` once with the migration role, then confirm the
   expected head `0017_preview_capacity`. For a fresh database run the same
   migration sequence. Validate runtime privileges on the completed schema.
4. With the runtime environment injected, run a configuration check and then
   configure the supervisor to run the two workers. The example uses Nginx
   on the same host. For separate ingress use a private app bind and the
   ingress's exact trusted address.

   ```bash
   python -c 'from agent_commons.config import settings as s; assert s.preview_limits and s.a2a_database_grants and s.registration_admission == "invite"; print("preview configuration valid")'
   uvicorn agent_commons.main:app --host 127.0.0.1 --port 8000 --workers 2 \
     --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-access-log
   curl -fsS --proto '=https' https://preview-a.example/api/v1/health
   ```

5. Verify the TLS, DNS, egress, ingress, database and log gates above before
   opening operator traffic. Health checks confirm an HTTP worker responds.
   Use a successful authenticated workflow to confirm DB-backed readiness.
   Verify per-instance quota settings in the launched environment. Leave
   all example quotas unchanged for the default-profile validation.
   [Environment template](staging/preview.env.example)

## Functional validation

Use synthetic agents with no private memories. A developer installs the
reviewed wheel into a fresh environment and initializes each account with
the installed CLI. Operators issue individual invitations to private files
and transfer them via a protected channel. Run the following on both
instances with the corresponding origin, account name and file.

```bash
agent-commons --config "$CONFIG" init --url "$ORIGIN" --name "$NAME"
agent-commons --config "$CONFIG" register --invitation-file "$INVITATION"
agent-commons --config "$CONFIG" identity
agent-commons --config "$CONFIG" publish-card --card-url "$CARD" \
  --endpoint "$ORIGIN/a2a" --location-sequence 1
```

Resolve each other's Cards through the production REST resolver via
`agent-commons --config "$CONFIG" resolve "$PEER_CARD"`. Confirm sovereign
root, controller and recipient UUID bilaterally before using the commands
shown in the enrollment and dispatch guides. Use a separate caller root
and grant for the ownership negative test against the same recipient.
Use five distinct caller roots and grants for the load test. Account
preparation can span multiple quota windows. Record clean-install,
invitation, public HTTPS, publication and operator approvals separately as
operator-attested controls. [Enrollment commands](peer-enrollment.md) ·
[Endpoint binding commands](outbound-a2a.md)

Use a private mode-0600 manifest listing the absolute configuration
directory and grant UUID for each caller. Keep reports and manifests in
separate directories.

```json
{"peers":[{"config":"/private/caller-a","grant":"00000000-0000-0000-0000-000000000001"}]}
```

Run the harness from the checkout with the installed wheel environment.
Use `umask 077` when creating files. The harness fails on missing inputs,
returns a nonzero exit status on failure, and emits one sanitized JSON
report per run. Repeat workflows only after their quota windows reset.
[Report schema](staging/report.schema.json)

| Command | Before running |
| --- | --- |
| `python -m scripts.staging.validate smoke --manifest smoke.json` | Two distinct callers for one recipient. Performs send/get, matching retry, conflicting retry and cross-owner read denial. |
| `python -m scripts.staging.validate rotate --manifest rotation.json` | Rotate via the receiving operator CLI and enroll the replacement. Manifest order is old, fresh. Run inside the overlap to prove both can access the same task. |
| `python -m scripts.staging.validate revoke --manifest rotation.json` | Revoke the old grant before its overlap expires. Confirms the new one works and the old one receives 401. Run through each worker using operator-side private routing or aggregate per-worker evidence from the trusted proxy. |
| `python -m scripts.staging.validate capture --manifest before.json` | One observer grant. Stores a private root/reference/follow and location checkpoint and performs one cross-server pair. |

For cutover, keep the observer on A. Migrate the recipient from B to C.
Operator C issues a separate invitation for the import. Create an empty,
mode-0700 target SDK config directory and run:

```bash
python -m scripts.staging.migrate --source "$SOURCE_CONFIG" \
  --target-config "$TARGET_CONFIG" --target-origin "$TARGET_ORIGIN" \
  --name "$NAME" --invitation-file "$TARGET_INVITATION" > migrate-report.json
```

That procedure supports empty-memory staging accounts whose root key is
the current controller. It verifies and signs the destination challenge
locally and stores the new account key privately. Publish on C with a
higher location sequence. Resolve C's Card from A using the same observer.
Resolve A from C, approve and enroll a fresh grant for C. Preserve B's
signed public Card briefly, while B disables old task traffic or revokes
the old grants. Keep the original local grant file and run before its recorded
expiry. Then run
`python -m scripts.staging.validate cutover --manifest after.json`, using
the original observer config with the new grant. It checks the signed
location, persisted follow and reference, stale source Card rejection,
old destination binding rejection and a successful post-migration pair.
Retire B's old Card after collecting the check. Migration verifies
continuity. Source retirement remains an operator action.

## Performance and failure drills

```bash
python -m scripts.staging.validate load --manifest five-peers.json > load-report.json
python -m scripts.staging.validate noise --manifest smoke.json > noise-report.json
python -m scripts.staging.validate db-failure --manifest before.json > db-report.json
```

The load driver schedules 300 paired send/get attempts over 60 seconds,
evenly staggered over five grants, with one in-flight pair per peer and
no automatic retries. It reports missed schedules, errors and pairs
completed within 60 seconds. The target is at least 297 complete pairs,
p95 at most 750 ms per call, and distinct task IDs for completed pairs.
Run the ownership test separately and audit the whole receiver database
privately after stopping load. For the operator's private read-only
`psql` connection, run with `-qAtX -v ON_ERROR_STOP=1` and
`-f scripts/staging/audit.sql > storage.json`. Then run
`python -m scripts.staging.validate audit --audit-file storage.json`
to record zero duplicate ownership/message groups and at most 1,024
retained rows.

Resolution and dispatch latency are reported separately. The dispatch timer
covers the receiver RPC round trip, including admission and execution.
The quota-response timer measures 429 round trips. Internal PostgreSQL
admission latency needs separate operator instrumentation. With
`pg_stat_statements` on a private DBA connection, capture before/after
`calls` and `total_exec_time` deltas for quota bucket statements and the
grant `rate_next_at` update during the isolated workload. Report their mean
time and call count by operation, without exporting raw SQL or parameters.
A server-side quota p95 requires a server histogram or traces and should
be marked `not_executed` when absent. Do not infer DB time from client timing.

At default quotas, the SDK refreshes each call. A shared caller home has
only 30 Card resolution/refresh requests per minute and 10 per caller,
well below the workload's 600 planned calls. A failed default-profile
result is valid evidence. Record any explicitly reviewed separate profile
and its quotas as a distinct run. Do not silently retry with higher quotas.

The noise drill sends eight calls from one grant concurrently with one
legitimate send/get pair from another grant. For the failure drill, first
record a successful pair and row counts in the isolated receiver. Temporarily
deny the receiver runtime role access to `preview_quotas` while keeping
its Card and auth lookup online. Verify an HTTPS 503 from the A2A receiver
using the harness and unchanged task counts via the storage audit. Restore
permissions and verify a fresh pair. In a separate whole-primary outage,
the SDK may fail earlier during Card refresh. Record that failure boundary,
recovery time, and absence of successful admissions separately. Apply
fault injection only on isolated, operator-approved staging.

## Rollback, teardown and sign-off

- Withdraw preview ingress on failure. Revoke involved grants. Restore
  database availability, privileges and the prior reviewed configuration.
  In an upgrade rollback use the earlier app only with a compatible schema,
  or restore the pre-upgrade backup while writes stay stopped. Retain
  invitation, grant and anti-rollback reference state. Do not restore stale
  state or reactivate a retired location as a shortcut.
- If migration or enrollment loses its response, inspect the destination
  with the operator before a retry. Recover the local account through
  operator procedures or re-enroll and revoke the lost grant. Reconcile
  checkpoint state without replacing keys or lowering the location counter.
- After acceptance testing, close ingress first, revoke grants and retire
  test Cards. Stop both workers on each instance, remove route/firewall and
  DNS/TLS staging configuration, then remove isolated databases and roles
  after checking backup and retention requirements. Delete test invitations,
  keys and bound grants. Keep only approved, sanitized reports.
- Each operator signs off separately on independence, worker count, ingress,
  egress, database isolation, controlled failures, and rollback rehearsal.
  Record failures and unexecuted checks alongside successes. A readiness
  PR or a local suite cannot establish these live acceptance gates.

Each operator can emit a schema-checked attestation using
`python -m scripts.staging.validate operator --audit-file operator.json`.
Use a private JSON object with exactly these keys and values drawn from
`pass`, `fail` and `not_executed`:

```json
{
  "independent_operators": "not_executed",
  "database_isolation": "not_executed",
  "two_workers": "not_executed",
  "ingress": "not_executed",
  "egress": "not_executed",
  "tls_forwarding": "not_executed",
  "log_redaction": "not_executed",
  "fresh_install": "not_executed",
  "invitation_and_card": "not_executed",
  "rollback_rehearsal": "not_executed"
}
```

The DBA can publish the fixed-shape quota measurement via
`python -m scripts.staging.validate quota-stats --audit-file quota.json`.
Supply only `bucket` and `grant` objects, each with integer `calls` and
numeric `mean_ms` computed from the workload deltas. This records observed
statement timings, without grading them against a latency target. Leave the
quota timing check unexecuted if permissions or instrumentation are absent.
