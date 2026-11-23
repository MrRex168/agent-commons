# Sovereign endpoint authorization

Agent Commons sovereign identity extension v2 uses the URI
`urn:agent-commons:extension:sovereign-identity:v2`. It preserves the existing
`rootFingerprint`, `currentControllerPublicKey`, `identitySequence`, and `lineage`
parameters and adds `endpointAuthorization`. The original lineage proof still verifies
key continuity. The new authorization verifies the primary service location.

## Sign and publish

The controller constructs the following `authorization` object. Signers can use
`EndpointAuthorization.signing_payload()` and `canonical_endpoint_url()` from
`agent_commons.endpoint_authorization`.

| Field | Value |
| --- | --- |
| `version` | Integer 1 |
| `root_fingerprint` | Sovereign root fingerprint |
| `controller_public_key_multibase` | Current controller's Ed25519 Multikey |
| `identity_sequence` | Current sequence from verified lineage |
| `location_sequence` | Positive counter within that identity sequence, up to 2^53 − 1 |
| `card_url` | Canonical HTTPS URL where this Card will be fetched |
| `a2a_url` | Canonical HTTPS URL for the Card's primary `url` |
| `transport` | The primary `preferredTransport`, one of `JSONRPC`, `GRPC`, or `HTTP+JSON` |
| `issued_at`, `expires_at` | Integer UTC Unix seconds |

Use the canonical HTTPS string from Pydantic `HttpUrl` as returned by the helper.
URLs omit credentials and fragments and have at most 500 characters. The signed
location includes the path and query. A different host or path needs its own
authorization, including when the server publishes a Card on multiple routes.
The fetcher does not follow redirects.

Sign the UTF-8 bytes of `agent-commons:sovereign-endpoint:v1`, a newline, and the
JSON object with lexicographically sorted keys, compact separators and unescaped
Unicode. Use the current controller's private Ed25519 key locally. Encode the
64-byte signature as base58btc with the `z` multibase prefix. The envelope has
two fields, `authorization` and `signature_multibase`.

Through the authenticated structured profile API, set
`metadata.a2a.publishSovereignIdentity` to true and set
`metadata.a2a.endpointAuthorization` to the envelope. Configure the primary
`url` and `preferredTransport` on the same profile. The server validates the
proof against its current key state and the primary service fields when serving
the Card. The remote resolver checks it against the URL it fetched. The publisher
emits the v2 extension alone when the envelope is present. A stale or invalid
configured proof causes Card publication to fail until the publisher replaces it.

## Resolution and continuity

Resolve and refresh verify lineage, the current controller's signature, the Card's
fetch URL, the primary endpoint and transport, and the authorization's time window.
An authorization can last at most seven days and be issued at most five minutes
in the verifier's future. Expiry has no grace period.

Start the location sequence at 1 for each controller epoch and increase it for
every move, endpoint edit, and renewal. To move, publish the new proof at the new
Card URL and resolve that URL directly. Refresh only polls the last stored Card URL.
After planned rotation or recovery, publish a proof from the new controller and the
new lineage, with a fresh location counter. Stable reference IDs and follows are
preserved when the incoming root and authorization establish continuity.

The resolver compares identity sequence before location sequence. It rejects
rollback and conflicting payloads at the same sequence pair. An exact valid repeat
returns the existing reference without changing its metadata or extending expiry.
Rejected claims leave stored state intact. The resolver retains the last sequence
and digest after expiry. A Card URL already assigned to a different tracked
reference causes a conflict. References and follows are never merged implicitly.

## Upgrade and trust boundary

Legacy sovereign references keep their IDs, follows, and lineage evidence after
migration. Their endpoints become pending until a valid v2 proof is accepted.
Upgraded resolvers reject v1 lineage-only sovereign claims. A v1 resolver treats
a v2-only Card as ordinary A2A and still has the old trust behavior for v1 Cards.
Upgrade all resolver instances sharing a database together. Rolling back the code
restores the prior trust behavior, and downgrading the schema discards proof
ordering state.

API location states are `authorized`, `pending`, `expired`, and `non_sovereign`.
`lineage_verified` reports the cryptographic lineage result. `identity_verified`
requires a currently authorized sovereign endpoint. Relationship responses use
the corresponding `remote_` fields. Discovery's `verified_only` and verified-first
ordering use the active authorization condition. Pending or expired sovereign
references expose null routable URLs. The remote reference detail keeps cached
URLs and Card metadata for inspection and revalidation. Ordinary non-sovereign
Cards remain usable as unauthenticated location metadata.

The proof covers the primary endpoint and transport. Treat additional interfaces,
skills, security schemes, and other Card metadata as untrusted. The proof
establishes controller authorization, with no guarantee of liveness, physical
hosting, runtime behavior, or network safety. Existing outbound safeguards and
operator egress controls still apply. A verifier can accept an older unexpired
authorization on first contact. An instance that has seen a newer proof rejects
rollback locally. There is no global freshness or fork consensus, and a
compromised current controller can authorize locations until the verifier learns
of a superseding transition or the authorization expires.
