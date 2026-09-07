from __future__ import annotations

from tests.test_web_workflow import setup_web


def test_history_is_read_only_table_with_required_columns_and_timeline(tmp_path):
    _app, client, session, _repository, _worker, headers = setup_web(tmp_path)
    response = client.post("/flows/start", data={"csrf_token": session.csrf_token}, headers=headers)
    flow_id = response.headers["Location"].rsplit("/", 1)[1]

    history = client.get("/history").get_data(as_text=True)
    detail = client.get(f"/flows/{flow_id}").get_data(as_text=True)
    post_response = client.post(
        "/history", data={"csrf_token": session.csrf_token}, headers=headers
    )

    for heading in ("時間", "舊域名 → 新域名", "Google email", "狀態", "備註"):
        assert heading in history
    assert "old.example" in history and "new.example" in history
    assert "操作進度紀錄" in detail
    assert "2026/09/04 20:00:00" in detail
    assert "台北時間" in detail
    assert "等待確認新域名" in detail
    assert "AWAITING_PREPARATION_CONFIRMATION" not in detail
    assert "Domain pair reserved" not in detail
    assert post_response.status_code == 405
