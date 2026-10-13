"""Integration tests against a real local PostgreSQL.

Run with TEST_DATABASE_URL set, e.g. inside Docker:
  docker compose run --rm -e TEST_DATABASE_URL=postgresql+psycopg://eventalpha:eventalpha_local_dev@db:5432/eventalpha_test api pytest
"""

import os

import pytest
from sqlalchemy import text

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(config, items):
    if TEST_DB:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set (integration tests need PostgreSQL)")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def engine():
    from alembic import command
    from alembic.config import Config

    from app.db import configure_engine

    os.environ["DATABASE_URL"] = TEST_DB
    from app.config import get_settings

    get_settings.cache_clear()
    eng = configure_engine(TEST_DB)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "..", "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB)
    command.upgrade(cfg, "head")
    yield eng


@pytest.fixture
def client(engine):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def run_jobs(engine):
    from app.worker.main import run_one

    def _run(max_jobs: int = 50) -> int:
        n = 0
        while n < max_jobs and run_one("test-worker"):
            n += 1
        return n

    return _run
