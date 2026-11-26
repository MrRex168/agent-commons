"""Operator-invoked production staging checks. No private-network test overrides."""

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent_commons.cli import LocalAccount, _directory, _read, _write
from agent_commons.client import CommonsClient, CommonsError
from agent_commons.outbound_a2a import BoundGrant
from agent_commons.outbound_https import request
from agent_commons.peer_protocol import https_audience

OPERATOR_CHECKS = (
    "independent_operators",
    "database_isolation",
    "two_workers",
    "ingress",
    "egress",
    "tls_forwarding",
    "log_redaction",
    "fresh_install",
    "invitation_and_card",
    "rollback_rehearsal",
)
CHECKS = (
    "preflight",
    "public_origins",
    "send_get",
    "idempotency",
    "ownership",
    "rotation",
    "revocation",
    "signed_migration",
    "preserved_relationship",
    "fresh_destination",
    "stale_location",
    "load",
    "functional",
    "functional_setup",
    "signed_dispatch",
    "functional_deadlines",
    "functional_quotas",
    "no_duplicate_tasks",
    "retention",
    "noisy_peer",
    "database_failure",
    "quota_db_timing",
    *OPERATOR_CHECKS,
)
SAMPLE = "preview staging statistics"
PAIR_COUNT = 300
DURATION_SECONDS = 60
FUNCTIONAL_PAIR_COUNT = 10
QUIET_SECONDS = 61
DEADLINE_MS = 750
RELATIONSHIPS = "agents/relationships"


class InputError(ValueError):
    pass


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: str
    grant: UUID


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peers: list[Entry] = Field(min_length=1, max_length=10)


class Audit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    retained: int = Field(ge=0)
    duplicate_groups: int = Field(ge=0)


class QuotaTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    calls: int = Field(gt=0)
    mean_ms: float = Field(ge=0)


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant: UUID
    audience: str
    card_url: str
    root: str
    reference: UUID
    follow: UUID
    location: int


class Report:
    def __init__(self, mode: str):
        self.data = {
            "schema_version": 1,
            "mode": mode,
            "checks": dict.fromkeys(CHECKS, "not_executed"),
            "metrics": {},
        }

    def set(self, check: str, passed: bool) -> bool:
        self.data["checks"][check] = "pass" if passed else "fail"
        return passed


class MeasuredClient(CommonsClient):
    """Measure SDK Card refresh and remaining task RPC work separately."""

    def __init__(self, origin, *, api_key):
        super().__init__(origin, api_key=api_key)
        self.refresh_ms: list[float] = []
        self.rpc_ms: list[float] = []
        self.call_ms: list[float] = []
        self.success_ms: list[float] = []
        self.quota_reply_ms: list[float] = []
        self.refresh_status: dict[int, int] = {}
        self._resolved = False

    def _request(self, action, method, path, **kwargs):
        try:
            return super()._request(action, method, path, **kwargs)
        except CommonsError as exc:
            if action == "Peer refresh" and exc.status is not None:
                self.refresh_status[exc.status] = self.refresh_status.get(exc.status, 0) + 1
            raise

    def _verified_destination(self, approved):
        start = monotonic()
        try:
            result = super()._verified_destination(approved)
            self._resolved = True
            return result
        finally:
            self.refresh_ms.append((monotonic() - start) * 1000)

    def _task(self, grant, method, params):
        self._resolved = False
        start = monotonic()
        quota = False
        succeeded = False
        try:
            result = super()._task(grant, method, params)
            succeeded = True
            return result
        except CommonsError as exc:
            quota = exc.status == 429
            raise
        finally:
            elapsed = (monotonic() - start) * 1000
            self.call_ms.append(elapsed)
            if succeeded:
                self.success_ms.append(elapsed)
            if self._resolved:
                rpc = max(0.0, elapsed - self.refresh_ms[-1])
                self.rpc_ms.append(rpc)
                if quota:
                    self.quota_reply_ms.append(rpc)


