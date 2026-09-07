from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from turkey_domain_replacer.auth import AuthError, AuthService, SessionStore
from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class FakeOidcProvider:
    def __init__(self, email: str = "agent@example.com", *, verified: bool = True) -> None:
        self.email = email
        self.verified = verified
        self.verifications = []

    def authorization_url(self, state: str, nonce: str) -> str:
        return f"https://accounts.google.test/auth?state={state}&nonce={nonce}"

    def exchange_and_verify(self, code, nonce, issuer, audience, now):
        self.verifications.append((code, nonce, issuer, audience, now))
        return {"email": self.email, "email_verified": self.verified}


def make_auth(tmp_path, provider=None):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    sessions = SessionStore(
        connection, idle_ttl=timedelta(minutes=30), absolute_ttl=timedelta(hours=8)
    )
    auth = AuthService(
        connection,
        provider or FakeOidcProvider(),
        sessions,
        allowed_emails=frozenset({"agent@example.com"}),
        issuer="https://accounts.google.com",
        audience="client-id.apps.googleusercontent.com",
        transaction_ttl=timedelta(minutes=5),
    )
    return connection, auth, sessions


def test_oauth_transaction_stores_hashes_and_verifies_contract(tmp_path):
    connection, auth, _sessions = make_auth(tmp_path)

    login = auth.begin_login(NOW)
    result = auth.finish_login(login.state, "one-time-code", NOW + timedelta(seconds=10))

    row = connection.execute(
        "SELECT state_hash, nonce_hash, consumed_at FROM oauth_transactions"
    ).fetchone()
    assert row["state_hash"] == hashlib.sha256(login.state.encode()).hexdigest()
    assert row["nonce_hash"] == hashlib.sha256(login.nonce.encode()).hexdigest()
    assert login.state not in tuple(row)
    assert login.nonce not in tuple(row)
    assert row["consumed_at"] is not None
    assert result.email == "agent@example.com"
    assert auth.provider.verifications == [
        (
            "one-time-code",
            login.nonce,
            "https://accounts.google.com",
            "client-id.apps.googleusercontent.com",
            NOW + timedelta(seconds=10),
        )
    ]


def test_oauth_state_is_one_time(tmp_path):
    _connection, auth, _sessions = make_auth(tmp_path)
    login = auth.begin_login(NOW)
    auth.finish_login(login.state, "code", NOW)

    with pytest.raises(AuthError, match="state"):
        auth.finish_login(login.state, "code", NOW)


@pytest.mark.parametrize(
    ("email", "verified"),
    [
        ("attacker@example.com", True),
        ("agent@example.com.attacker.net", True),
        ("agent@example.com", False),
    ],
)
def test_oauth_requires_verified_exact_allowlisted_email(tmp_path, email, verified):
    _connection, auth, _sessions = make_auth(
        tmp_path, FakeOidcProvider(email=email, verified=verified)
    )
    login = auth.begin_login(NOW)

    with pytest.raises(AuthError, match="authorized"):
        auth.finish_login(login.state, "code", NOW)


def test_session_tokens_are_hashed_and_enforce_idle_expiry(tmp_path):
    connection, _auth, sessions = make_auth(tmp_path)
    created = sessions.create("agent@example.com", NOW)

    assert created.cookie_token not in tuple(
        connection.execute("SELECT token_hash FROM web_sessions").fetchone()
    )
    assert (
        sessions.authenticate(created.cookie_token, NOW + timedelta(minutes=29)).email
        == "agent@example.com"
    )
    assert sessions.authenticate(created.cookie_token, NOW + timedelta(minutes=60)) is None


def test_concurrent_callbacks_can_consume_oauth_state_only_once(tmp_path):
    path = tmp_path / "state.db"
    seed = connect(path)
    migrate(seed)
    seed_sessions = SessionStore(
        seed, idle_ttl=timedelta(minutes=30), absolute_ttl=timedelta(hours=8)
    )
    seed_auth = AuthService(
        seed,
        FakeOidcProvider(),
        seed_sessions,
        allowed_emails=frozenset({"agent@example.com"}),
        issuer="https://accounts.google.com",
        audience="client-id",
        transaction_ttl=timedelta(minutes=5),
    )
    login = seed_auth.begin_login(NOW)
    seed.close()
    select_barrier = threading.Barrier(2)

    class BarrierConnection:
        def __init__(self):
            self.raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self.raw.row_factory = sqlite3.Row
            self.raw.execute("PRAGMA busy_timeout = 5000")

        def execute(self, statement, parameters=()):
            cursor = self.raw.execute(statement, parameters)
            if statement.startswith("SELECT * FROM oauth_transactions"):
                select_barrier.wait(timeout=2)
            return cursor

    def finish():
        connection = BarrierConnection()
        sessions = SessionStore(
            connection, idle_ttl=timedelta(minutes=30), absolute_ttl=timedelta(hours=8)
        )
        auth = AuthService(
            connection,
            FakeOidcProvider(),
            sessions,
            allowed_emails=frozenset({"agent@example.com"}),
            issuer="https://accounts.google.com",
            audience="client-id",
            transaction_ttl=timedelta(minutes=5),
        )
        try:
            auth.finish_login(login.state, "code", NOW)
            return "success"
        except AuthError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: finish(), range(2)))

    assert sorted(results) == ["rejected", "success"]
