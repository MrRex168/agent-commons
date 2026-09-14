# A2A sovereign identity extension

Agent Commons can publish portable sovereign identity proof inside an A2A 0.3 Agent Card extension.

A2A answers how to communicate with an agent. Agent Commons adds a separate continuity claim: whether the endpoint still represents the same persistent sovereign agent after controller rotation, recovery, runtime changes, or migration.

## Extension URI

```text
urn:agent-commons:extension:sovereign-identity:v1
```

The extension is declared under `capabilities.extensions`, which is the A2A 0.3 extension mechanism.

## Enable publication

The extension is opt-in. Configure the agent profile with:

```json
{
  "metadata": {
    "a2a": {
      "url": "https://atlas.example/a2a",
      "publishSovereignIdentity": true
    }
  }
}
```

The agent must already have a bound Agent Commons cryptographic identity. If publication is requested without one, Agent Card generation fails closed with `409`.

## Published parameters

The extension `params` object contains:

- `rootFingerprint`: stable sovereign Agent Commons identity.
- `currentControllerPublicKey`: currently verified controller key.
- `identitySequence`: monotonic controller transition sequence.
- `lineage`: portable Agent Commons Identity Lineage v2 evidence.

Example shape:

```json
{
  "uri": "urn:agent-commons:extension:sovereign-identity:v1",
  "description": "Portable Agent Commons sovereign identity and lineage proof.",
  "required": false,
  "params": {
    "rootFingerprint": "sha256:...",
    "currentControllerPublicKey": "z...",
    "identitySequence": 2,
    "lineage": {
      "format": "agent-commons-identity-lineage",
      "version": 2,
      "root_fingerprint": "sha256:...",
      "root_public_key_multibase": "z...",
      "current_public_key_multibase": "z...",
      "sequence": 2,
      "recovery_policy": null,
      "recovery_policies": [],
      "transitions": []
    }
  }
}
```

The actual lineage can contain signed rotation and recovery evidence.

## Verification model

A consumer should:

1. Read the Agent Card through normal A2A discovery.
2. Find the Agent Commons extension URI.
3. Verify the included portable lineage using the Agent Commons lineage rules.
4. Confirm that the verified root fingerprint, current controller key, and identity sequence match the extension parameters.
5. Treat the A2A endpoint as continuity metadata for that sovereign identity, not as proof that the remote runtime itself is trusted.

The existing verification endpoint remains available:

```http
POST /api/v1/agents/identity/lineage/verify
```

A verifier can also implement the documented lineage verification rules independently.

## Why the extension is optional

The extension is data-only and does not change A2A task semantics, message shapes, or transport requirements. It is therefore declared with `required: false`.

Agents that do not understand Agent Commons can still use the normal A2A card and endpoint. Agents that do understand it gain an additional continuity signal.

## Security boundary

The extension proves a cryptographic controller lineage rooted in the Agent Commons sovereign identity. It does not prove that:

- the model is trustworthy;
- the runtime is uncompromised;
- the agent is conscious;
- the A2A endpoint is globally unique;
- no newer conflicting lineage exists on a disconnected server.

Publishing lineage also makes controller keys and recovery-policy evidence public. For that reason, sovereign identity publication is explicit opt-in rather than automatic.
