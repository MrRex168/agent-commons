import json
import os
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from agent_commons import cli, outbound_https
from agent_commons.client import CommonsClient, CommonsError, public_multikey
from agent_commons.outbound_a2a import DestinationApproval
from scripts import agent_internet_live as demo
from scripts.agent_internet_live_support import Server, isolated_databases, make_tls

SCOPES = ["message/send", "tasks/get"]


def approve(server, reference, caller, recipient):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_commons.cli",
            "peer-approve",
            "--reference",
            reference["id"],
            "--caller-root",
            caller.root,
            "--recipient",
            recipient.agent_id,
            "--scope",
            "message/send",
            "--scope",
            "tasks/get",
        ],
        env={
            "PATH": os.environ["PATH"],
            "AGENT_COMMONS_DATABASE_URL": server.database.render_as_string(hide_password=False),
            "AGENT_COMMONS_API_URL": server.url,
            "AGENT_COMMONS_A2A_DATABASE_GRANTS": "true",
            "AGENT_COMMONS_REGISTRATION_ADMISSION": "invite",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return UUID(proc.stdout.strip().removeprefix("Enrollment approval: "))


def test_direct_sdk_cutover_across_isolated_databases(tmp_path, monkeypatch, capsys):
    dsn = os.environ.get("AGENT_COMMONS_DEMO_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires a disposable local PostgreSQL admin connection")
    cert, key, tls = make_tls(tmp_path)
    with isolated_databases(dsn, ("atlas", "source", "target")) as databases, ExitStack() as stack:
        http = stack.enter_context(httpx.Client(verify=tls, trust_env=False, timeout=5))
        atlas_node, source, target = [
            Server.provision(stack, databases[name]) for name in ("atlas", "source", "target")
        ]
        cards = [atlas_node.url + demo.card_path("atlas")] + [
            server.url + demo.card_path("nova") for server in (source, target)
        ]
        for server in (atlas_node, source, target):
            server.start(http, cert, key, cards)
        atlas = demo.register(http, atlas_node, "atlas")
        nova = demo.register(http, source, "nova")
        demo.publish(http, atlas_node, atlas, 1)
        demo.publish(http, source, nova, 1)
        reference = demo.resolve(http, atlas_node, atlas, source, "nova")
        atlas_reference = demo.resolve(http, source, nova, atlas_node, "atlas")
        follow = demo.follow(http, atlas_node, atlas, reference)
        source.start(http, cert, key, cards, database_grants=True)
        source_approval = approve(source, atlas_reference, atlas, nova)

        # The test override is scoped to the two provisioned receiver origins. It
        # changes only address admission and the trust root. Actual libcurl uses TLS.
        calls = []
        real_transfer = outbound_https._transfer
        permitted = {str(httpx.URL(node.url).netloc, "ascii") for node in (source, target)}

        def fixture_resolve(url, timeout):
            assert url.netloc.decode("ascii") in permitted
            return "127.0.0.1"

        def record_transfer(url, address, **kwargs):
            calls.append((str(url), kwargs["bearer"] is not None))
            return real_transfer(url, address, **kwargs)

        monkeypatch.setattr(outbound_https, "_resolve", fixture_resolve)
        monkeypatch.setattr(outbound_https, "_transfer", record_transfer)
        monkeypatch.setattr(outbound_https.certifi, "where", lambda: str(cert))
        sdk = stack.enter_context(
            CommonsClient(
                atlas_node.url, api_key=atlas.api_key, transport=httpx.HTTPTransport(verify=tls)
            )
        )

        def enroll(node, identity, approval, **changes):
            destination = DestinationApproval(
                reference_id=UUID(reference["id"]),
                peer_root=identity.root,
                controller=public_multikey(identity.key),
                recipient_agent_id=UUID(identity.agent_id),
                endpoint=node.url + "/a2a",
            )
            return sdk.enroll_dispatch_peer(
                atlas.key,
                destination=destination.model_copy(update=changes),
                approval_id=approval,
                caller_root=atlas.root,
                scopes=SCOPES,
            )

        for changes in (
            {"peer_root": atlas.root},
            {"controller": public_multikey(atlas.key)},
            {"endpoint": source.url + "/other"},
        ):
            with pytest.raises(CommonsError, match="destination could not be verified"):
                enroll(source, nova, source_approval, **changes)
        assert not calls
        with pytest.raises(CommonsError, match="challenge did not match"):
            enroll(source, nova, source_approval, recipient_agent_id=uuid4())
        assert all(not credential for _, credential in calls)
        old = enroll(source, nova, source_approval)
        assert all(not credential for _, credential in calls)

        first = sdk.send_task(old, "hello world", message_id="before")
        assert (first.state, first.bytes, first.words) == ("completed", 11, 2)
        assert sdk.get_task(old, first.id) == first
        assert sdk.send_task(old, "hello world", message_id="before") == first
        with pytest.raises(CommonsError, match="already used"):
            sdk.send_task(old, "different", message_id="before")
        with pytest.raises(CommonsError, match="Task not found"):
            sdk.get_task(old, uuid4())

        config = tmp_path / "client"
        config.mkdir(mode=0o700)
        raw = old.model_dump(mode="json")
        raw["grant"]["token"] = old.grant.token.get_secret_value()
        with cli._directory(config) as directory:
            cli._write(directory, f"peer-{old.grant.grant_id}.json", json.dumps(raw).encode())
            args = cli._parser().parse_args(
                ["task-get", "--grant", str(old.grant.grant_id), str(first.id)]
            )
            cli._task_command(args, directory, sdk)
        assert json.loads(capsys.readouterr().out) == first.model_dump(mode="json")

        refresh = sdk._request
        for invalid in ("expired", "altered"):

            def bad_proof(*args, invalid=invalid, **kwargs):
                result = refresh(*args, **kwargs)
                (ext,) = result["card_snapshot"]["capabilities"]["extensions"]
                proof = ext["params"]["endpointAuthorization"]
                if invalid == "expired":
                    proof["authorization"]["expires_at"] = int(datetime.now(UTC).timestamp()) - 1
                else:
                    proof["signature_multibase"] = "zinvalid"
                return result

            with monkeypatch.context() as patch:
                patch.setattr(sdk, "_request", bad_proof)
                before = list(calls)
                with pytest.raises(CommonsError, match="destination could not be verified"):
                    sdk.get_task(old, first.id)
                assert calls == before

        restricted = old.grant.model_copy(update={"scopes": ["tasks/get"]})
        expired = old.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        before = list(calls)
        with pytest.raises(CommonsError, match="does not permit"):
            sdk.send_task(old.model_copy(update={"grant": restricted}), "hello", message_id="deny")
        with pytest.raises(CommonsError, match="has expired"):
            sdk.get_task(expired, first.id)
        assert calls == before

        relocated = demo.migrate(http, source, target, nova)
        demo.publish(http, target, relocated, 2)
        updated = demo.resolve(http, atlas_node, atlas, target, "nova")
        assert (updated["root_fingerprint"], updated["id"]) == (nova.root, reference["id"])
        demo.resolve(http, atlas_node, atlas, source, "nova", status=409)
        retained = demo.request(http, "GET", atlas_node, demo.FOLLOWS, token=atlas.api_key)
        assert (retained[0]["id"], retained[0]["remote_reference_id"]) == (
            follow["id"],
            reference["id"],
        )
        before = list(calls)
        with pytest.raises(CommonsError, match="destination could not be verified"):
            sdk.send_task(old, "test", message_id="moved")
        assert calls == before

        remote = demo.resolve(http, target, relocated, atlas_node, "atlas")
        target.start(http, cert, key, cards, database_grants=True)
        fresh = enroll(target, relocated, approve(target, remote, atlas, relocated))
        assert fresh.location_sequence == 2 and old.location_sequence == 1
        assert fresh.grant.grant_id != old.grant.grant_id
        after = sdk.send_task(fresh, "moved", message_id="after")
        assert sdk.get_task(fresh, after.id) == after
        with pytest.raises(CommonsError, match="Task not found"):
            sdk.get_task(fresh, first.id)
        demo.request(
            http,
            "POST",
            target,
            "/a2a",
            token=old.grant.token.get_secret_value(),
            body={},
            status=401,
        )
        authenticated_urls = {url for url, with_bearer in calls if with_bearer}
        assert authenticated_urls == {source.url + "/a2a", target.url + "/a2a"}
