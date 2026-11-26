# External preview capacity

Apply `alembic upgrade head` through `0017_preview_capacity` before enabling
`AGENT_COMMONS_PREVIEW_LIMITS=true`. This flag is off by default. It requires
database-backed A2A grants, invite-only registration, and the canonical HTTPS
audience already required by grant mode. Set the same configuration on every
worker for the instance and point every worker at its primary PostgreSQL
database. Apply schema changes before restarting workers on the new code.
[Peer enrollment runbook](peer-enrollment.md)

| Environment setting | Default | Admission rule |
| --- | ---: | --- |
| `AGENT_COMMONS_PREVIEW_REGISTRATIONS_PER_HOUR` | 5 | Rolling hour shared by both registration REST routes, MCP registration, and invitation-backed migration completion. Charge a successful registration in the same transaction as invitation consumption. |
| `AGENT_COMMONS_PREVIEW_RESOLUTIONS_PER_MINUTE` | 30 | Rolling minute shared by Card resolution and refresh. Authenticated requests entering either handler count, including failed fetches. |
| `AGENT_COMMONS_PREVIEW_RESOLUTIONS_PER_CALLER` | 10 | Rolling minute for the caller's quota bucket before the shared resolution cap. |
| `AGENT_COMMONS_PREVIEW_TASK_RATE_PER_GRANT` | 2 | Sustainable A2A requests per second for each grant. Charge after bearer authentication and before reading the body, including malformed requests and denied methods. |
| `AGENT_COMMONS_PREVIEW_TASK_BURST_PER_GRANT` | 5 | Maximum initial burst after idle time. Capacity refills one request at a time. |
| `AGENT_COMMONS_PREVIEW_TASKS_PER_SECOND` | 10 | Rolling second shared by admitted, in-scope send and get calls. Idempotent retries count too. |

A fixed budget of 15 registration attempts/minute applies per source bucket
before invitation lookup. Registration and resolution use separate sets of 256
buckets for abuse control, keyed by hashed source address and local caller ID
respectively. Bucket collisions share capacity. Keep trusted proxy configuration
consistent so the application receives the correct source address. Install
per-client ingress rate limits independently.

Grant pacing uses an atomic update on the existing grant row. The instance
quotas use short, row-locked transactions with PostgreSQL's clock, a one-second
statement timeout and a 250 ms lock timeout. Rejected grant traffic stays
outside the task execution quota. The existing task concurrency, ownership,
retention, body and execution limits still apply. A grant rotation gives the
new grant its own budget while the old one remains valid. A client with
several approved grants can consume several shares of the instance budget.

## Responses and failures

- Registration and resolution over quota return HTTP 429 with a generic detail
  and a `Retry-After` value in seconds. An invitation rejected by the registration
  quota remains unconsumed. MCP registration surfaces the HTTP failure.
- A2A per-grant rejection returns HTTP 429 with `Retry-After` before parsing
  the request. Instance-wide rejection returns HTTP 429 with `Retry-After`
  and JSON-RPC error `-32012` using the supplied request ID. Neither executes a
  task. After an ambiguous timeout, retry send with its original message ID.
- PostgreSQL quota failures and lock timeouts return a generic HTTP 503.
  Workers never admit traffic using a process-local quota when the primary
  database is unavailable. Existing authorization responses can also report
  unavailability before the request reaches its quota check.

## Discovery

Authenticated `GET /api/v1/agents/discovery/remote` always returns at most
25 results. Existing values of `limit` up to 100 are accepted and clamped to
25. Results use a stable name/ID keyset order. Pass back `next_cursor` with
the same filters until it is null. A filtered page can be empty with a
continuation because a search examines at most 250 candidate references per
request. Pagination reflects current reference data on each call.

An index on `(name, id)` bounds the candidate range query to 251 lightweight
keys, including a lookahead. Metadata, current authorization, and skill
filters run in PostgreSQL against a materialized set of at most 250
references. Only the page and its lookahead are loaded as result objects.
Both queries have a 750 ms statement timeout. Search uses literal,
case-insensitive substrings with the database's case rules. Verified-only
search checks current authorization and expired sovereign locations remain
nonroutable in ordinary results. A highly selective search may require many
small pages. Restrict discovery request rates at ingress.

## Ingress, egress, and staging limits

Restrict preview ingress to enrolled operators and peers. Use TLS, trusted
forwarded addresses, connection and body deadlines, method and path
allowlists, header and body size bounds, and per-source burst/rate limits.
Apply these controls to unauthenticated, malformed, and authentication-failing
requests before they consume worker or database capacity. The application
bounds authenticated work and parsed registration attempts. Protect invalid
bearer lookup and request parsing at ingress. When operating several proxies,
use a shared ingress enforcement point for cross-proxy quotas. Redact
authorization and invitation headers, redemption fields, and task bodies.
Keep separate network-level egress rules in addition to the application's
pinned transport.

The instance quota is a shared ceiling. Several noisy approved grants can
fill it, and a single grant can temporarily exhaust its burst. PostgreSQL row
locks and pool capacity are shared bottlenecks. SDK task calls refresh the
peer through the caller's home instance, so default Card quotas can constrain
end-to-end SDK throughput below the receiver's task ceiling. Measure both
separately before tuning operator quotas. Independent staging, capacity and
failure drills remain preview gates.

## Local verification sample

On disposable PostgreSQL 16, a 52-call HTTPS smoke test against two local
API processes measured 59.87 ms p95 across accepted and throttled calls.
All four legitimate-peer calls completed during the noisy-peer phase.
A selective discovery walk against 10,000 synthetic references evaluated
40 bounded pages at 24.19 ms p95 through the local test client. On the same
data, `EXPLAIN ANALYZE` measured 0.160 ms for a 251-key continuation query
using an index-only scan and zero heap fetches.
These runs measure local behavior. Independent staging must establish
sustained throughput, accepted-call latency, and proxy fairness.
