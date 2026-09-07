from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from turkey_domain_replacer.adapters.fake import FakeCloudProvider, FakeDomainInventorySource
from turkey_domain_replacer.auth import AuthService, SessionStore
from turkey_domain_replacer.commands import CommandService
from turkey_domain_replacer.config import Settings
from turkey_domain_replacer.db import connect
from turkey_domain_replacer.migrations import migrate
from turkey_domain_replacer.models import DomainInventory
from turkey_domain_replacer.planner import AwsInventorySnapshot
from turkey_domain_replacer.repository import Repository
from turkey_domain_replacer.web import create_app
from turkey_domain_replacer.worker import Worker

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "aws_inventory.json"


class UnusedOidc:
    def authorization_url(self, state, nonce):
        return "https://accounts.google.test/auth"

    def exchange_and_verify(self, *args):
        raise AssertionError("unused")


def setup_web(tmp_path):
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
        connection, idle_ttl=timedelta(hours=1), absolute_ttl=timedelta(hours=8)
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
    source = FakeDomainInventorySource(
        DomainInventory("old.example", ("new.example", "later.example"), "rev-1", NOW)
    )
    commands = CommandService(repository)
    app = create_app(
        settings,
        repository=repository,
        commands=commands,
        auth=auth,
        inventory_source=source,
        clock=lambda: NOW,
    )
    app.testing = True
    client = app.test_client()
    session = sessions.create("agent@example.com", NOW)
    client.set_cookie("tdr_session", session.cookie_token)
    client.set_cookie("tdr_csrf", session.csrf_token)
    cloud = FakeCloudProvider(
        AwsInventorySnapshot.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))
    )
    worker = Worker(repository, cloud, source, labels=("fun", "joy"), worker_id="worker-1")
    headers = {"Origin": "http://localhost"}
    return app, client, session, repository, worker, headers


def test_start_page_has_no_customer_controlled_domain_field(tmp_path):
    _app, client, _session, _repository, _worker, _headers = setup_web(tmp_path)

    body = client.get("/").get_data(as_text=True)
    input_names = re.findall(r'<input[^>]+name="([^"]+)"', body)

    assert input_names
    assert set(input_names) == {"csrf_token"}
    assert "開始取得下一組域名" in body


def test_server_selected_pair_is_shown_before_preparation_confirmation(tmp_path):
    _app, client, session, _repository, _worker, headers = setup_web(tmp_path)

    response = client.post("/flows/start", data={"csrf_token": session.csrf_token}, headers=headers)
    body = client.get(response.headers["Location"]).get_data(as_text=True)

    assert "old.example" in body
    assert "new.example" in body
    assert "確認，開始準備新域名" in body


def test_cleanup_page_shows_exact_planned_impact_and_old_domain_input(tmp_path):
    _app, client, session, repository, worker, headers = setup_web(tmp_path)
    response = client.post("/flows/start", data={"csrf_token": session.csrf_token}, headers=headers)
    flow_id = response.headers["Location"].rsplit("/", 1)[1]
    client.post(
        f"/flows/{flow_id}/confirm-preparation",
        data={"csrf_token": session.csrf_token},
        headers=headers,
    )
    worker.run_once(NOW)
    client.post(
        f"/flows/{flow_id}/confirm-backoffice",
        data={"csrf_token": session.csrf_token},
        headers=headers,
    )
    worker.run_once(NOW)

    body = client.get(f"/flows/{flow_id}").get_data(as_text=True)

    assert "將清理的舊資源" in body
    assert "移除舊域名解析紀錄（4 項）" in body
    assert "刪除未使用的舊憑證（1 項）" in body
    for page in (
        body,
        client.get("/").get_data(as_text=True),
        client.get("/history").get_data(as_text=True),
    ):
        for internal_identifier in (
            "ZONE-OLD",
            "DIST-FUN",
            "DIST-JOY",
            "arn:aws:",
            "111111111111",
            "us-east-1",
            "certificate/old",
        ):
            assert internal_identifier not in page
    assert any("arn:aws:" in row["resource_id"] for row in repository.planned_operations(flow_id))
    assert 'name="old_domain"' in body
    assert "new.example" not in "\n".join(
        item["resource_id"] for item in repository.planned_operations(flow_id)
    )


def test_web_route_map_contains_no_retry_unlock_or_account_admin(tmp_path):
    app, _client, _session, _repository, _worker, _headers = setup_web(tmp_path)

    routes = {rule.rule for rule in app.url_map.iter_rules()}

    assert not any(word in route for route in routes for word in ("retry", "unlock", "accounts"))