def load_peer(entry: Entry):
    with _directory(entry.config) as directory:
        account = LocalAccount.model_validate_json(_read("account.json", directory=directory))
        grant = BoundGrant.model_validate_json(
            _read(f"peer-{entry.grant}.json", directory=directory)
        )
    if (
        not account.api_key
        or account.origin != grant.home_origin
        or entry.grant != grant.grant.grant_id
        or account.origin == grant.destination.audience
        or grant.expires_at <= datetime.now(UTC)
        or set(grant.grant.scopes) != {"message/send", "tasks/get"}
    ):
        raise InputError
    return account, grant


def validate_pair(account, grant):
    for origin in (account.origin, grant.destination.audience):
        response = request(https_audience(origin) + "/api/v1/health", response_limit=4096)
        if json.loads(response).get("status") != "ok":
            raise InputError
    if grant.destination.endpoint != grant.destination.audience + "/a2a":
        raise InputError


def expected_error(operation, status):
    try:
        operation()
    except CommonsError as exc:
        return exc.status == status
    return False


def task_missing(operation):
    try:
        operation()
    except CommonsError as exc:
        # The installed SDK maps A2A task-not-found to this fixed message.
        return str(exc) == "Task not found"
    return False


def pair(client, grant, *, message_id=None):
    task = client.send_task(grant, SAMPLE, message_id=message_id or uuid4().hex)
    if (
        task.bytes != len(SAMPLE)
        or task.words != len(SAMPLE.split())
        or client.get_task(grant, task.id) != task
    ):
        raise InputError
    return task


def _client(peer):
    return MeasuredClient(peer[0].origin, api_key=peer[0].api_key)


def smoke(report, peers):
    _, grant = peers[0]
    with _client(peers[0]) as client:
        task = pair(client, grant)
        report.set("send_get", True)
        message = uuid4().hex
        repeated = client.send_task(grant, SAMPLE, message_id=message)
        same = client.send_task(grant, SAMPLE, message_id=message) == repeated
        try:
            client.send_task(grant, "different", message_id=message)
        except CommonsError as exc:
            conflict = str(exc) == "Message ID was already used with different text"
        else:
            conflict = False
        ok = report.set("idempotency", same and conflict)
    if len(peers) != 2:
        raise InputError
    other = peers[1][1]
    if (
        other.destination.endpoint != grant.destination.endpoint
        or other.grant.caller_root == grant.grant.caller_root
    ):
        raise InputError
    with _client(peers[1]) as client:
        return report.set("ownership", task_missing(lambda: client.get_task(other, task.id))) and ok


def _checkpoint(client, grant, *, create):
    ref = client._request("Reference", "GET", f"agents/remote/{grant.destination.reference_id}")
    claim = client._verified_destination(grant.destination)
    if claim.location_sequence != grant.location_sequence:
        raise InputError
    reference = str(grant.destination.reference_id)
    response = client._http.get(RELATIONSHIPS, headers={"Authorization": f"Bearer {client._key}"})
    if response.status_code != 200 or not isinstance(follows := response.json(), list):
        raise InputError
    selected = next((item for item in follows if item["remote_reference_id"] == reference), None)
    if selected is None and create:
        selected = client._request(
            "Follow",
            "POST",
            RELATIONSHIPS,
            body={"remote_reference_id": reference, "kind": "follow"},
        )
    if (
        not selected
        or selected["kind"] != "follow"
        or selected["remote_root_fingerprint"] != claim.root_fingerprint
    ):
        raise InputError
    return Checkpoint(
        grant=grant.grant.grant_id,
        audience=grant.destination.audience,
        card_url=ref["card_url"],
        root=claim.root_fingerprint,
        reference=grant.destination.reference_id,
        follow=UUID(selected["id"]),
        location=claim.location_sequence,
    )


def cutover(report, entry, peer, *, before):
    with _client(peer) as client:
        current = _checkpoint(client, peer[1], create=before)
        with _directory(entry.config) as directory:
            if before:
                pair(client, peer[1])
                _write(
                    directory,
                    "staging-checkpoint.json",
                    current.model_dump_json().encode(),
                    replace=False,
                )
                return report.set("send_get", True)
            old = Checkpoint.model_validate_json(
                _read("staging-checkpoint.json", directory=directory)
            )
        same = old.root == current.root and old.reference == current.reference
        passed = report.set(
            "signed_migration",
            same and old.audience != current.audience and old.location < current.location,
        )
        passed &= report.set("preserved_relationship", same and old.follow == current.follow)
        if not passed:
            return False
        stale = expected_error(lambda: client.resolve_peer(old.card_url), 409)
        report.set("stale_location", stale)
        old_peer = load_peer(Entry(config=entry.config, grant=old.grant))
        denied = expected_error(lambda: client._verified_destination(old_peer[1].destination), None)
        pair(client, peer[1])
        fresh = old.grant != current.grant and denied
        return report.set("fresh_destination", fresh) and stale


