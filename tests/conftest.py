import os
import re

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

TEST_DATABASE_ENV = "AGENT_COMMONS_TEST_DATABASE_URL"


def pytest_sessionstart() -> None:
    """Select and check the test engine before importing test modules."""
    raw_url = os.environ.get(TEST_DATABASE_ENV)
    if not raw_url:
        raise pytest.UsageError(
            f"Set {TEST_DATABASE_ENV} to a disposable PostgreSQL database. "
            "See the README quality checks."
        )

    try:
        test_url = make_url(raw_url)
    except (ArgumentError, ValueError):
        raise pytest.UsageError(f"{TEST_DATABASE_ENV} is not a valid database URL.") from None

    if (
        test_url.drivername != "postgresql+psycopg"
        or not test_url.host
        or re.fullmatch(r"[A-Za-z0-9.:-]+", test_url.host) is None
        or test_url.query
        or re.fullmatch(r"[a-z][a-z0-9_]{0,57}_test", test_url.database or "") is None
    ):
        raise pytest.UsageError(
            f"{TEST_DATABASE_ENV} must use postgresql+psycopg, an explicit host, "
            "and a database name ending in _test, without URL query parameters."
        )

    if os.environ.get("PGHOSTADDR") or os.environ.get("PGSERVICE"):
        raise pytest.UsageError("Unset PGHOSTADDR and PGSERVICE before running pytest.")

    from agent_commons.config import settings

    try:
        app_url = make_url(settings.database_url)
    except (ArgumentError, ValueError):
        raise pytest.UsageError("Cannot verify the configured application database URL.") from None

    if not app_url.database:
        raise pytest.UsageError("The application database URL must specify a database name.")

    if test_url.database in (app_url.database, *app_url.normalized_query.get("dbname", ())):
        raise pytest.UsageError(
            "The test database name must differ from the application database name."
        )

    # Only pytest redirects the application engine. Alembic and app startup still
    # use their ordinary configuration when launched outside this process.
    settings.database_url = raw_url
    from agent_commons.db import engine

    if engine.url != test_url:
        raise pytest.UsageError("The application engine was initialized before test isolation.")
