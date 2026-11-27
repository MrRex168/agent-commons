"""Run the local typed collaboration scenario over disposable HTTPS origins."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx

from agent_commons import outbound_https
from agent_commons.client import CommonsClient, EndpointVerificationError, TypedTaskError
from agent_commons.outbound_a2a import DestinationApproval
from scripts.agent_internet_live import (
    AGENTS,
    FOLLOWS,
    card_path,
    follow,
    location_sequence,
    migrate,
    publish,
    register,
    request,
    require,
    resolve,
    rpc,
)
from scripts.agent_internet_live_support import (
    LOOPBACK,
    Server,
    collaboration_environment,
    isolated_databases,
    make_tls,
)
from scripts.collaboration_live_evidence import REPORT, SUMMARY, Checks, Evidence, FailedReport

LABELS = ("atlas", "source", "orion", "target")
SCOPES = ["message/send", "tasks/get"]
HISTORY = AGENTS + "/collaboration/history"
ENROLLMENT_PATH = re.compile(r"/api/v1/peers/[0-9a-f-]{36}/(?:challenge|redeem)")


@contextmanager
def local_dispatch(nodes, cert):
    """Adapt only address admission and trust root for this demo process."""
    allowed = frozenset(node.url for node in nodes.values())
    transfer = outbound_https._transfer

    def resolve_address(url, _timeout):
        origin = str(url.copy_with(path="/")).rstrip("/")
        if (
            origin not in allowed
            or not (url.path == "/a2a" or ENROLLMENT_PATH.fullmatch(url.path))
            or url.query
        ):
            raise outbound_https.DestinationError("Route is outside the local fixture")
        return LOOPBACK

    def transfer_local(*args, ca_file, **kwargs):
        return transfer(*args, ca_file=str(cert), **kwargs)

    with (
        patch.object(outbound_https, "_resolve", resolve_address),
        patch.object(outbound_https, "_transfer", transfer_local),
    ):
        yield


def local_command(node, skills, module, *args):
    """Use the recipient's environment and existing operator/worker entry point."""
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        env={**node.environment(), **collaboration_environment(skills)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        text=True,
    )
    require(result.returncode == 0, f"Local {module} command failed")
    return result.stdout.strip()


def approve_and_enroll(http, sdk, caller, recipient, node, reference, skill, caller_node, skills):
    caller_ref = resolve(http, node, recipient, caller_node, caller.name)
    require(
        caller_ref["root_fingerprint"] == caller.root
        and caller_ref["endpoint_authorization_status"] == "authorized",
        "Operator could not verify the expected caller",
    )
    output = local_command(
        node,
        skills,
        "agent_commons.cli",
        "peer-approve",
        "--reference",
        caller_ref["id"],
        "--caller-root",
        caller.root,
        "--recipient",
        recipient.agent_id,
        "--skill",
        skill,
        "--scope",
        "message/send",
        "--scope",
        "tasks/get",
    )
    approval = UUID(output.removeprefix("Enrollment approval: "))
    return sdk.enroll_dispatch_peer(
        caller.key,
        destination=DestinationApproval(
            reference_id=UUID(reference["id"]),
            peer_root=recipient.root,
            controller=reference["current_controller_public_key"],
            recipient_agent_id=UUID(recipient.agent_id),
            endpoint=node.url + "/a2a",
        ),
        approval_id=approval,
        caller_root=caller.root,
        scopes=SCOPES,
        skills=[skill],
    )


def expect_error(operation, reason):
    try:
        operation()
    except TypedTaskError as exc:
        require(exc.reason == reason, f"Expected rejection: {reason}")
    else:
        raise RuntimeError(f"Request was accepted, expected: {reason}")


def expect_stale(operation):
    try:
        operation()
    except EndpointVerificationError:
        return
    raise RuntimeError("The SDK accepted the old destination")


def task_arguments(grant, skill, data):
    return dict(
        grant=grant,
        skill=skill,
        data=data,
        message_id=uuid4().hex,
        deadline=datetime.now(UTC) + timedelta(seconds=15),
    )


