# Open Agent Network implementation plan

Target: operator-enrolled external developer preview. Keep REST `/api/v1`,
MCP, sovereign formats, and A2A 0.3 compatibility stable. The compatibility
spike establishes a working upstream-client pairing. Milestones 3A and 3B
implement receiving-side grants and pinned outbound transport. Independent
staging and deployment load validation remain pending.
[Compatibility results](a2a-compatibility.md) · [Onboarding guide](onboarding.md) · [Security and deployment](security-architecture.md)

| Milestone | Deliverables and dependencies | Required regression coverage |
| --- | --- | --- |
| **2 · External Agent Onboarding and SDK** | Add a thin Python client and CLI around the existing registration, identity challenge, profile, and Card APIs. Generate and retain keys on the client. Document installed-package onboarding against the pinned external SDK. Add opt-in registration admission with single-use invitations and an additive migration for invite digests, expiry, and consumption. Keep the public preview closed until Milestone 3 egress controls land. | Clean install, client-side signing, no key leakage, expired and concurrent invitation rejection, consistent admission across REST aliases and MCP, and legacy development compatibility. |
| **3A · Secure Peer Enrollment** | Implemented operator approvals, challenge redemption, database-backed grants, rotation, and revocation. [Operator runbook](peer-enrollment.md). | Enrollment, expiry, scope, concurrency, multi-process revocation, and legacy isolation. |
| **3B · Safe Dispatch** | Pin public HTTPS connections for Card fetching and direct client task calls. Require an exact destination-bound grant and refresh the signed location before dispatch. [Dispatch guide](outbound-a2a.md). | Wrong root/controller/recipient/scope, expiry, stale and moved endpoints, DNS rebinding, TLS hostname mismatch, disabled redirects and proxies, bounded transfers, and credential isolation. |
| **3C · Preview Capacity** | Enforce opt-in shared PostgreSQL quotas for registration, resolution, and task admission. Bound remote discovery with SQL filters and keyset pagination. [Capacity runbook](preview-capacity.md). | Alias and MCP coverage, quota rollover and contention, fail-closed storage errors, noisy-peer isolation, and bounded discovery. |
| **4 · Independent Network Validation** | Enroll two independently operated public HTTPS staging instances and run the supported workflow with upstream A2A 0.3 compatibility. Upgrade a populated v0.5 database, validate real network egress, run the load gates, and publish a redacted reproducibility report. No schema change expected. | Clean install onboarding, production Card resolution on both hosts, real send/get, ownership and idempotency under load, fresh destination credentials after migration, preserved root/reference/follow, stale route rejection, and full v0.5 regression coverage. |

**Dependencies and blockers.** Milestone 2 can proceed with the documented
A2A 0.3 compatibility client. External rollout depends on verified egress
controls, authoritative grant revocation, and two separate operators with
public HTTPS staging origins. A 1.0-only partner requires an explicit versioned
adapter decision and new interop coverage. The existing loopback demonstration
and the SDK spike cannot satisfy the independent staging gate.

**Release gate.** An external developer completes onboarding without source
edits, obtains an approved scoped grant, discovers the approved peer and
completes real tasks across both origins. Staging meets the measured load and
security gates, passes migration and legacy regression checks, and shows safe
cutover after a signed location change. Keep public self-enrollment, an inbox,
automatic crawling, and server-hosted explorer work out of the preview.
