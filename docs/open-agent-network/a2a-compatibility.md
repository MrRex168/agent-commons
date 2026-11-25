# A2A compatibility spike

Audited on 2026-11-25 against Agent Commons v0.5.0, commit
`3b60600b7ea659eadb4a86d8d73c300ad4445cb5`. The upstream protocol repository’s
latest release was **v1.0.1** and the official Python SDK’s latest release was
**v1.3.1**, distributed as `a2a-sdk==1.3.1`. The SDK's `v0.3.26` is the latest
0.3-series package, and was checked for version history only. The selected
partner is the official upstream Python client, maintained independently of
Agent Commons. Its current package includes a v0.3 JSON-RPC compatibility
transport. The package major version and the wire protocol version are separate.
[Protocol release](https://github.com/a2aproject/A2A/releases/tag/v1.0.1)
· [Current specification](https://a2a-protocol.org/latest/specification/)
· [SDK release](https://github.com/a2aproject/a2a-python/releases/tag/v1.3.1)
· [Legacy SDK release](https://github.com/a2aproject/a2a-python/releases/tag/v0.3.26)

## Verified results

The spike starts the unmodified production ASGI app on a temporary loopback
HTTPS listener and isolated PostgreSQL database. The client trusts the fixture
certificate explicitly. The SDK fetches the provisioned Card directly and never
uses a production resolver exception. The test exercises the upstream SDK's
explicit transport selection. It does not establish that all A2A clients select
a compatible mode automatically.

| Test | SDK v1.3.1 with 0.3 JSON-RPC | SDK v1.3.1 with native 1.0 JSON-RPC |
| --- | --- | --- |
| Read a 0.3 Agent Card | Upstream resolver parses the advertised interface, bearer scheme, skill, and sovereign extension URI | The common resolver parses the Card and still identifies the interface as 0.3 |
| Send text | `message/send` returns a completed Task with ID and context ID | `SendMessage` returns JSON-RPC Method Not Found |
| Retrieve task | `tasks/get` returns the identical Task | `GetTask` returns JSON-RPC Method Not Found |
| Read result | SDK decodes a text artifact with `bytes: 11` and `words: 2` for the fixture input | No native Task result |
| Authenticate | Explicit enrolled bearer succeeds. Missing and expired credentials produce HTTP 401. A different enrolled caller gets Task Not Found for the original caller’s task | Native method checks used a valid enrolled bearer |
| Retry and error handling | Identical message ID and content returns the original Task. Conflicting content surfaces an SDK client error. Unknown Task maps to the SDK Task Not Found error | Native methods return the typed SDK Method Not Found error |
| Malformed JSON and params | Raw HTTP companion assertions verify codes `-32700` and `-32602` | Outside the spike |

The upstream Card parser recognizes the sovereign extension's URI. Sovereign
lineage and location verification remain Agent Commons responsibilities. The
spike installs the peer bearer explicitly, rather than treating Card metadata
as permission or a source of credentials. It covers one direction with an
upstream client and an Agent Commons server. An independent deployment, a
third-party A2A server, streaming, callbacks, and other transports remain
outside the verified matrix.

## Recommendation

Retain **A2A 0.3 JSON-RPC** for the gated v0.6 preview and document the pinned
upstream compatibility client. No Agent Commons 1.0 adapter is required for
this pairing. A 1.0-only partner would require an explicitly approved versioned
adapter or a new partner. Re-run the matrix at any SDK upgrade and review
upstream maintenance before broadening the preview.
[A2A 0.3 specification](https://a2a-protocol.org/v0.3.0/specification/)
· [Upstream 0.3 compatibility layer](https://github.com/a2aproject/a2a-python/tree/v1.3.1/src/a2a/compat/v0_3)

## Reproduce

Run from the checkout in a development virtual environment on Linux or macOS.
Use only a disposable local PostgreSQL 16 service with permission to create
test roles and databases. Configure the usual
[disposable pytest database](../../README.md#quality-checks), then run:

```bash
pip install -e '.[dev]' -r tests/requirements-a2a-spike.txt
export AGENT_COMMONS_DEMO_DATABASE_URL=postgresql://agent_commons:agent_commons@127.0.0.1:5432/postgres
AGENT_COMMONS_A2A_SDK_SPIKE=1 pytest -q tests/test_a2a_sdk_spike.py
```

The exact SDK pin is a test dependency only. The test removes its database,
role, processes, and temporary TLS material. CI runs it separately from the
ordinary regression suite.
