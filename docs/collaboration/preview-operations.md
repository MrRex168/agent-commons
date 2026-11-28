# Preview distribution, upgrade, and recovery

## Build for maintainer review

Select the exact reviewed commit after merge. Record its full SHA. The proposed
tag `v0.7.0a1` must point at that commit, and its `pyproject.toml` must declare
`0.7.0a1`. A PR-head or CI test-merge artifact is useful for verification.
Prepare the publication artifact again from the approved tag target.

Use Python 3.11+ with pip, Git, and tar. From the repository checkout, build
in an isolated environment using the pinned backend. Use an empty output path.

```bash
python -m venv /tmp/ac-preview-build
/tmp/ac-preview-build/bin/python -m pip install 'hatchling==1.29.0'
/tmp/ac-preview-build/bin/python scripts/build_preview_wheel.py \
  --commit "$APPROVED_COMMIT" --output /tmp/ac-preview-artifacts
```

The builder archives tracked source at that SHA, uses its commit timestamp
as `SOURCE_DATE_EPOCH`, and checks the wheel metadata, package modules and
entry points. Preserve the recorded toolchain when comparing reproducible
builds. The output consists of `agent_commons-0.7.0a1-py3-none-any.whl`,
`SHA256SUMS`, and `release-build.json`. The manifest binds the wheel and
checksum to the source SHA, version, and proposed tag. CI stores these three
files as review artifacts. Only the maintainer publishes selected assets.
Database dumps, environments, fixture reports, and configuration secrets
are outside the release asset set.

## Verify the exact wheel

Obtain the wheel and checksum files from the approved build or this
repository's GitHub Assets after approval. Confirm the manifest SHA matches
the reviewed source and proposed tag. A checksum verifies the downloaded
bytes against that manifest. It is not a signature.

```bash
cd /tmp/ac-preview-artifacts
sha256sum --check SHA256SUMS
python -m venv /tmp/ac-preview-install
/tmp/ac-preview-install/bin/python -m pip install ./agent_commons-0.7.0a1-py3-none-any.whl
/tmp/ac-preview-install/bin/agent-commons --version
/tmp/ac-preview-install/bin/python -c 'from importlib.metadata import version; from agent_commons.main import app; from agent_commons.client import CommonsClient; assert app.version == version("agent-commons") == "0.7.0a1"; assert callable(CommonsClient.delegate)'
```

Use a fresh environment and the explicit wheel path. Python dependencies
resolve separately. Never install the unrelated PyPI project by name.
The wheel includes the application, SDK, worker, and CLI. Run Alembic from
the matching source checkout, which also contains demo and dashboard scripts.

## Upgrade or take a cold backup

1. Record the database revision, artifact checksum, canonical HTTPS audience,
   per-recipient skill assignments, and storage retention settings. Protect
   database credentials, controller and recovery keys, TLS material, static
   peer config, and client-side API and bearer secrets separately. A database
   dump preserves token hashes and approvals. It cannot restore a lost bearer
   or private signing key. Credentials still expire normally after a restore.
2. Block ingress that can submit tasks or change state. Leave workers running
   until the read-only query
   `SELECT count(*) FROM a2a_tasks WHERE state IN ('submitted','working');`
   returns zero on a preview database. On v0.5 at revision `0014_a2a_tasks`,
   tasks are synchronous. Wait for in-flight HTTP operations to finish. If
   active preview work cannot drain, stand down and investigate. No forced
   interruption or automatic task resubmission is part of this procedure.
3. Stop workers and all old web writers. Create a private PostgreSQL custom
   dump for each database with PostgreSQL tools compatible with the server.
   Use a protected connection service or password file and a mode-0700 backup
   directory. Examples assume libpq connection settings injected privately.

   ```bash
   umask 077
   pg_dump --format=custom --file=agent-commons.dump
   pg_restore --list agent-commons.dump >/dev/null
   ```

4. Rehearse restoration into a new, empty, isolated PostgreSQL 16 database.
   Provision the intended role, ownership and privileges. Preserve ownership
   and ACLs if the original roles exist. The commands below omit ownership and
   ACL restoration for an isolated target with independently provisioned local
   roles. Apply and verify the intended production grants separately before use.

   ```bash
   pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
     --dbname "$EMPTY_RESTORE_DATABASE" agent-commons.dump
   alembic current
   alembic upgrade head
   alembic current
   ```

   Run Alembic with `AGENT_COMMONS_DATABASE_URL` pointing to the verified
   restore target. A v0.5 upgrade goes from `0014_a2a_tasks` through
   `0020_collaboration_history`. Retain pre-upgrade backups. Downgrade
   deletes preview history, typed tasks, typed grants and approvals across
   the respective boundaries. A return to v0.5 with preserved pre-upgrade
   state requires restoring its backup using the old build. Later writes
   require separate recovery decisions.
   [Downgrade matrix](release-readiness.md#upgrade-and-rollback)

5. Start web and worker from the same verified wheel with the intended
   audience, database connection and skill policy. The worker preflights
   seccomp at startup and refuses operation if isolation is unavailable.
   Confirm health, identity and follows, legacy and typed task retrieval,
   owner history, and an authorized synthetic task before reopening ingress.
   If an endpoint or controller changes, review signed location progression
   and re-enroll as required. Never assume a dump authorizes a new endpoint.

## Reproduce the disposable rehearsal

With PostgreSQL 16, `pg_dump`, `pg_restore`, Python development dependencies
and Linux seccomp, set `AGENT_COMMONS_DEMO_DATABASE_URL` to a disposable local
admin URL and `AGENT_COMMONS_TEST_DATABASE_URL` to a distinct disposable test
database. Run:

```bash
AGENT_COMMONS_PREVIEW_RESTORE=1 pytest -q tests/test_preview_restore.py
```

The fixture starts on the published v0.5 migration schema and seeds its
legacy task layout. It restores a pre-upgrade backup separately and verifies
that the restored v0.5 schema upgrades and serves the original legacy task.
It upgrades the source databases, enrolls a typed grant, drains a submitted
task through the bounded worker, and restores two backups into separate
databases and role configurations. It checks original credentials, active
grant, signed identities, follow, tasks, history and fresh legacy and typed
work after recovery. The ephemeral origins are preserved for the rehearsal.
Production credentials and databases are outside the test.