def lifecycle(report, peers, *, revoked):
    if len(peers) != 2:
        raise InputError
    old, fresh = (peer[1] for peer in peers)
    field = "revocation" if revoked else "rotation"
    if (
        old.grant.grant_id == fresh.grant.grant_id
        or old.grant.caller_root != fresh.grant.caller_root
        or old.destination != fresh.destination
        or peers[0][0].origin != peers[1][0].origin
    ):
        raise InputError
    with _client(peers[1]) as client:
        task = pair(client, fresh)

        def check_old():
            return client.get_task(old, task.id)

        if revoked:
            return report.set(field, expected_error(check_old, 401))
        return report.set(field, check_old() == task)


def p95(values):
    if not values:
        return None
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 2)


def _load_peers(peers):
    if (
        len(peers) != 5
        or len({peer[1].grant.caller_root for peer in peers}) != 5
        or len({peer[1].grant.grant_id for peer in peers}) != 5
        or len({peer[1].destination.endpoint for peer in peers}) != 1
    ):
        raise InputError


def _offsets(index, functional):
    if functional:
        # Interleave the five callers at six-second intervals.
        return (6 * index, 30 + 6 * index)
    return (second + index / 5 for second in range(DURATION_SECONDS))


def _workload(report, peers, *, functional):
    planned = FUNCTIONAL_PAIR_COUNT if functional else PAIR_COUNT
    prefix = uuid4().hex
    start = monotonic()

    def worker(index, peer):
        completed = []
        errors = {429: 0, 503: 0, "other": 0, "missed": 0}
        with _client(peer) as client:
            for sequence, offset in enumerate(_offsets(index, functional)):
                deadline = start + offset
                sleep(max(0.0, deadline - monotonic()))
                if monotonic() > deadline + 0.8:
                    errors["missed"] += 1
                    continue
                message_id = f"{prefix}-{index}-{sequence}" if functional else uuid4().hex
                try:
                    result = pair(client, peer[1], message_id=message_id)
                    if monotonic() <= start + DURATION_SECONDS:
                        completed.append((result.id, message_id))
                    else:
                        errors["missed"] += 1
                except (CommonsError, InputError) as exc:
                    code = getattr(exc, "status", None)
                    errors[code if code in (429, 503) else "other"] += 1
            return completed, errors, client

    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = [pool.submit(worker, index, peer) for index, peer in enumerate(peers)]
        results = [job.result() for job in jobs]
    if functional:
        sleep(max(0.0, start + DURATION_SECONDS - monotonic()))
    ids = [task for completed, _, _ in results for task, _ in completed]
    samples = [client for _, _, client in results]
    failures = {
        key: sum(errors[key] for _, errors, _ in results) for key in (429, 503, "other", "missed")
    }
    resolution_429 = sum(sample.refresh_status.get(429, 0) for sample in samples)
    receiver_429 = sum(len(sample.quota_reply_ms) for sample in samples)
    total_p95 = p95([ms for client in samples for ms in client.call_ms])
    successes = [ms for client in samples for ms in client.success_ms]
    counts = [len(completed) for completed, _, _ in results]
    report.data["metrics"].update(
        {
            "scheduled_pairs": planned,
            "completed_pairs": len(ids),
            "completed_pairs_per_peer": counts,
            "duration_seconds": round(monotonic() - start, 2),
            "call_p95_ms": total_p95,
            "successful_call_p95_ms": p95(successes),
            "resolution_p95_ms": p95([ms for client in samples for ms in client.refresh_ms]),
            "dispatch_p95_ms": p95([ms for client in samples for ms in client.rpc_ms]),
            "quota_response_p95_ms": p95(
                [ms for client in samples for ms in client.quota_reply_ms]
            ),
            "failed_pairs": failures,
            "missed_schedules": failures["missed"],
            "unique_completed_task_ids": len(set(ids)),
            "resolution_429_responses": resolution_429,
            "receiver_429_responses": receiver_429,
        }
    )
    if not functional:
        passed = (
            len(ids) >= math.ceil(0.99 * PAIR_COUNT)
            and total_p95 is not None
            and total_p95 <= DEADLINE_MS
            and len(set(ids)) == len(ids)
        )
        return report.set("load", passed)

    complete = report.set("send_get", counts == [2] * 5)
    quotas = report.set("functional_quotas", resolution_429 == receiver_429 == 0)
    deadlines = report.set(
        "functional_deadlines",
        complete
        and len(successes) == 20
        and max(successes) <= DEADLINE_MS
        and not failures["missed"],
    )
    proofs = report.set(
        "signed_dispatch", complete and sum(len(client.rpc_ms) for client in samples) == 20
    )
    if not (complete and quotas and deadlines and proofs):
        return report.set("functional", False)
    return report.set("functional", _functional_ownership(report, peers, results, ids))


