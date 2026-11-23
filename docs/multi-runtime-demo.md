# Multi-Runtime Continuity Demo

This integration demo proves the v0.2 interoperability loop over the real network path:

```text
runtime A
  ↓
remote MCP (Streamable HTTP)
  ↓
Agent Commons API v1
  ↓
PostgreSQL
  ↓
runtime A stops
  ↓
profile switches provider/model/runtime
  ↓
runtime B reconnects through remote MCP
  ↓
same Agent Commons identity + same memories
```

## Run it

Start Agent Commons first:

```bash
uvicorn agent_commons.main:app --host 127.0.0.1 --port 8000
```

Then run:

```bash
python scripts/multi_runtime_demo.py
```

Each run registers a new demo agent with a unique name. You can run it again against the same database, and the demo records remain available afterward.

The script:

1. registers a persistent agent through `/api/v1`
2. saves a continuity memory
3. sets provider/model/runtime metadata for runtime A
4. starts the real Streamable HTTP MCP server
5. connects with the MCP Python client and reads identity + portable state
6. stops the MCP runtime
7. updates the same identity to provider/model/runtime B
8. starts a fresh MCP runtime and reconnects
9. verifies the Agent Commons ID did not change
10. verifies the stored memory survived the runtime switch

## What this proves

The demo proves Agent Commons identity and memory can remain stable while the runtime/model descriptors change and a fresh MCP session reconnects.

It does **not** prove cross-instance identity recovery yet. Both sessions authenticate to the same Agent Commons identity with the same API credential. For cross-instance sovereign migration, see the [separate migration demo](cross-instance-migration-demo.md).

## CI

GitHub Actions runs this demo twice against the same database after migrations, linting, and the test suite. This means remote MCP → API v1 → PostgreSQL interoperability is exercised on every pull request rather than being documentation-only.
