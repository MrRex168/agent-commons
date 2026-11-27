# Collaboration developer preview readiness

The latest published GitHub release is **v0.5.0**, and the checkout still
builds as **0.5.0**. This review covers the source changes after v0.5.0,
including PRs #54–#64 and the dashboard and developer guide changes.
[Published release](https://github.com/MrRex168/agent-commons/releases/tag/v0.5.0)

## Recommendation

**Choose B, an experimental self-hosted developer preview.** Prepare
`0.7.0a1` as a proposed opt-in prerelease for operators evaluating the
local workflow and integrating their own runtimes. Before publishing,
choose a verified distribution channel, approve the distinct package metadata,
rerun packaging and release checks
on the proposed artifact, and rehearse backup restoration on a copy of the
operator's database. Keep access invitation-based and operator-enrolled.
The package version in this milestone remains at 0.5.0.
[Draft preview notes](../releases/v0.7.0a1-draft.md)

| Option | Recommendation |
| --- | --- |
| A · Continue development without a release | Keep using source checkouts if the prerelease packaging or restore rehearsal remains incomplete. |
| B · Publish an explicitly experimental preview | Provides a reproducible starting point for local evaluation with two deterministic handlers and a restricted operator model. |
| C · Wait for stable acceptance | Required before a stable release or claims of independently validated public deployment. |

## Upgrade and rollback

Back up PostgreSQL and controller/recovery material separately. Rehearse
against a copy. Close preview ingress, drain typed work, and stop workers and older web writers,
apply `alembic upgrade head`, and verify revision `0020_collaboration_history`.
Start matching web and worker builds with the same database, canonical HTTPS
audience, and recipient skill policy. Verify health and isolation before
enabling preview traffic. Older binaries cannot safely serve a database with
typed tasks. Use a reviewed backup and restore procedure for rollback when
history, typed tasks, grants, or invitations must survive.

| Revision | Upgrade and downgrade consequences |
| --- | --- |
| 0015 | Adds one-time invitation storage. Downgrade removes invitations. |
| 0016 | Adds approvals and revocable grants. Downgrade removes both tables. |
| 0017 | Adds shared quotas and discovery pagination index. Downgrade removes quota state, the index, and the grant rate field. |
| 0018 | Adds skill scopes and guards against broadened grants. Downgrade deletes typed grants and approvals before removing those scopes. |
| 0019 | Adds typed task execution columns and claim index. Downgrade deletes typed tasks and preserves legacy Text Statistics rows. |
| 0020 | Adds private caller history. Downgrade drops history records. |

## Operating boundaries

- Default task execution is opt-in. Typed execution requires invitation
  admission, PostgreSQL grants, shared preview quotas, a canonical HTTPS
  audience and an explicit per-recipient skill policy. Start the Linux
  worker with libseccomp 2. Its isolation preflight fails closed.
- Keep controller, recovery, account, invitation and peer credentials
  separate. Protect local files and redact application and proxy logs.
  Production peers require TLS, independent fingerprint verification,
  pinned public addresses, egress controls and ingress limits. The demo
  uses an isolated loopback fixture and private trust root.
- Receivers bound admission, request size, execution time, child memory,
  concurrency and retention. The worker allows two CPU seconds and
  128 MiB address space per child, four working tasks across supervisors,
  and 32 active tasks. Task storage is capped at 1,024 retained rows,
  with 24-hour expiry. Caller history expires after 30 days and is capped
  at 512 rows per owner and 8,192 per instance.
- Cancellation or worker failure fences late results. Timeouts and
  abandoned attempts may end in failure. Revocation prevents future
  grant use. A migration requires fresh enrollment and operator-owned
  retirement of the source.
- REST remains `/api/v1`. MCP and the legacy Text Statistics path remain
  supported. The upstream SDK compatibility test exercises A2A 0.3
  JSON-RPC Text Statistics. Typed tests exercise the Agent Commons SDK.
  Native A2A 1.0, arbitrary handler execution and public self-enrollment
  remain outside scope.

[Worker policy](typed-task-execution.md) · [SDK errors and retention](typed-sdk-history.md) · [A2A compatibility results](../open-agent-network/a2a-compatibility.md) · [Staging runbook](../open-agent-network/staging-readiness.md)

## Release blockers and acceptance evidence

| Area | Finding and required action |
| --- | --- |
| Security and compatibility | Local regression covers identity, destination binding, grant revocation, isolation and existing interfaces. Treat mixed-version writers, missing seccomp, or absent ingress/egress enforcement as deployment blockers. Rehearse restore before promoting a deployment. |
| Distribution provenance | PyPI's `agent-commons` project links to the unrelated `t54-labs/agent-commons` repository. The current GitHub release has no wheel assets. A preview needs an approved distribution channel, such as a wheel attached to this repository's GitHub prerelease. Install from this checkout today. [PyPI metadata](https://pypi.org/pypi/agent-commons/json) |
| Developer experience | Use the tested quickstart and SDK guide. A published preview still needs distinct prerelease wheel metadata and a clean install of that artifact. Platform prerequisites must be installed by the operator. |
| Independent network validation | Outstanding. Run the functional pilot across separate operators and public HTTPS origins, with production DNS, TLS and network controls and no fixture exceptions. |
| Capacity and performance | Stable acceptance remains **297 of 300** completed pairs within 60 seconds, with p95 at most 750 ms and the existing ownership and storage checks. Default refresh-per-call consumes a shared 30 resolutions/minute against 600 scheduled calls on a shared home. The local functional demo and the separate 10-pair pilot do not satisfy that gate. |
| Experimental limitations | Both installed handlers are deterministic. Evidence is a local snapshot without independent attestation. It does not establish exclusive identity control, third-party typed interoperability, or automatic source retirement. |

Use the PR's CI run and its recorded artifacts for test counts, wheel and
Docker verification, compatibility checks, and demo measurements. The
project is MIT licensed. Follow the existing contribution and private
security reporting instructions.
[License](../../LICENSE) · [Contributing](../../CONTRIBUTING.md) · [Security policy](../../SECURITY.md)
