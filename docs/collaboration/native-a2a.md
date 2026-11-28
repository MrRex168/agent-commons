# Native A2A 1.0 JSON-RPC preview profile

This profile adds a wire adapter to the existing opt-in typed receiver.
A2A 0.3, REST, MCP and legacy Text Statistics retain their current entry
points and contracts. Both typed wire versions use the same PostgreSQL
task ownership and message namespace, grants, quotas, leases, and isolated
worker. The upstream Python SDK is pinned to **1.3.1** for testing only.
The wheel needs no additional runtime dependency.
[A2A 1.0.1 specification release](https://github.com/a2aproject/A2A/releases/tag/v1.0.1) ·
[Python SDK pin](https://github.com/a2aproject/a2a-python/releases/tag/v1.3.1)

## Enable and authorize

1. Apply migration `0021_a2a_task_pagination` before starting updated web and
   worker processes. It adds last-update metadata and an owner index.
   Existing timestamps are initialized from the recorded retention deadline
   and do not represent historical transitions. Downgrade removes the
   metadata and index and preserves task rows. Disable native traffic and
   retire native grants before reverting to an older binary.
2. Enable `AGENT_COMMONS_A2A_NATIVE=true` on the typed receiver. The existing
   execution, PostgreSQL grant, preview quota, HTTPS audience and invite
   settings still apply. Assign existing typed skills to the local recipient.
3. Publish the controller-signed 0.3 Card and the native Card at
   `/api/v1/agents/<name>/agent-card/v1`. For CLI publication add
   `--native-card-url` to the ordinary `publish-card` command. Both Card
   versions use the exact same canonical HTTPS JSON-RPC `/a2a` endpoint.
   The native proof binds its Card URL, protocol version, binding, sovereign
   root, controller, identity/location epochs and the digest of the current
   signed 0.3 location. Renew both proofs on rotation, move or expiry.
4. The receiving operator explicitly approves a typed grant with
   `--scope a2a/1.0` and precise method/skill scopes. Add `--scope tasks/list`
   only when listing is authorized. Discovery and publication confer no
   permission. A 0.3-only grant cannot invoke native operations.
5. Caller operators resolve the 0.3 Card as before and explicitly approve
   the exact native Card URL on the same task origin. Pass
   `--native-card-url` during bound CLI enrollment or set
   `DestinationApproval.native_card_url` in the Python SDK. Existing bound
   grants continue using 0.3. A native SDK binding never silently retries
   via 0.3. A grant with the native scope can still use its individually
   approved methods on a deliberately selected 0.3 client, with the same
   task IDs and owner scope.

The direct SDK refreshes the 0.3 location and reads the exact native Card
anonymously before each dispatch. It verifies both proofs before attaching
the bearer or sending task data. Native Card retrieval and RPC transfer
retain connection-time address policy, TLS hostname verification, numeric
destination pinning, bounded bodies, and disabled redirects. No caching or
quota changes are included.

## Wire contract

Use `A2A-Version: 1.0` explicitly for JSON-RPC. A missing header selects 0.3,
and unsupported or ambiguous versions fail. Existing 0.3 Cards and standard
well-known discovery continue as before. Native discovery uses its signed,
named HTTPS Card path and advertises one 1.0 JSON-RPC interface.

| Native operation | Required scope, in addition to `a2a/1.0` | Semantics |
| --- | --- | --- |
| `SendMessage` | `message/send` | One user data part, using the existing `skill`, `input` and `deadline` contract. Immediate asynchronous reservation. |
| `GetTask` | `tasks/get` | Retrieves an owned task. IDs and typed data artifacts are shared with 0.3. |
| `CancelTask` | `tasks/cancel` | Cancels an owned active task and fences late completion. Repeating a native cancellation of an already canceled task returns that state. |
| `ListTasks` | `tasks/list` | Owner, recipient and skill-scoped cursor pagination ordered by most recent update with an ID tie-breaker. Maximum 20 tasks, or one when artifacts are requested. Filters cover context, state and timestamp. Task events/history are outside this profile. |

There is one grant-bound tenant context. The adapter rejects unsupported
metadata and execution options rather than silently discarding them.
Streaming, subscriptions, push, extended Cards, additional bindings,
arbitrary handlers, and a cross-tenant override are outside this profile.
The native codec preserves exact JSON decimal lexemes until it verifies
integrality and schema bounds. Only the declared integer fields in the
two existing typed schemas are normalized. Fractional values, overflow,
unknown fields, and noncanonical task content remain subject to validation.
Equivalent content reaches the same reservation digest across wire versions.
The SDK supports native delegate, status, cancellation and listing through
the explicit bound destination. Lists omit artifacts in the SDK.

HTTP 401/403 retain authentication/authorization meaning. JSON-RPC uses
`-32001` for inaccessible tasks, `-32002` for noncancelable tasks,
`-32009` for unsupported versions, and standard parse, method and parameter
errors. Existing Agent Commons conflict and quota codes are retained.
HTTP 429 carries `Retry-After` at the receiver. The SDK has no automatic
resubmission after uncertain native delivery.

## Verification matrix

| Boundary | Evidence |
| --- | --- |
| Same reservation and worker | Mixed concurrent 0.3/1.0 submissions, conflict retry, exact numeric validation, and working cancellation with late-completion fencing |
| Scope and tenant isolation | Missing/expired/revoked credentials, native scope, method/skill denial, wrong owner get/list/cancel, foreign cursor, wrong audience and quota metadata |
| Authenticated destination | Live HTTPS SDK publication and enrollment, native signed refresh, stale epoch, signature and version substitutions before bearer dispatch |
| Compatibility | Upstream SDK 1.3.1 local native calls and existing 0.3 compatibility tests, genuine PostgreSQL, operator enrollment and isolated worker. REST/MCP independently covered by the regression suite. |

Reproduce the local wire test with disposable PostgreSQL 16, Linux seccomp,
the existing SDK-spike requirements, and the staging demo DSN:

```bash
AGENT_COMMONS_A2A_SDK_SPIKE=1 pytest -q tests/test_a2a_sdk_spike.py tests/test_a2a_native.py
```

The native wire test uses the upstream client and the Commons SDK with
loopback HTTPS fixture address admission. Independent public deployments,
third-party native receiver interoperability, and the stable 297/300
capacity gate remain separate.
