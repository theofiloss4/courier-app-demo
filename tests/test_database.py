# =============================================================================
# Unit tests for the startup database-connection retry logic in
# app/database.py (_wait_for_database). This guards against exactly the
# Docker Compose startup race condition reported in production: PostgreSQL
# is healthy according to Compose, but the application container's own
# DNS/network view is not instantly consistent the moment its process
# starts, causing a transient connection failure that used to crash the
# whole app immediately.
# =============================================================================
import pytest
from sqlalchemy.exc import OperationalError

from app import database


class _FakeConnection:
    """Minimal stand-in for the object returned by Engine.connect(), only
    supporting the `with engine.connect():` usage in _wait_for_database.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_wait_for_database_retries_transient_failures(monkeypatch):
    """A connection that fails twice (simulating DNS not resolvable yet)
    and then succeeds must be retried transparently - the function should
    return normally instead of raising on the first failure.
    """
    attempts = []

    def fake_connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise OperationalError("connect", {}, Exception("name resolution failed"))
        return _FakeConnection()

    monkeypatch.setattr(database.engine, "connect", fake_connect)
    # Removes the real delay from the test - only the retry COUNT matters here.
    monkeypatch.setattr(database.time, "sleep", lambda _seconds: None)

    database._wait_for_database(max_attempts=5, delay_seconds=0)

    assert len(attempts) == 3


def test_wait_for_database_gives_up_after_max_attempts(monkeypatch):
    """A permanently unreachable database (wrong host, firewall, etc.) must
    eventually raise instead of retrying forever - the retry exists to
    absorb a few seconds of startup timing noise, not to mask a genuine
    misconfiguration.
    """

    def always_fail():
        raise OperationalError("connect", {}, Exception("connection refused"))

    monkeypatch.setattr(database.engine, "connect", always_fail)
    monkeypatch.setattr(database.time, "sleep", lambda _seconds: None)

    with pytest.raises(OperationalError):
        database._wait_for_database(max_attempts=3, delay_seconds=0)
