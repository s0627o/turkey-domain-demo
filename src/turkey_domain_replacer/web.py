from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from flask import (
    Flask,
    Response,
    abort,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from .auth import AuthError, AuthService, WebSession
from .commands import CommandService, DomainConfirmationError, InvalidTransition
from .config import Settings
from .db import connect
from .models import FlowStatus
from .presentation import (
    backoffice_test_urls,
    operation_label,
    stage_description,
    stage_label,
    taipei_time,
)
from .repository import ActiveFlowExists, FlowLockUnavailable, InventoryUnavailable, Repository


class InventorySource(Protocol):
    def read_inventory(self): ...


def create_app(
    settings: Settings,
    *,
    repository: Repository,
    commands: CommandService,
    auth: AuthService,
    inventory_source: InventorySource,
    clock: Callable[[], datetime],
) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters.update(
        stage_label=stage_label,
        stage_description=stage_description,
        taipei_time=taipei_time,
        operation_label=operation_label,
    )
    database_row = repository.connection.execute("PRAGMA database_list").fetchone()
    database_path = database_row["file"]
    if not database_path:
        raise ValueError("request-scoped Web access requires a file-backed SQLite database")

    @app.before_request
    def open_request_database() -> None:
        connection = connect(Path(database_path))
        g.database_connection = connection
        g.repository = Repository(connection)
        g.commands = CommandService(g.repository)
        g.auth = auth.with_connection(connection)

    @app.teardown_request
    def close_request_database(_error) -> None:
        connection = g.pop("database_connection", None)
        if connection is not None:
            connection.close()

    @app.before_request
    def enforce_security() -> Response | None:
        if request.endpoint in {"login", "oauth_callback", "static"}:
            return None
        session = _auth().sessions.authenticate(request.cookies.get("tdr_session"), clock())
        if session is None:
            abort(401)
        g.web_session = session
        if request.method == "POST":
            if request.mimetype != "application/x-www-form-urlencoded":
                abort(415)
            expected_origin = settings.public_origin or request.host_url.rstrip("/")
            if request.headers.get("Origin", "").rstrip("/") != expected_origin:
                abort(403)
            if not _auth().sessions.validate_csrf(session, request.form.get("csrf_token")):
                abort(403)
        return None

    @app.before_request
    def check_flow_exists():
        flow_id = (request.view_args or {}).get("flow_id")
        if flow_id:
            try:
                _repository().get_flow(flow_id)
            except KeyError:
                abort(404)

    @app.after_request
    def prevent_stale_forms(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    def error_page(status, message):
        session = g.get("web_session")
        flow_id = (request.view_args or {}).get("flow_id")
        return render_template(
            "error.html",
            message=message,
            back_url=url_for("flow_detail", flow_id=flow_id)
            if flow_id and status not in {401, 404}
            else url_for("index"),
            login_url=url_for("login"),
            email=session.email if session else None,
            csrf_token=request.cookies.get("tdr_csrf", ""),
        ), status

    @app.errorhandler(401)
    def login_required(_error):
        return error_page(401, "登入已失效，請重新登入後繼續。流程進度仍會保留。")

    @app.errorhandler(403)
    def form_rejected(_error):
        return error_page(
            403,
            "頁面驗證未通過，這次操作未執行。請返回流程重新載入頁面；若仍無法操作，請重新登入。",
        )

    @app.errorhandler(404)
    def not_found(_error):
        return error_page(404, "找不到這個頁面或流程，請返回首頁查看目前進度。")

    @app.errorhandler(405)
    @app.errorhandler(415)
    def invalid_request(error):
        return error_page(error.code, "無法使用這個方式送出操作，請返回頁面後使用畫面上的按鈕。")

    @app.errorhandler(InvalidTransition)
    @app.errorhandler(FlowLockUnavailable)
    def stale_transition(_error):
        return error_page(
            409, "流程已進入其他階段，這次操作未執行。請返回流程查看最新進度，無需重複送出。"
        )

    @app.errorhandler(500)
    def unexpected_error(_error):
        return error_page(
            500, "系統暫時無法處理，請返回流程查看進度。若問題持續，請聯絡管理者，勿重複提交。"
        )

    @app.get("/login")
    def login():
        return redirect(_auth().begin_login(clock()).authorization_url, code=303)

    @app.get("/oauth/callback")
    def oauth_callback():
        try:
            session = _auth().finish_login(
                request.args.get("state", ""), request.args.get("code", ""), clock()
            )
        except AuthError:
            abort(403)
        response = make_response(redirect(url_for("index"), code=303))
        response.set_cookie(
            "tdr_session",
            session.cookie_token,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="Lax",
        )
        response.set_cookie(
            "tdr_csrf",
            session.csrf_token,
            secure=settings.cookie_secure,
            httponly=False,
            samesite="Strict",
        )
        return response

    @app.get("/")
    def index():
        lock = _repository().get_lock()
        flow = _repository().get_flow(lock.flow_id) if lock else None
        return render_template(
            "index.html",
            flow=flow,
            csrf_token=request.cookies.get("tdr_csrf", ""),
            email=_session().email,
        )

    @app.post("/flows/start")
    def start_flow():
        session = _session()
        try:
            flow = _repository().start_flow(
                session.email, inventory_source.read_inventory(), clock()
            )
        except ActiveFlowExists:
            return error_page(409, "目前已有進行中的替換，請返回首頁查看。")
        except InventoryUnavailable:
            return error_page(409, "目前沒有可用的備用域名，或域名資料需要確認。請聯絡管理者。")
        return redirect(url_for("flow_detail", flow_id=flow.id), code=303)

    @app.get("/flows/<flow_id>")
    def flow_detail(flow_id: str):
        return render_flow(flow_id)

    def render_flow(flow_id, *, domain_error=None, submitted_domain=""):
        detail = _repository().flow_detail(flow_id)
        impact_counts = Counter(
            operation_label(row["operation_type"])
            for row in _repository().planned_operations(flow_id)
        )
        return render_template(
            "detail.html",
            detail=detail,
            impact=tuple(impact_counts.items()),
            statuses=FlowStatus,
            csrf_token=request.cookies.get("tdr_csrf", ""),
            email=_session().email,
            domain_error=domain_error,
            submitted_domain=submitted_domain,
            backoffice_test_urls=backoffice_test_urls(detail.flow.new_domain),
        )

    @app.post("/flows/<flow_id>/confirm-preparation")
    def confirm_preparation(flow_id: str):
        _commands().confirm_preparation(flow_id, _session().email, clock())
        return redirect(url_for("flow_detail", flow_id=flow_id), code=303)

    @app.post("/flows/<flow_id>/confirm-backoffice")
    def confirm_backoffice(flow_id: str):
        _commands().confirm_backoffice_switch(flow_id, _session().email, clock())
        return redirect(url_for("flow_detail", flow_id=flow_id), code=303)

    @app.post("/flows/<flow_id>/confirm-cleanup")
    def confirm_cleanup(flow_id: str):
        submitted_domain = request.form.get("old_domain", "")
        try:
            _commands().confirm_cleanup(flow_id, submitted_domain, _session().email, clock())
        except DomainConfirmationError:
            return render_flow(
                flow_id,
                domain_error="請輸入下方顯示的舊主域名，不能輸入新域名。",
                submitted_domain=submitted_domain,
            ), 422
        return redirect(url_for("flow_detail", flow_id=flow_id), code=303)

    @app.get("/history")
    def history():
        return render_template(
            "history.html",
            flows=_repository().history(),
            csrf_token=request.cookies.get("tdr_csrf", ""),
            email=_session().email,
        )

    @app.post("/logout")
    def logout():
        _auth().sessions.revoke(_session(), clock())
        response = make_response(redirect(url_for("login"), code=303))
        response.delete_cookie("tdr_session")
        response.delete_cookie("tdr_csrf")
        return response

    return app


def _session() -> WebSession:
    return g.web_session


def _repository() -> Repository:
    return g.repository


def _commands() -> CommandService:
    return g.commands


def _auth() -> AuthService:
    return g.auth


def main() -> None:
    raise SystemExit("Use a production WSGI server after deployment approval")
