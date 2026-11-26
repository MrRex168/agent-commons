"""Disposable PostgreSQL and loopback HTTPS for the integration demo."""

import ipaddress
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from scripts.cross_instance_ci import ROOT

LOOPBACK = "127.0.0.1"


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def make_tls(directory: Path) -> tuple[Path, Path, ssl.SSLContext]:
    # A short-lived certificate trusted only by the demo clients.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Agent Internet Live fixture")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(LOOPBACK))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "localhost.pem"
    key_path = directory / "localhost-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path, ssl.create_default_context(cafile=str(cert_path))


def _driver_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def isolated_databases(admin_dsn: str, labels: tuple[str, ...]):
    admin = make_url(admin_dsn)
    if (
        admin.drivername not in {"postgresql", "postgresql+psycopg"}
        or admin.host not in {LOOPBACK, "localhost", "::1"}
        or admin.database != "postgres"
        or admin.query
        or os.environ.get("PGHOSTADDR")
        or os.environ.get("PGSERVICE")
    ):
        raise ValueError("Demo PostgreSQL admin URL must point to a local disposable postgres DB")
    names = {label: f"ac_live_{secrets.token_hex(8)}_{label}" for label in labels}
    with psycopg.connect(_driver_url(admin), autocommit=True, connect_timeout=3) as db:
        with ExitStack() as cleanup:
            nodes = {}
            for label, name in names.items():
                password = secrets.token_hex(24)
                ident = sql.Identifier(name)
                db.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(ident, sql.Literal(password))
                )
                cleanup.callback(db.execute, sql.SQL("DROP ROLE {}").format(ident))
                db.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(ident, ident))
                cleanup.callback(db.execute, sql.SQL("DROP DATABASE {}").format(ident))
                db.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(ident))
                nodes[label] = admin.set(
                    drivername="postgresql+psycopg", database=name, username=name, password=password
                )

            for label, node in nodes.items():
                with psycopg.connect(_driver_url(node), connect_timeout=3) as own:
                    current = own.execute("SELECT current_database(), current_user").fetchone()
                    if current != (names[label], names[label]):
                        raise RuntimeError("Demo database or role mismatch")
                for other in labels:
                    if other == label:
                        continue
                    try:
                        with psycopg.connect(
                            _driver_url(node.set(database=names[other])), connect_timeout=3
                        ):
                            raise RuntimeError("Demo database accepted a sibling's credentials")
                    except psycopg.OperationalError as exc:
                        if "permission denied for database" not in str(exc):
                            raise RuntimeError("Unexpected database isolation failure") from None
            yield nodes
        if db.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = ANY(%s)) OR "
            "EXISTS(SELECT 1 FROM pg_roles WHERE rolname = ANY(%s))",
            (list(names.values()), list(names.values())),
        ).fetchone()[0]:
            raise RuntimeError("Demo database or role cleanup failed")


class Server:
    def __init__(self, listener: socket.socket, database: URL):
        self.listener = listener
        self.url = f"https://{LOOPBACK}:{listener.getsockname()[1]}"
        self.database = database
        self.process: subprocess.Popen | None = None

    @classmethod
    def provision(cls, resources: ExitStack, database: URL):
        listener = resources.enter_context(socket.socket())
        listener.bind((LOOPBACK, 0))
        listener.listen()
        server = cls(listener, database)
        resources.callback(server.stop)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={
                "PATH": os.environ["PATH"],
                "AGENT_COMMONS_DATABASE_URL": database.render_as_string(hide_password=False),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
        return server

    def start(
        self,
        client: httpx.Client,
        cert: Path,
        key: Path,
        cards: list[str],
        *,
        peers: list | None = None,
        links: list | None = None,
        database_grants: bool = False,
        app_entrypoint: str = "scripts.agent_internet_live_app:app",
    ) -> None:
        self.stop()
        fd = self.listener.fileno()
        # Do not inherit proxy settings, application credentials, or remote database URLs.
        env = {
            "PATH": os.environ["PATH"],
            "AGENT_COMMONS_DATABASE_URL": self.database.render_as_string(hide_password=False),
            "AGENT_COMMONS_API_URL": self.url,
            "AGENT_COMMONS_A2A_TASK_PEERS": json.dumps(peers or []),
            "AGENT_INTERNET_LIVE_CARDS": json.dumps(cards),
            "AGENT_INTERNET_LIVE_LINKS": json.dumps(links or []),
            "SSL_CERT_FILE": str(cert),
        }
        if database_grants:
            env["AGENT_COMMONS_REGISTRATION_ADMISSION"] = "invite"
            env["AGENT_COMMONS_A2A_DATABASE_GRANTS"] = "true"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                app_entrypoint,
                "--fd",
                str(fd),
                "--ssl-certfile",
                str(cert),
                "--ssl-keyfile",
                str(key),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=env,
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.process = process
        for _ in range(30):
            if process.poll() is not None:
                break
            try:
                if client.get(self.url + "/api/v1/health", timeout=0.25).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("A demo HTTPS server failed to start")

    def stop(self) -> None:
        if self.process is not None:
            _stop(self.process)
            self.process = None
