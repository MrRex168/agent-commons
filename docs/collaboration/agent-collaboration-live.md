# Agent Internet Collaboration Live

Atlas discovers and verifies Nova and Orion, then enrolls with each under a
separate operator approval. Atlas delegates synthetic analysis to Nova,
validates the structured result, and sends it to Orion to generate a report.
Both tasks use A2A 0.3 over HTTPS and the existing typed SDK, with Atlas's
application coordinating the handoff.

Atlas restarts and reads both history records from PostgreSQL. Nova then
moves to another origin and publishes a controller-signed location at
sequence 2. Atlas retains its reference and follow, rejects the old
location, and explicitly enrolls a new grant before delegating again.
Earlier task IDs and artifact digests remain associated with their original
execution origins. Sovereign continuity does not prove exclusive control.
The demo leaves source retirement outside the migration protocol.

## Run locally

Use a Linux environment with Python 3.11+, libseccomp 2, and a disposable
local PostgreSQL 16 instance. The PostgreSQL admin role must be able to
create databases and login roles. Docker can run the database. Run these
commands from the repository root in an activated virtual environment.

```bash
pip install -e '.[dev]'
docker run --rm -d --name ac-collaboration-db \
  -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=demo_admin \
  -e POSTGRES_PASSWORD=local_demo_only \
  postgres:16
export AGENT_COMMONS_DEMO_DATABASE_URL=postgresql://demo_admin:local_demo_only@127.0.0.1:55432/postgres
docker exec ac-collaboration-db pg_isready -U demo_admin
python -m scripts.agent_collaboration_live --evidence /tmp/agent-collaboration-live.json
python -m scripts.agent_internet_dashboard --evidence /tmp/agent-collaboration-live.json
```

Open `http://127.0.0.1:8080/`. Review the 19 checks, the signed 1 → 2 location
change and the three completed tasks. Stop the dashboard with Ctrl+C and
run `docker stop ac-collaboration-db`.

Wait for PostgreSQL to report that it accepts connections before starting.
Use a new output filename each run. The runner provisions and checks four
distinct databases and database roles, creates an ephemeral TLS certificate,
starts independent services, and runs the production typed worker with
seccomp. Temporary databases, roles, certificates, and services are removed
on exit, including after handled failures. No LLM key is needed.

The fixture uses an exact local HTTPS allowlist and ephemeral trust root.
The SDK still uses its pinned transfer, certificate verification, and
redirect, size, and credential restrictions. The production resolver and
public-address policy remain unchanged. The demo provisions accounts before
enabling invite-only grant receivers and uses one operator harness to
exercise separate approval policies. Public network conditions, independent
operator participation, and capacity acceptance require separate evidence.

## Evidence and integration tests

The versioned report contains synthetic names, sovereign fingerprints,
skill scopes, display-only execution origins, task IDs, terminal states,
artifact digests, measured elapsed time, and check outcomes. A failed CLI
run emits a minimal failure record and exits with a nonzero status. It omits
credentials, endpoint URLs, task bodies, and raw artifacts. The dashboard supports this report and the
v0.5 evidence format. It loads only the operator-selected file and does not
connect to an agent or database. Runtime measures one local fixture run,
including temporary database and service setup and cleanup.

Set `AGENT_COMMONS_TEST_DATABASE_URL` to a separate disposable PostgreSQL
database ending in `_test` and keep the demo admin URL above.

```bash
pytest -q tests/test_agent_collaboration_live.py tests/test_typed_execution.py
```

The integration checks run the four-origin demo twice and exercise cleanup
after a failure. The typed execution tests cover cancellation and fencing
of late results, receiver-side authorization, and seccomp deny rules.
The existing v0.6 297/300 capacity gate remains a separate release criterion.
