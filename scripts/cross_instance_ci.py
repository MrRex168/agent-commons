import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = (
    "postgresql+psycopg://agent_commons:agent_commons@localhost:5432/agent_commons_source"
)
DESTINATION_DB = (
    "postgresql+psycopg://agent_commons:agent_commons@localhost:5432/agent_commons_destination"
)


def _create_databases() -> None:
    connection = psycopg.connect(
        "postgresql://agent_commons:agent_commons@localhost:5432/postgres",
        autocommit=True,
    )
    with connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE DATABASE agent_commons_source")
            cursor.execute("CREATE DATABASE agent_commons_destination")


def _migrate(database_url: str) -> None:
    env = os.environ.copy()
    env["AGENT_COMMONS_DATABASE_URL"] = database_url
    subprocess.run(["alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True)


def _start_server(database_url: str, api_url: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["AGENT_COMMONS_DATABASE_URL"] = database_url
    env["AGENT_COMMONS_API_URL"] = api_url
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agent_commons.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
    )


def _wait_for_health(url: str) -> None:
    for _ in range(20):
        try:
            response = httpx.get(f"{url}/health", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Agent Commons instance did not become healthy: {url}")


def main() -> None:
    _create_databases()
    _migrate(SOURCE_DB)
    _migrate(DESTINATION_DB)

    source_url = "http://127.0.0.1:8010"
    destination_url = "http://127.0.0.1:8020"
    source = _start_server(SOURCE_DB, source_url, 8010)
    destination = _start_server(DESTINATION_DB, destination_url, 8020)

    try:
        _wait_for_health(source_url)
        _wait_for_health(destination_url)
        subprocess.run(
            [
                sys.executable,
                "scripts/cross_instance_migration_demo.py",
                "--source-url",
                source_url,
                "--destination-url",
                destination_url,
            ],
            cwd=ROOT,
            check=True,
        )
    finally:
        source.terminate()
        destination.terminate()
        source.wait(timeout=10)
        destination.wait(timeout=10)


if __name__ == "__main__":
    main()