def load(report, peers):
    _load_peers(peers)
    return _workload(report, peers, functional=False)


def _functional_setup(report, peers):
    # A signed preflight is outside the measured workload. The cooldown clears
    # the 60-second home buckets and the receiver's shorter quota windows.
    setup = report.data["metrics"]
    setup["setup_card_refreshes"] = 0
    setup["setup_resolution_429_responses"] = 0
    for peer in peers:
        grant = peer[1]
        with _client(peer) as client:
            try:
                claim = client._verified_destination(grant.destination)
            finally:
                setup["setup_card_refreshes"] += len(client.refresh_ms)
                setup["setup_resolution_429_responses"] += client.refresh_status.get(429, 0)
            if (
                claim.identity_sequence != grant.identity_sequence
                or claim.location_sequence != grant.location_sequence
                or (min(claim.expiration(), grant.expires_at) - datetime.now(UTC)).total_seconds()
                <= QUIET_SECONDS + DURATION_SECONDS + 30
            ):
                raise InputError
    sleep(QUIET_SECONDS)
    setup["setup_quiet_seconds"] = QUIET_SECONDS


def _functional_ownership(report, peers, results, ids):
    same = len(set(ids)) == len(ids)
    ownership = True
    checks = report.data["metrics"]
    checks["post_run_card_refreshes"] = 0
    for index, peer in enumerate(peers):
        own_id, own_message = results[index][0][0]
        other_id, _ = results[(index + 1) % len(peers)][0][0]
        with _client(peer) as client:
            try:
                same &= client.send_task(peer[1], SAMPLE, message_id=own_message).id == own_id
                ownership &= task_missing(
                    lambda client=client, grant=peer[1], task_id=other_id: client.get_task(
                        grant, task_id
                    )
                )
            except CommonsError:
                same = ownership = False
            finally:
                checks["post_run_card_refreshes"] += len(client.refresh_ms)
    report.set("no_duplicate_tasks", same)
    report.set("ownership", ownership)
    return same and ownership


def functional(report, peers):
    try:
        _load_peers(peers)
        _functional_setup(report, peers)
    except (InputError, CommonsError):
        report.set("functional_setup", False)
        return report.set("functional", False)
    report.set("functional_setup", True)
    return _workload(report, peers, functional=True)


def drills(report, peers, *, outage):
    if outage:
        with _client(peers[0]) as client:
            return report.set(
                "database_failure",
                expected_error(
                    lambda: client.send_task(peers[0][1], SAMPLE, message_id=uuid4().hex), 503
                ),
            )
    if (
        len(peers) != 2
        or peers[0][1].grant.caller_root == peers[1][1].grant.caller_root
        or peers[0][1].destination.endpoint != peers[1][1].destination.endpoint
    ):
        raise InputError
    noisy, control = peers

    def burst():
        with _client(noisy) as client:
            return sum(
                expected_error(
                    lambda: client.send_task(noisy[1], SAMPLE, message_id=uuid4().hex), 429
                )
                for _ in range(8)
            )

    with ThreadPoolExecutor(max_workers=2) as pool, _client(control) as client:
        future = pool.submit(burst)
        pair(client, control[1])
        return report.set("noisy_peer", future.result() > 0)


