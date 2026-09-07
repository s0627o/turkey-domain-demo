import pytest

from tests.test_web_workflow import NOW, setup_web
from turkey_domain_replacer.commands import CommandService
from turkey_domain_replacer.models import FlowStatus


def cleanup_ready(tmp_path):
    app, client, session, repo, worker, headers = setup_web(tmp_path)
    response = client.post("/flows/start", data={"csrf_token": session.csrf_token}, headers=headers)
    flow_id = response.headers["Location"].rsplit("/", 1)[-1]
    commands = CommandService(repo)
    commands.confirm_preparation(flow_id, session.email, NOW)
    worker.run_once(NOW)
    commands.confirm_backoffice_switch(flow_id, session.email, NOW)
    worker.run_once(NOW)
    return app, client, session, repo, worker, headers, flow_id


@pytest.mark.parametrize("candidate", ["new.example", "", "<script>alert(1)</script>"])
def test_wrong_domain_is_inline_and_does_not_start_cleanup(tmp_path, candidate):
    _, client, session, repo, _, headers, flow_id = cleanup_ready(tmp_path)
    response = client.post(
        f"/flows/{flow_id}/confirm-cleanup",
        data={"csrf_token": session.csrf_token, "old_domain": candidate},
        headers=headers,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 422
    assert "請輸入下方顯示的舊主域名" in body
    assert "old.example" in body
    assert 'aria-invalid="true"' in body
    assert "<script>" not in body
    assert repo.get_flow(flow_id).status is FlowStatus.AWAITING_CLEANUP_CONFIRMATION
    assert (
        repo.connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='CLEANUP'").fetchone()[0] == 0
    )
    assert repo.get_lock().flow_id == flow_id


def test_corrected_form_and_duplicate_submission_create_one_job(tmp_path):
    _, client, session, repo, worker, headers, flow_id = cleanup_ready(tmp_path)
    for candidate in ["new.example", "old.example", "old.example"]:
        response = client.post(
            f"/flows/{flow_id}/confirm-cleanup",
            data={"csrf_token": session.csrf_token, "old_domain": candidate},
            headers=headers,
        )
        assert response.status_code == (422 if candidate == "new.example" else 303)
    assert (
        repo.connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='CLEANUP'").fetchone()[0] == 1
    )
    worker.run_once(NOW)
    response = client.post(
        f"/flows/{flow_id}/confirm-cleanup",
        data={"csrf_token": session.csrf_token, "old_domain": "old.example"},
        headers=headers,
    )
    assert response.status_code == 409
    assert "流程已進入其他階段" in response.get_data(as_text=True)


def test_security_rejection_has_recovery_links_and_preserves_progress(tmp_path):
    _, client, session, repo, _, headers, flow_id = cleanup_ready(tmp_path)
    response = client.post(
        f"/flows/{flow_id}/confirm-cleanup",
        data={"csrf_token": "stale", "old_domain": "old.example"},
        headers=headers,
    )
    assert response.status_code == 403
    body = response.get_data(as_text=True)
    assert "這次操作未執行" in body
    assert "返回查看進度" in body and "重新登入" in body
    assert "Forbidden" not in body
    assert response.headers["Cache-Control"] == "no-store"
    assert repo.get_flow(flow_id).status is FlowStatus.AWAITING_CLEANUP_CONFIRMATION


def test_unknown_flow_and_missing_login_have_chinese_pages(tmp_path):
    _, client, _, _, _, _ = setup_web(tmp_path)
    response = client.get("/flows/not-found")
    assert response.status_code == 404
    assert "找不到這個頁面或流程" in response.get_data(as_text=True)
    client.delete_cookie("tdr_session")
    response = client.get("/")
    assert response.status_code == 401
    assert "請重新登入後繼續" in response.get_data(as_text=True)
