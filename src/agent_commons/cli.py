"""Operator and developer commands for Agent Commons onboarding."""

import argparse
import fcntl
import getpass
import json
import os
import re
import secrets
import stat
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_commons.client import CommonsClient, CommonsError, public_multikey, server_origin
from agent_commons.identity_crypto import identity_fingerprint
from agent_commons.outbound_a2a import BoundGrant, DestinationApproval

FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
NAME_PATTERN = r"^[a-zA-Z0-9_-]{3,80}$"
MAX_CONFIG_BYTES = 4096


class LocalAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    origin: str
    name: str = Field(pattern=NAME_PATTERN)
    state: Literal["initialized", "registering", "registered"] = "initialized"
    api_key: Annotated[str, Field(pattern=r"^ac_[A-Za-z0-9_-]{43}$")] | None = Field(
        default=None, repr=False
    )

    @model_validator(mode="after")
    def validate_account(self):
        if (self.state == "registered") != bool(self.api_key):
            raise ValueError("Inconsistent registration state")
        return self


@contextmanager
def _directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CommonsError("The configuration directory must be owned by you with mode 0700")
        # Serialize CLI operations across processes, including registration and publication.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


def _read(name: str | Path, *, directory: int | None = None) -> bytes:
    fd = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise CommonsError("Credential files must be private regular files with mode 0600")
        data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise CommonsError("The credential file is too large")
        return data


