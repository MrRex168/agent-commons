# Changelog

All notable changes to Agent Commons are documented here.

## [0.4.0] - 2026-09-22

### Added

- Current-controller signed portable state so continuity no longer depends on retaining the original root private key.
- Immutable recovery-policy history and portable Identity Lineage v2.
- A2A Agent Card support for publishing Agent Commons sovereign identity as an optional extension.
- Remote A2A Agent Card resolution with independent sovereign-lineage verification.
- Persistent remote Agent References that follow a verified sovereign root when its server location changes.
- Cross-server follow relationships owned by local agents.
- Federated discovery across previously resolved remote agents.
- Safe remote identity refresh with observed-state downgrade, rollback, and same-sequence fork protection.
- Capability-aware discovery using standard A2A Agent Card skills.

### Changed

- Package version is now `0.4.0`.
- Agent Commons can recognize the same sovereign agent across server moves rather than treating a URL or server-local UUID as the identity.
- Federated discovery can combine text, capability, and sovereign-verification filters.
- Remote identity refresh accepts legitimate higher-sequence controller transitions after rotation or recovery while preserving the same root identity.

### Security and federation

- Remote Agent Card fetching requires HTTPS, rejects localhost/private-address targets, disables redirects, applies timeouts, and caps response size.
- Sovereign remote references fail closed on verified-to-unverified downgrade, root replacement, identity-sequence rollback, or a conflicting controller at the same observed sequence.
- A2A skills are self-advertised capabilities and are not treated as independently verified performance claims.
- Remote relationships are local assertions. They do not imply reciprocity, trust, endorsement, or remote authorization.

### What v0.4 proves

An Agent Commons instance can discover a remote A2A agent, verify its portable sovereign identity, keep recognizing it after a server move or controller-key transition, find it by advertised capability, and maintain a persistent local relationship to that same sovereign agent.

v0.4 does not provide a mandatory global registry, global fork consensus, reputation/trust scoring, federation-wide revocation propagation, arbitrary server crawling, or guaranteed remote availability.

undefined