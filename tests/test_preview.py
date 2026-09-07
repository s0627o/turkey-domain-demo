import re

import pytest

from turkey_domain_replacer.preview import create_preview


@pytest.mark.parametrize("origin", [None, "https://demo-20203.app.github.dev"])
def test_preview_runs_whole_flow_without_network_and_hides_provider_ids(tmp_path, origin):
    app = create_preview(tmp_path, public_origin=origin)
    app.testing = True
    if origin:
        app.config.update(SERVER_NAME="demo-20203.app.github.dev", PREFERRED_URL_SCHEME="https")
    client = app.test_client()
    response = client.get("/login", follow_redirects=True)
    assert response.status_code == 200
    assert "模擬展示版" in response.get_data(as_text=True)
    domain = "demo-20203.app.github.dev" if origin else "localhost"
    csrf = client.get_cookie("tdr_csrf", domain=domain).value
    assert client.get_cookie("tdr_session", domain=domain).secure == bool(origin)
    headers = {"Origin": origin or "http://127.0.0.1:20203"}

    assert (
        client.post(
            "/flows/start",
            data={"csrf_token": csrf},
            headers={"Origin": "https://other.app.github.dev"},
        ).status_code
        == 403
    )
    assert (
        client.post("/flows/start", data={"csrf_token": "wrong"}, headers=headers).status_code
        == 403
    )

    response = client.post("/flows/start", data={"csrf_token": csrf}, headers=headers)
    path = response.headers["Location"]
    response = client.post(
        path + "/confirm-preparation",
        data={"csrf_token": csrf},
        headers=headers,
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    timeline = body.split('<ol class="timeline">')[1]
    assert timeline.index("正在更新域名紀錄") < timeline.index("等待你切換公司後台")
    response = client.post(
        path + "/confirm-backoffice",
        data={"csrf_token": csrf},
        headers=headers,
        follow_redirects=True,
    )
    assert "將清理的舊資源" in response.get_data(as_text=True)
    assert "DEMO-" not in response.get_data(as_text=True)
    response = client.post(
        path + "/confirm-cleanup",
        data={"csrf_token": csrf, "old_domain": "new.example"},
        headers=headers,
    )
    assert response.status_code == 422
    response = client.post(
        path + "/confirm-cleanup",
        data={"csrf_token": csrf, "old_domain": "old.example"},
        headers=headers,
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert re.search(r'<p class="status">域名替換完成</p>', response.get_data(as_text=True))
    assert client.get("/history").status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "http://demo-20203.app.github.dev",
        "https://example.com",
        "https://demo.app.github.dev.evil.example",
        "https://user@demo.app.github.dev",
        "https://demo.app.github.dev/path",
        "https://demo.app.github.dev?x=1",
        "https://demo.app.github.dev#fragment",
        "https://demo.app.github.dev:443",
        "https://app.github.dev",
        "",
    ],
)
def test_preview_rejects_unsafe_remote_origins(tmp_path, origin):
    with pytest.raises(ValueError, match="Codespaces HTTPS origin"):
        create_preview(tmp_path, public_origin=origin)
    assert not (tmp_path / "preview.db").exists()