def _write(directory: int, name: str, data: bytes, *, replace: bool = True) -> None:
    temp = ".tmp-" + secrets.token_hex(12)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            os.link(temp, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temp, dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass


def _save(directory: int, account: LocalAccount) -> None:
    _write(directory, "account.json", account.model_dump_json().encode("utf-8"))


def _initialize(args) -> None:
    account = LocalAccount(origin=server_origin(args.url), name=args.name)
    key = Ed25519PrivateKey.generate()
    try:
        args.config.mkdir(mode=0o700)
    except FileExistsError:
        raise CommonsError(
            "The configuration path already exists. Choose a new directory"
        ) from None
    with _directory(args.config) as directory:
        _write(directory, "controller.key", key.private_bytes_raw())
        _save(directory, account)
    print(f"Local sovereign fingerprint: {identity_fingerprint(public_multikey(key))}")


def _register(args, directory: int, account: LocalAccount, client: CommonsClient) -> None:
    if account.state == "registered":
        raise CommonsError("This configuration is already registered")
    if account.state == "registering":
        raise CommonsError("Registration outcome is unknown. Ask the operator before retrying")
    invitation = None
    if args.invitation_file:
        invitation = _read(args.invitation_file).decode("ascii").strip()
    elif args.prompt_invitation:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                invitation = getpass.getpass("Registration invitation: ").strip()
            except (getpass.GetPassWarning, EOFError):
                raise CommonsError("Use a secure terminal or a private invitation file") from None
    if invitation is not None and not re.fullmatch(r"aci_[A-Za-z0-9_-]{43}", invitation):
        raise CommonsError("Invalid registration invitation format")
    # Persist intent before the request. A lost one-time key cannot be recovered by replay.
    _save(directory, account.model_copy(update={"state": "registering"}))
    try:
        api_key = client.register(account.name, invitation=invitation)
    except CommonsError as exc:
        if exc.status in (403, 409, 422):
            _save(directory, account)
        if exc.status == 403:
            raise CommonsError(
                "Registration denied. Ask the operator for a valid invitation"
            ) from None
        if exc.status not in (409, 422):
            raise CommonsError(
                "Registration outcome is unknown. Ask the operator before retrying"
            ) from None
        raise
    _save(directory, account.model_copy(update={"state": "registered", "api_key": api_key}))
    print("Agent registered. Local API key saved privately.")


def _operator_invite(path: Path) -> None:
    # The developer commands never import settings or open a database connection.
    from agent_commons.config import settings
    from agent_commons.db import SessionLocal
    from agent_commons.registration import issue_invitation

    if settings.registration_admission != "invite":
        raise CommonsError("Invite mode must be enabled before issuing an invitation")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream, SessionLocal.begin() as db:
            stream.write(issue_invitation(db) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print("Single-use invitation saved to a private file. It expires in 15 minutes.")


def _operator_peer(args) -> None:
    from agent_commons.db import SessionLocal
    from agent_commons.peer_enrollment import approve_peer, change_grant

    with SessionLocal.begin() as db:
        if args.command == "peer-approve":
            approval_id = approve_peer(
                db,
                reference_id=args.reference,
                expected_root=args.caller_root,
                recipient=args.recipient,
                scopes=args.scope,
                lifetime_hours=args.hours,
                skills=args.skill,
            )
        else:
            approval_id = change_grant(db, args.grant, rotate=args.command == "peer-rotate")
    if args.command == "peer-revoke":
        print("Peer grant revoked.")
    else:
        print(f"Enrollment approval: {approval_id}")


def _enroll_peer(args, directory: int, key: Ed25519PrivateKey, account: LocalAccount) -> None:
    bound_options = (args.reference, args.peer_root, args.peer_controller, args.endpoint)
    options = {"skills": args.skill} if args.skill is not None else {}
    if any(bound_options):
        if not all(bound_options):
            raise CommonsError("Provide the reference, peer root, controller and endpoint together")
        destination = DestinationApproval(
            reference_id=args.reference,
            peer_root=args.peer_root,
            controller=args.peer_controller,
            recipient_agent_id=args.recipient,
            endpoint=args.endpoint,
        )
        if args.url != destination.audience:
            raise CommonsError("Enrollment URL must match the approved task origin")
        with CommonsClient(account.origin, api_key=account.api_key) as client:
            bound = client.enroll_dispatch_peer(
                key,
                destination=destination,
                approval_id=args.approval,
                caller_root=args.caller_root,
                scopes=args.scope,
                **options,
            )
        grant = bound.grant
        raw = bound.model_dump(mode="json")
        raw["grant"]["token"] = grant.token.get_secret_value()
    else:
        if options:
            raise CommonsError("Typed enrollment requires an approved destination binding")
        with CommonsClient(args.url) as client:
            grant = client.enroll_peer(
                key,
                approval_id=args.approval,
                caller_root=args.caller_root,
                recipient_agent_id=args.recipient,
                scopes=args.scope,
                **options,
            )
        raw = grant.model_dump(mode="json")
        raw["token"] = grant.token.get_secret_value()
    name = f"peer-{grant.grant_id}.json"
    try:
        _write(directory, name, json.dumps(raw).encode("utf-8"), replace=False)
    except OSError:
        raise CommonsError(
            f"Could not save issued grant {grant.grant_id}. Ask the operator to revoke it"
        ) from None
    print(f"Peer grant saved privately: {grant.grant_id}")


def _task_command(args, directory: int, client: CommonsClient) -> None:
    raw = _read(f"peer-{args.grant}.json", directory=directory)
    try:
        grant = BoundGrant.model_validate_json(raw)
        if grant.grant.grant_id != args.grant:
            raise ValueError
    except ValueError:
        raise CommonsError(
            "The credential file has no matching verified destination binding"
        ) from None
    if args.command == "task-send":
        try:
            text = sys.stdin.buffer.read(4097).decode("utf-8")
        except UnicodeDecodeError:
            raise CommonsError("Task input must be UTF-8 text") from None
        result = client.send_task(grant, text, message_id=args.message_id or uuid4().hex)
    else:
        result = client.get_task(grant, args.task_id)
    print(result.model_dump_json())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-commons", description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".agent-commons")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a local controller key and configuration")
    init.add_argument("--url", required=True)
    init.add_argument("--name", required=True)
    register = commands.add_parser("register", help="Register with the configured server")
    invitation = register.add_mutually_exclusive_group()
    invitation.add_argument("--invitation-file", type=Path)
    invitation.add_argument("--prompt-invitation", action="store_true")
    commands.add_parser("identity", help="Bind the local controller key to the account")
    profile = commands.add_parser("profile", help="Update the account profile")
    profile.add_argument("--description")
    profile.add_argument("--capability", action="append")
    card = commands.add_parser("publish-card", help="Publish a controller-authorized Agent Card")
    card.add_argument("--card-url", required=True)
    card.add_argument("--endpoint", required=True)
    card.add_argument("--location-sequence", type=int, required=True)
    card.add_argument("--transport", choices=["JSONRPC", "GRPC", "HTTP+JSON"], default="JSONRPC")
    resolve = commands.add_parser("resolve", help="Resolve a known peer Card through the server")
    resolve.add_argument("card_url")
    inspect = commands.add_parser("inspect", help="Inspect a cached peer's verification status")
    inspect.add_argument("reference_id", type=UUID)
    issue = commands.add_parser("issue-invite", help="Issue an invitation as a database operator")
    issue.add_argument("--output", type=Path, required=True)
    approve = commands.add_parser("peer-approve", help="Approve enrollment as database operator")
    approve.add_argument("--reference", required=True, type=UUID)
    approve.add_argument("--hours", type=int, default=1)
    enroll = commands.add_parser("peer-enroll", help="Redeem a peer approval with a local key")
    enroll.add_argument("--url", required=True)
    enroll.add_argument("--approval", required=True, type=UUID)
    enroll.add_argument("--reference", type=UUID)
    enroll.add_argument("--peer-root")
    enroll.add_argument("--peer-controller")
    enroll.add_argument("--endpoint")
    for command in (approve, enroll):
        command.add_argument(
            "--skill", action="append", help="Exact typed skill such as analysis.summary@1"
        )
        command.add_argument("--caller-root", required=True)
        command.add_argument("--recipient", required=True, type=UUID)
        command.add_argument(
            "--scope", required=True, action="append", choices=["message/send", "tasks/get"]
        )
    for name in ("peer-rotate", "peer-revoke"):
        command = commands.add_parser(name)
        command.add_argument("--grant", required=True, type=UUID)
    send = commands.add_parser("task-send", help="Send a Text Statistics task from stdin")
    send.add_argument("--grant", required=True, type=UUID)
    send.add_argument("--message-id")
    get = commands.add_parser("task-get", help="Retrieve a task by ID")
    get.add_argument("--grant", required=True, type=UUID)
    get.add_argument("task_id", type=UUID)
    return parser


def run(args) -> None:
    if args.command == "init":
        _initialize(args)
        return
    if args.command == "issue-invite":
        _operator_invite(args.output)
        return

    if args.command in ("peer-approve", "peer-rotate", "peer-revoke"):
        _operator_peer(args)
        return

    with _directory(args.config) as directory:
        account = LocalAccount.model_validate_json(_read("account.json", directory=directory))
        if args.command in ("task-send", "task-get"):
            with CommonsClient(account.origin, api_key=account.api_key) as client:
                _task_command(args, directory, client)
            return
        key = Ed25519PrivateKey.from_private_bytes(_read("controller.key", directory=directory))
        if args.command == "peer-enroll":
            _enroll_peer(args, directory, key, account)
            return
        with CommonsClient(account.origin, api_key=account.api_key) as client:
            match args.command:
                case "register":
                    _register(args, directory, account, client)
                case "identity":
                    print(f"Verified sovereign fingerprint: {client.bind_identity(key)}")
                case "profile":
                    client.update_profile(
                        description=args.description, capabilities=args.capability
                    )
                    print("Agent profile updated.")
                case "publish-card":
                    root = client.publish_card(
                        key,
                        card_url=args.card_url,
                        endpoint=args.endpoint,
                        location_sequence=args.location_sequence,
                        transport=args.transport,
                    )
                    print(f"Agent Card authorization published for {root}")
                case "resolve":
                    print(client.resolve_peer(args.card_url).model_dump_json(indent=2))
                case "inspect":
                    print(client.inspect_peer(args.reference_id).model_dump_json(indent=2))


def main() -> None:
    args = _parser().parse_args()
    try:
        run(args)
    except CommonsError as exc:
        print(f"agent-commons: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("agent-commons: operation interrupted", file=sys.stderr)
        raise SystemExit(130) from None
    except BlockingIOError:
        print("agent-commons: another process is using this configuration", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Never emit API bodies, connection details, configuration contents, or tracebacks.
        print(
            "agent-commons: operation failed. Check configuration, file permissions, "
            "and server connectivity",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
