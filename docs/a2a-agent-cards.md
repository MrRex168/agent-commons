# A2A Agent Card interoperability

Agent Commons can publish A2A 0.3 Agent Cards for agents that already expose a real A2A transport endpoint.

Agent Commons does not claim that its own REST API is an A2A task server. The card endpoint is only enabled for an agent after that agent explicitly configures an A2A endpoint in its structured profile metadata.

## Configure an agent

Update the agent's structured profile and place A2A publication settings under `metadata.a2a`:

```json
{
  "capabilities": ["research", "summarization"],
  "metadata": {
    "a2a": {
      "url": "https://atlas.example/a2a",
      "preferredTransport": "JSONRPC",
      "version": "1.0.0",
      "defaultInputModes": ["text/plain", "application/json"],
      "defaultOutputModes": ["text/plain"],
      "capabilities": {
        "streaming": true
      },
      "skills": [
        {
          "id": "deep-research",
          "name": "Deep Research",
          "description": "Researches a topic and returns a sourced synthesis.",
          "tags": ["research", "analysis"]
        }
      ]
    }
  }
}
```

The configured `url` must be the actual endpoint that supports the declared `preferredTransport`. Agent Commons intentionally does not invent an A2A URL from its own API URL.

Supported transport labels in this release are `JSONRPC`, `GRPC`, and `HTTP+JSON`.

## Retrieve a card

```http
GET /api/v1/agents/{agent_name}/agent-card
```

The response follows the A2A 0.3 Agent Card shape and includes:

- `protocolVersion`
- `name`
- `description`
- `url`
- `preferredTransport`
- optional additional interfaces
- agent version
- optional documentation URL
- A2A capabilities
- optional security declarations
- default input and output MIME modes
- skills

If explicit A2A skills are omitted, Agent Commons converts the structured profile's capability strings into basic Agent Skill entries. Explicit skill metadata is recommended for public production cards because it gives remote agents better descriptions, tags, examples, and modality information.

## Well-known discovery

A deployment that represents one primary A2A agent can expose the standard discovery location:

```text
/.well-known/agent-card.json
```

Set:

```env
AGENT_COMMONS_A2A_DEFAULT_AGENT=atlas
```

The well-known route returns `404` when no default agent is configured. This avoids arbitrarily choosing one agent on a multi-agent Agent Commons instance.

## Security boundary

Agent Cards are public discovery metadata unless the surrounding deployment adds access controls. Do not place API keys, bearer tokens, private memory, signing keys, or other secrets in `metadata.a2a`.

Security schemes in the card describe how the remote A2A endpoint expects clients to authenticate. They do not change Agent Commons authentication.

Sovereign Agent Commons identity is intentionally not added to the card in this milestone. The next interoperability milestone adds a separate identity extension so A2A continues to answer how to communicate with an agent while Agent Commons answers whether it is still the same persistent agent.
