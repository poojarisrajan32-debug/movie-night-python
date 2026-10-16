"""Integration-test plumbing: the real app against a real Postgres and Redis.

Where the services live comes from DATABASE_URL / REDIS_URL, so the same tests
run on a laptop, in Compose (host "db") and in CircleCI (host "localhost").
"""
import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import cache
from app.main import app

MIGRATIONS = sorted((Path(__file__).parents[2] / "migrations").glob("*.sql"))


@pytest.fixture(scope="session", autouse=True)
def services():
    """Fail loudly if the services aren't configured, then apply the migrations."""
    missing = [v for v in ("DATABASE_URL", "REDIS_URL") if not os.environ.get(v)]
    if missing:
        # fail, don't skip: a skipped suite looks green in CI while testing nothing
        pytest.fail(f"integration tests need {' and '.join(missing)} set", pytrace=False)
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        for path in MIGRATIONS:  # idempotent, so safe on a database that already has them
            conn.execute(path.read_text())


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture
def redis():
    return cache.client()


@pytest.fixture
def sql():
    def run(query, params=()):
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            return conn.execute(query, params).fetchall()
    return run


@pytest.fixture
def make_poll(client, redis):
    """Create polls through the API; whatever a test made is removed afterwards."""
    made = []

    def create(title="Friday film night", opened_by="tester"):
        r = client.post("/polls", json={"title": title, "opened_by": opened_by})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()

    yield create
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DELETE FROM polls WHERE id = ANY(%s)", (made,))
    for pid in made:
        redis.delete(f"poll:{pid}:first", f"lock:close:{pid}")


@pytest.fixture
def poll(make_poll):
    return make_poll()
