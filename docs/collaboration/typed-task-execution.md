# Bounded typed task execution

Typed execution is opt-in on the existing A2A 0.3 JSON-RPC `POST /a2a`.
Apply Alembic migration `0019_typed_task_execution` and upgrade all web and
worker processes before enabling it. Stop and drain typed execution before
downgrading. Downgrade deletes typed task records before removing the
discriminator, and preserves existing Text Statistics records. Never run an
older web binary against a database holding typed tasks.

## Operate the worker

Enable `AGENT_COMMONS_COLLABORATION_EXECUTION=true` alongside
`AGENT_COMMONS_A2A_DATABASE_GRANTS=true` and
`AGENT_COMMONS_PREVIEW_LIMITS=true`. Configure the fixed contracts per
recipient with `AGENT_COMMONS_COLLABORATION_SKILLS`. Configure the worker with
the same audience, database and skill policy as the web process.

Supervise `python -m agent_commons.typed_worker` as a separate Linux service.
It requires a kernel that supports seccomp filters and libseccomp 2. The
Docker image includes the system library. Use a dedicated service identity,
no local peer credential directory, and OS egress policy allowing the
supervisor only its PostgreSQL connection. The executor is a separate child
with an empty environment, closed inherited descriptors except stdin,
stdout and stderr, and no database or peer credentials. A kernel syscall
allowlist is installed before reading peer input. It disallows opening or
modifying files, creating sockets, creating processes, and executing a
new program. The worker refuses startup if its isolation check fails.
There is no in-process fallback.

Each child has a 128 MiB address-space limit, two CPU seconds, an 8 KiB
output file cap and a wall deadline enforced by the supervisor. Process
death kills the child. The worker executes one task at a time, and the
database admits at most four working tasks across supervisors. The active
queue cap is 32, and typed and legacy tasks share the existing 1,024-row
retention cap and preview task quotas. Operators should alert on worker
health when execution is enabled. A stopped worker can leave reservations
pending until a sweeper runs.

## A2A request and lifecycle

Enroll using a v2 typed grant with the required skill and methods.
`tasks/cancel` is a distinct scope available only for typed grants. Use the
existing signed endpoint and destination verification before sending the
bearer. The receiver derives ownership from the database grant and validates
the trusted HTTPS request audience. Legacy `a2a_` grants and typed `a2c_`
grants use separate task paths.

Submit `message/send` with one A2A data part. The part has exactly
`skill`, `deadline`, and `input`. For example, the following is the
`message` inside JSON-RPC `params`. Supply an actual near-future UTC
deadline within the contract and grant budgets.

```json
{
  "kind": "message",
  "role": "user",
  "messageId": "example-summary-1",
  "parts": [{
    "kind": "data",
    "data": {
      "skill": "analysis.summary@1",
      "deadline": "2026-11-27T12:00:15Z",
      "input": {"values": [2, 3, -1]}
    }
  }]
}
```

Submission commits a `submitted` Task. Poll its ID with `tasks/get` under
the same owner and recipient, with an active grant for the skill. On success
the Task is `completed` with a bounded data artifact containing
`{"count":3,"total":4,"minimum":-1,"maximum":3}`. Identical authorized replay
returns the existing Task within retention, even after the original
execution deadline. A changed canonical digest conflicts.

A claim is `working` until it finishes. Authorization is checked again
before launching the executor. Revocation observed before launch yields
`rejected`. Revocation after launch blocks subsequent bearer operations.
Queued cancellation immediately prevents execution. Running cancellation
commits `canceled`, fences the result, and the supervisor terminates its
child when the next state check observes cancellation. Completion and
cancellation race on the task row. The first terminal commit wins.
Cancellation makes no claim about reversing side effects.

A timeout, invalid output, process failure or ambiguous abandoned attempt
ends as `failed`. The sweeper fences abandoned attempts at their original
deadline. Database errors prevent admission or publication and never put
a claimed task back in the queue. Terminal tasks clear the input and attempt
token. Results and deduplication metadata expire after 24 hours. Terminal
artifacts remain untrusted content. Only the two fixed, side-effect-free
handlers are executable. No arbitrary local plugins, peer delegation, or
SDK task API expansion is included.

## Reproduce locally

With a disposable PostgreSQL 16 database and the development dependencies,
run the existing signed enrollment fixture through the A2A ingress and
launch the production worker as a separate subprocess:

```bash
AGENT_COMMONS_TEST_DATABASE_URL="$DISPOSABLE_POSTGRES_URL" \
  pytest -q tests/test_typed_execution.py::test_typed_execute_and_replay
```

The test asserts the submitted and completed task states and the exact
structured outcome above. It uses a local HTTPS-shaped ASGI test request
and real PostgreSQL and seccomp. Independent public HTTPS verification and
the v0.6 capacity gate remain separate.
