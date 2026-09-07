"""Loopback-only interactive demo. No cloud credentials or external clients."""

import argparse
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from flask import g, request
from werkzeug.serving import WSGIRequestHandler

from .adapters.fake import FakeCloudProvider, FakeDomainInventorySource
from .auth import AuthError, AuthService, SessionStore
from .commands import CommandService
from .config import Settings
from .db import connect
from .migrations import migrate
from .models import DomainInventory
from .planner import (
    AwsInventorySnapshot,
    Certificate,
    Distribution,
    DnsRecord,
    HostedZone,
    RegisteredDomain,
)
from .repository import Repository
from .web import create_app
from .worker import Worker


def now():
    return datetime.now(timezone.utc)


class PreviewLogin:
    def authorization_url(self, state, nonce):
        return "/oauth/callback?" + urlencode({"state": state, "code": "preview"})

    def exchange_and_verify(self, code, nonce, issuer, audience, timestamp):
        if code != "preview":
            raise AuthError("Invalid preview login")
        return {"email": "preview@example.com", "email_verified": True}


def preview_inventory():
    labels = ("fun", "joy")
    return AwsInventorySnapshot(
        complete=True,
        distributions=tuple(
            Distribution("DEMO-" + label, (f"{label}.old.example",)) for label in labels
        ),
        certificates=(Certificate("demo-certificate", ("*.old.example",), ()),),
        hosted_zones=(
            HostedZone(
                "DEMO-OLD",
                "old.example",
                (DnsRecord("old.example", "NS"), DnsRecord("old.example", "SOA"))
                + tuple(
                    DnsRecord(f"{label}.old.example", kind)
                    for label in labels
                    for kind in ("A", "AAAA")
                ),
            ),
            HostedZone(
                "DEMO-NEW",
                "new.example",
                (DnsRecord("new.example", "NS"), DnsRecord("new.example", "SOA")),
            ),
        ),
        registered_domains=(
            RegisteredDomain("old.example", True),
            RegisteredDomain("new.example", True),
        ),
    )


def create_preview(state_dir: Path, port: int = 20203, *, public_origin: str | None = None):
    if public_origin is not None and not re.fullmatch(
        r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.app\.github\.dev", public_origin
    ):
        raise ValueError("Expected an exact Codespaces HTTPS origin without path or credentials")
    settings = Settings.from_env(
        {
            "TDR_ALLOWED_EMAILS": "preview@example.com",
            "TDR_PUBLIC_ORIGIN": public_origin or f"http://127.0.0.1:{port}",
        },
        base_dir=state_dir,
    )
    if public_origin:
        settings = replace(settings, cookie_secure=True)
    connection = connect(state_dir / "preview.db")
    migrate(connection)
    repository = Repository(connection)
    sessions = SessionStore(
        connection, idle_ttl=timedelta(hours=8), absolute_ttl=timedelta(hours=24)
    )
    auth = AuthService(
        connection,
        PreviewLogin(),
        sessions,
        allowed_emails=settings.allowed_emails,
        issuer="preview",
        audience="preview",
        transaction_ttl=timedelta(minutes=5),
    )
    source = FakeDomainInventorySource(
        DomainInventory("old.example", ("new.example", "later.example"), "preview-1", now())
    )
    cloud = FakeCloudProvider(preview_inventory())
    app = create_app(
        settings,
        repository=repository,
        commands=CommandService(repository),
        auth=auth,
        inventory_source=source,
        clock=now,
    )
    app.jinja_env.auto_reload = True
    connection.close()

    if public_origin:

        def normalize_demo_tunnel_origin():
            # Codespaces rewrites Host and Origin to its loopback target.
            # Only this fake-data preview accepts that exact local transport;
            # session and CSRF validation in web.py still run unchanged.
            if (
                request.remote_addr == "127.0.0.1"
                and request.host == f"localhost:{port}"
                and request.headers.get("Origin") == f"http://localhost:{port}"
            ):
                request.environ["HTTP_ORIGIN"] = public_origin

        app.before_request_funcs[None].insert(0, normalize_demo_tunnel_origin)

    @app.after_request
    def simulate_work(response):
        # Synchronous only in this demo, so each confirmation shows the next screen.
        if request.method == "POST" and response.status_code == 303:
            Worker(
                g.repository, cloud, source, labels=("fun", "joy"), worker_id="preview", clock=now
            ).run_once(now())
        if response.mimetype == "text/html":
            banner = (
                '<div style="background:#fff2cb;color:#5e4200;padding:12px 24px;'
                'text-align:center">模擬展示版 · 全部操作使用假資料，不影響真實域名</div>'
            )
            response.set_data(response.get_data(as_text=True).replace("<body>", "<body>" + banner))
        return response

    return app


class PreviewRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # Never log OAuth state, cookies or form values.
        self.log("info", "%s %s %s", self.command, self.path.split("?", 1)[0], code)


def main():
    parser = argparse.ArgumentParser(description="本機模擬展示版（假資料）")
    parser.add_argument("--port", type=int, default=20203)
    parser.add_argument("--public-origin", help="指定 Codespaces HTTPS 展示網址（不含路徑）")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    with TemporaryDirectory(prefix="turkey-domain-preview-") as directory:
        app = create_preview(Path(directory), args.port, public_origin=args.public_origin)
        origin = args.public_origin or f"http://127.0.0.1:{args.port}"
        print(f"模擬介面：{origin}/login")
        app.run(
            host="127.0.0.1",
            port=args.port,
            debug=False,
            use_reloader=False,
            threaded=False,
            request_handler=PreviewRequestHandler,
        )


if __name__ == "__main__":
    main()
