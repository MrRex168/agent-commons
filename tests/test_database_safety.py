import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
DEVELOPMENT_URL = "postgresql+psycopg://agent_commons:agent_commons@localhost/agent_commons"
TEST_URL = DEVELOPMENT_URL + "_test"


@pytest.mark.parametrize(
    ("test_url", "app_url", "extra_env", "message"),
    [
        (None, DEVELOPMENT_URL, {}, "Set AGENT_COMMONS_TEST_DATABASE_URL"),
        (None, TEST_URL, {}, "Set AGENT_COMMONS_TEST_DATABASE_URL"),
        ("", DEVELOPMENT_URL, {}, "Set AGENT_COMMONS_TEST_DATABASE_URL"),
        ("invalid", DEVELOPMENT_URL, {}, "is not a valid database URL"),
        (DEVELOPMENT_URL, DEVELOPMENT_URL, {}, "a database name ending in _test"),
        (TEST_URL, TEST_URL, {}, "must differ from the application database name"),
        (
            TEST_URL,
            DEVELOPMENT_URL.removesuffix("/agent_commons"),
            {},
            "application database URL must specify a database name",
        ),
        (
            TEST_URL,
            DEVELOPMENT_URL + "?dbname=agent_commons_test",
            {},
            "must differ from the application database name",
        ),
        (TEST_URL + "?dbname=agent_commons", DEVELOPMENT_URL, {}, "without URL query parameters"),
        (TEST_URL.replace("@localhost", "@"), DEVELOPMENT_URL, {}, "an explicit host"),
        (TEST_URL.replace("postgresql+psycopg", "sqlite"), DEVELOPMENT_URL, {}, "use postgresql"),
        (TEST_URL, DEVELOPMENT_URL, {"PGHOSTADDR": "127.0.0.1"}, "Unset PGHOSTADDR and PGSERVICE"),
        (TEST_URL, DEVELOPMENT_URL, {"PGSERVICE": "development"}, "Unset PGHOSTADDR and PGSERVICE"),
    ],
)
def test_unsafe_database_exits_before_collection(
    tmp_path: Path, test_url: str | None, app_url: str, extra_env: dict[str, str], message: str
) -> None:
    (tmp_path / ".env").write_text(f"AGENT_COMMONS_DATABASE_URL={app_url}\n")
    env = os.environ.copy()
    env.pop("AGENT_COMMONS_DATABASE_URL", None)
    env.pop("AGENT_COMMONS_TEST_DATABASE_URL", None)
    env.pop("PGHOSTADDR", None)
    env.pop("PGSERVICE", None)
    env.update(extra_env)
    if test_url is not None:
        env["AGENT_COMMONS_TEST_DATABASE_URL"] = test_url

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(TESTS / "test_privacy.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert message in result.stderr
    assert "test_privacy.py::" not in result.stdout