def finish(sdk, task, grant, node, skills):
    require(task.status == "submitted", "A typed task was not admitted")
    # --once preflights seccomp and executes a real reservation in an isolated child.
    with ThreadPoolExecutor(max_workers=1) as supervisor:
        worker = supervisor.submit(
            local_command, node, skills, "agent_commons.typed_worker", "--once"
        )
        result = sdk.wait_for_task(task.reference, grant=grant, timeout=12, interval=1)
        worker.result()
    require(result.status == "completed" and result.artifact is not None, "Typed task failed")
    return result


def check_discovery(sdk, skill, recipient, node):
    matches = sdk.discover_known(skill=skill).agents
    require(
        len(matches) == 1
        and matches[0].identity_verified
        and matches[0].root_fingerprint == recipient.root
        and matches[0].a2a_url == node.url + "/a2a",
        "Exact signed capability discovery failed",
    )


def history(sdk):
    return {row.task_id: row for row in sdk.list_collaboration_history().records}


def task_evidence(record, task, node, caller, recipient, label):
    require(
        record.task_id == task.id
        and record.execution_origin == node.url
        and record.caller_root == caller.root
        and record.peer_root == recipient.root
        and record.status == task.status == "completed"
        and record.artifact_digest
        == hashlib.sha256(
            json.dumps(
                task.artifact, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        and record.skill == task.reference.skill
        and record.location_sequence == task.reference.location_sequence,
        "History does not match the validated task and execution origin",
    )
    return {
        "origin": label,
        "skill": record.skill,
        "task_id": task.id,
        "state": record.status,
        "artifact_digest": record.artifact_digest,
    }


def demonstrate(http, nodes, cert, key, tls):
    atlas_node, source, orion_node, target = (nodes[label] for label in LABELS)
    cards = [
        node.url + card_path(name)
        for node, name in zip(nodes.values(), ("atlas", "nova", "orion", "nova"), strict=True)
    ]
    start = partial(Server.start, client=http, cert=cert, key=key, cards=cards)
    for node in (atlas_node, source, orion_node):
        start(node)
    start(target, collaboration={})

    atlas = register(http, atlas_node, "atlas")
    visitor = register(http, atlas_node, "history-visitor")
    nova = register(http, source, "nova")
    orion = register(http, orion_node, "orion")
    source_policy = {nova.agent_id: [SUMMARY]}
    orion_policy = {orion.agent_id: [REPORT]}
    start(source, collaboration=source_policy)
    start(orion_node, collaboration=orion_policy)
    for node, agent in ((atlas_node, atlas), (source, nova), (orion_node, orion)):
        publish(http, node, agent, 1)

    nova_ref = resolve(http, atlas_node, atlas, source, nova.name)
    orion_ref = resolve(http, atlas_node, atlas, orion_node, orion.name)
    following = follow(http, atlas_node, atlas, nova_ref)
    transport = partial(httpx.HTTPTransport, verify=tls, trust_env=False)

    with (
        local_dispatch(nodes, cert),
        CommonsClient(atlas_node.url, api_key=atlas.api_key, transport=transport()) as sdk,
        CommonsClient(orion_node.url, api_key=orion.api_key, transport=transport()) as orion_sdk,
    ):
        check_discovery(sdk, SUMMARY, nova, source)
        check_discovery(sdk, REPORT, orion, orion_node)
        nova_grant = approve_and_enroll(
            http, sdk, atlas, nova, source, nova_ref, SUMMARY, atlas_node, source_policy
        )
        orion_grant = approve_and_enroll(
            http, sdk, atlas, orion, orion_node, orion_ref, REPORT, atlas_node, orion_policy
        )
        arguments = task_arguments(nova_grant, SUMMARY, {"values": [2, 3, -1]})
        first = sdk.delegate(**arguments)
        require(sdk.delegate(**arguments).id == first.id, "Duplicate allocated another task")
        expect_error(
            lambda: sdk.delegate(**(arguments | {"data": {"values": [6]}})),
            "message_conflict",
        )
        expect_error(
            lambda: sdk.delegate(
                **task_arguments(
                    nova_grant,
                    REPORT,
                    {
                        "title": "Out of scope",
                        "summary": {
                            "count": 1,
                            "total": 1,
                            "minimum": 1,
                            "maximum": 1,
                        },
                    },
                )
            ),
            "scope_denied",
        )
        analysis = finish(sdk, first, nova_grant, source, source_policy)
        require(
            analysis.artifact == {"count": 3, "total": 4, "minimum": -1, "maximum": 3},
            "Analysis did not produce the expected deterministic artifact",
        )
        report = finish(
            sdk,
            sdk.delegate(
                **task_arguments(
                    orion_grant,
                    REPORT,
                    {"title": "Synthetic analysis", "summary": analysis.artifact},
                )
            ),
            orion_grant,
            orion_node,
            orion_policy,
        )
        require(
            report.artifact
            == {
                "markdown": "# Synthetic analysis\n\n- Count: 3\n"
                "- Total: 4\n- Minimum: -1\n- Maximum: 3"
            },
            "Report did not reflect the validated analysis artifact",
        )

        # Another sovereign caller with the same skill grant still cannot read Atlas's task.
        other_ref = resolve(http, orion_node, orion, source, nova.name)
        other_grant = approve_and_enroll(
            http, orion_sdk, orion, nova, source, other_ref, SUMMARY, orion_node, source_policy
        )
        expect_error(
            lambda: orion_sdk.get_task_status(analysis.reference, grant=other_grant), "task_missing"
        )

        before = history(sdk)
        require(len(before) == 2, "Expected exactly two pre-migration history records")
        tasks = [
            task_evidence(before[analysis.id], analysis, source, atlas, nova, "nova-source"),
            task_evidence(before[report.id], report, orion_node, atlas, orion, "orion"),
        ]
        old_process = atlas_node.process
        start(atlas_node)
        require(
            old_process.poll() is not None and atlas_node.process is not old_process,
            "Atlas service did not restart",
        )
        require(history(sdk) == before, "History changed after Atlas restarted")
        foreign = request(http, "GET", atlas_node, HISTORY, token=visitor.api_key)
        require(
            foreign == {"records": [], "next_cursor": None}, "History leaked to another account"
        )
        request(
            http,
            "GET",
            atlas_node,
            f"{HISTORY}?cursor={before[analysis.id].id}",
            token=visitor.api_key,
            status=404,
        )

        invite_file = cert.parent / "migration.invite"
        local_command(target, {}, "agent_commons.cli", "issue-invite", "--output", str(invite_file))
        try:
            moved = migrate(
                http,
                source,
                target,
                nova,
                invitation=invite_file.read_text(encoding="ascii").strip(),
            )
        finally:
            invite_file.unlink(missing_ok=True)
        target_policy = {moved.agent_id: [SUMMARY]}
        start(target, collaboration=target_policy)
        publish(http, target, moved, 2)
        moved_ref = resolve(http, atlas_node, atlas, target, moved.name)
        require(
            moved_ref["root_fingerprint"] == nova.root
            and moved_ref["identity_verified"]
            and moved_ref["id"] == nova_ref["id"]
            and location_sequence(moved_ref) == 2
            and location_sequence(nova_ref) == 1,
            "Signed location and sovereign reference continuity failed",
        )
        (retained,) = request(http, "GET", atlas_node, FOLLOWS, token=atlas.api_key)
        require(
            retained["id"] == following["id"]
            and retained["remote_reference_id"] == moved_ref["id"]
            and retained["remote_a2a_url"] == target.url + "/a2a",
            "Follow did not retain the newly verified location",
        )
        resolve(http, atlas_node, atlas, source, nova.name, status=409)
        expect_stale(lambda: sdk.get_task_status(analysis.reference, grant=nova_grant))
        # Explicit negative control over fixture HTTPS, never via SDK routing.
        request(
            http,
            "POST",
            target,
            "/a2a",
            token=nova_grant.grant.token.get_secret_value(),
            body=rpc("tasks/get", {"id": str(analysis.id)}),
            status=401,
        )

        fresh = approve_and_enroll(
            http, sdk, atlas, moved, target, moved_ref, SUMMARY, atlas_node, target_policy
        )
        require(fresh.grant.grant_id != nova_grant.grant.grant_id, "Destination reused a grant")
        expect_stale(lambda: sdk.get_task_status(analysis.reference, grant=fresh))
        missing = request(
            http,
            "POST",
            target,
            "/a2a",
            token=fresh.grant.token.get_secret_value(),
            body=rpc("tasks/get", {"id": str(analysis.id)}),
        )
        require(missing.get("error", {}).get("code") == -32001, "Destination exposed a source task")
        continued = finish(
            sdk,
            sdk.delegate(**task_arguments(fresh, SUMMARY, {"values": [4, 5]})),
            fresh,
            target,
            target_policy,
        )
        require(
            continued.artifact == {"count": 2, "total": 9, "minimum": 4, "maximum": 5},
            "Migrated Nova returned unexpected analysis",
        )
        after = history(sdk)
        require(
            len(after) == 3 and all(after[key] == row for key, row in before.items()),
            "Migration changed existing history",
        )
        tasks.append(
            task_evidence(after[continued.id], continued, target, atlas, moved, "nova-target")
        )
        local_command(
            target,
            target_policy,
            "agent_commons.cli",
            "peer-revoke",
            "--grant",
            str(fresh.grant.grant_id),
        )
        expect_error(
            lambda: sdk.get_task_status(continued.reference, grant=fresh), "credential_unavailable"
        )

    return {
        "nodes": ("atlas", "nova-source", "orion", "nova-target"),
        "identities": {"atlas": atlas.root, "nova": nova.root, "orion": orion.root},
        "advertised_skills": {"nova": (SUMMARY,), "orion": (REPORT,)},
        "authorized_skills": {
            "nova": tuple(nova_grant.grant.skills),
            "orion": tuple(orion_grant.grant.skills),
        },
        "tasks": tasks,
        "migration": {
            "reference_id": moved_ref["id"],
            "follow_id": retained["id"],
            "location_before": location_sequence(nova_ref),
            "location_after": location_sequence(moved_ref),
        },
    }


def run(admin_dsn: str) -> Evidence:
    began = monotonic()
    with TemporaryDirectory(prefix="agent-collaboration-live-") as directory:
        cert, key, tls = make_tls(Path(directory))
        with isolated_databases(admin_dsn, LABELS) as databases, ExitStack() as resources:
            http = resources.enter_context(
                httpx.Client(verify=tls, trust_env=False, follow_redirects=False, timeout=10)
            )
            nodes = {
                label: Server.provision(resources, database)
                for label, database in databases.items()
            }
            data = demonstrate(http, nodes, cert, key, tls)
    return Evidence.model_validate(
        data
        | {
            "runtime_seconds": round(monotonic() - began, 3),
            "checks": {name: "pass" for name in Checks.model_fields},
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="Write to a new evidence file")
    args = parser.parse_args()
    dsn = os.environ.get("AGENT_COMMONS_DEMO_DATABASE_URL")
    if not dsn:
        parser.error("Set AGENT_COMMONS_DEMO_DATABASE_URL to a disposable local PostgreSQL URL")

    def emit(report):
        payload = report.json_report()
        if args.evidence:
            with args.evidence.open("x", encoding="utf-8") as stream:
                stream.write(payload)
        print(payload, end="")

    try:
        report = run(dsn)
    except Exception as exc:
        # Evidence and user output never include request or response bodies.
        try:
            emit(FailedReport())
        except OSError:
            raise SystemExit("Could not write collaboration evidence") from None
        detail = exc.reason if isinstance(exc, TypedTaskError) else type(exc).__name__
        raise SystemExit(f"Collaboration demo failed: {detail}") from None
    try:
        emit(report)
    except OSError:
        raise SystemExit("Could not write collaboration evidence") from None


if __name__ == "__main__":
    main()
