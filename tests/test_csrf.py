from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from turkey_domain_replacer.adapters.fake import FakeDomainInventorySource
from turkey_domain_replacer.auth import AuthService, SessionStore
from turkey_domain_replacer.commands import CommandService
from turkey_domain_replacer.config import Settings
from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory
from turkey_domain_replacer.repository import Repository
from turkey_domain_replacer.web import create_app

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class UnusedOidc:
    def authorization_url(self, state, nonce):
        return "https://accounts.google.test/auth"

    def exchange_and_verify(self, *args):
        raise AssertionError("OAuth exchange is not used in this test")


def make_client(tmp_path):
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    repository = Repository(connection)
    settings = Settings.from_env(
        {
            "TDR_ALLOWED_EMAILS": "agent@example.com",
            "TDR_PUBLIC_ORIGIN": "http://localhost",
        },
        base_dir=tmp_path,
    )
    sessions = SessionStore(
        connection, idle_ttl=timedelta(minutes=30), absolute_ttl=timedelta(hours=8)
    )
    auth = AuthService(
        connection,
        UnusedOidc(),
        sessions,
        allowed_emails=settings.allowed_emails,
        issuer="https://accounts.google.com",
        audience="client-id",
        transaction_ttl=timedelta(minutes=5),
    )
    inventory = FakeDomainInventorySource(
        DomainInventory("old.example", ("new.example", "later.example"), "rev-1", NOW)
    )
    app = create_app(
        settings,
        repository=repository,
        commands=CommandService(repository),
        auth=auth,
        inventory_source=inventory,
        clock=lambda: NOW,
    )
    app.testing = True
    client = app.test_client()
    session = sessions.create("agent@example.com", NOW)
    client.set_cookie("tdr_session", session.cookie_token)
    return client, session, connection


def test_every_workflow_route_requires_server_session(tmp_path):
    client, _session, _connection = make_client(tmp_path)
    client.delete_cookie("tdr_session")

    assert client.get("/").status_code == 401
    assert client.get("/history").status_code == 401
    assert client.post("/flows/start").status_code == 401


def test_state_changing_post_requires_form_content_origin_and_csrf(tmp_path):
    client, session, connection = make_client(tmp_path)

    assert client.post("/flows/start").status_code == 415
    assert client.post("/flows/start", data={"csrf_token": session.csrf_token}).status_code == 403
    assert (
        client.post(
            "/flows/start",
            data={"csrf_token": "wrong"},
            headers={"Origin": "http://localhost"},
        ).status_code
        == 403
    )
    response = client.post(
        "/flows/start",
        data={"csrf_token": session.csrf_token},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 303
    assert connection.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == 1


def test_logout_revokes_server_session(tmp_path):
    client, session, _connection = make_client(tmp_path)

    response = client.post(
        "/logout",
        data={"csrf_token": session.csrf_token},
        headers={"Origin": "http://localhost"},
    )

    assert response.status_code == 303
    assert client.get("/").status_code == 401


def test_each_web_request_uses_its_own_sqlite_connection(tmp_path):
    client, session, _connection = make_client(tmp_path)
    app = client.application
    barrier = threading.Barrier(2)

    def load_index(_):
        thread_client = app.test_client()
        thread_client.set_cookie("tdr_session", session.cookie_token)
        barrier.wait(timeout=2)
        return thread_client.get("/").status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(load_index, range(2)))

    assert statuses == [200, 200]