def operator_report(report, path):
    document = json.loads(_read(Path(path)))
    if not isinstance(document, dict) or set(document) != set(OPERATOR_CHECKS):
        raise InputError
    if any(value not in ("pass", "fail", "not_executed") for value in document.values()):
        raise InputError
    report.data["checks"].update(document)
    return all(value == "pass" for value in document.values())


def quota_report(report, path):
    document = json.loads(_read(Path(path)))
    if not isinstance(document, dict) or set(document) != {"bucket", "grant"}:
        raise InputError
    stats = {key: QuotaTiming.model_validate(value).model_dump() for key, value in document.items()}
    report.data["metrics"]["quota_db_timing"] = stats
    return report.set("quota_db_timing", True)


Mode = Literal[
    "smoke",
    "rotate",
    "revoke",
    "capture",
    "cutover",
    "load",
    "functional",
    "noise",
    "db-failure",
    "audit",
    "operator",
    "quota-stats",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=Mode.__args__)
    parser.add_argument("--manifest", help="Private JSON file listing local grant files")
    parser.add_argument("--audit-file", help="Private SQL summary or operator attestation JSON")
    parser.add_argument(
        "--confirm-quiet",
        action="store_true",
        help="Confirm unused staging quota windows and exclusive workload for functional mode",
    )
    args = parser.parse_args()
    report = Report(args.mode)
    if args.mode in {"audit", "operator", "quota-stats"}:
        try:
            if args.mode == "operator":
                success = operator_report(report, args.audit_file)
            elif args.mode == "quota-stats":
                success = quota_report(report, args.audit_file)
            else:
                audit = Audit.model_validate_json(_read(Path(args.audit_file)))
                clean = report.set("no_duplicate_tasks", audit.duplicate_groups == 0)
                retained = report.set("retention", audit.retained <= 1024)
                report.data["metrics"] = {"retained_tasks": audit.retained}
                success = clean and retained
            report.set("preflight", True)
        except (OSError, ValueError, TypeError):
            report.set("preflight", False)
            print("Check the protected SQL summary or operator evidence file.", file=sys.stderr)
            success = False
        print(json.dumps(report.data, sort_keys=True))
        return 0 if success else 1
    field = {
        "smoke": "send_get",
        "rotate": "rotation",
        "revoke": "revocation",
        "capture": "send_get",
        "cutover": "signed_migration",
        "load": "load",
        "functional": "functional",
        "noise": "noisy_peer",
        "db-failure": "database_failure",
    }[args.mode]
    try:
        if args.mode == "functional" and not args.confirm_quiet:
            print(
                "Functional mode requires --confirm-quiet after quota isolation.", file=sys.stderr
            )
            raise InputError
        manifest = Manifest.model_validate_json(_read(Path(args.manifest)))
        peers = [load_peer(peer) for peer in manifest.peers]
        for account, grant in peers:
            validate_pair(account, grant)
        report.set("preflight", True)
        report.set("public_origins", True)
        match args.mode:
            case "smoke":
                success = smoke(report, peers)
            case "rotate" | "revoke":
                success = lifecycle(report, peers, revoked=args.mode == "revoke")
            case "capture" | "cutover":
                success = cutover(
                    report, manifest.peers[0], peers[0], before=args.mode == "capture"
                )
            case "load":
                success = load(report, peers)
            case "functional":
                success = functional(report, peers)
            case "noise" | "db-failure":
                success = drills(report, peers, outage=args.mode == "db-failure")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, httpx.HTTPError):
        if report.data["checks"]["public_origins"] == "not_executed":
            report.set("preflight", False)
            print(
                "Staging preflight failed. Check protected configuration, grants, DNS and HTTPS.",
                file=sys.stderr,
            )
        else:
            if report.data["checks"][field] == "not_executed":
                report.set(field, False)
            print(
                "Staging check failed. Verify the selected workflow and operator prerequisites.",
                file=sys.stderr,
            )
        success = False
    print(json.dumps(report.data, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
