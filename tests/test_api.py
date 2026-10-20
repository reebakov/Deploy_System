import base64

import pytest
from fastapi.testclient import TestClient

import inventory_manager
import mm_server
import security
import settings_store
from database import DeployLog, session_scope
from statuses import Status

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret-pass").decode()}


@pytest.fixture
def client(monkeypatch, tmp_path):
    security.reset_rate_limits()
    monkeypatch.setattr(mm_server, "run_and_wait_detached", lambda *a, **k: None)
    inv = tmp_path / "inv"
    inv.write_text("[smr_clubgg_mobidesk]\nA105\n", encoding="utf-8")
    monkeypatch.setattr(inventory_manager, "INVENTORY_PATH", inv)
    monkeypatch.setattr(inventory_manager, "INVENTORY_LOCK_PATH", tmp_path / "inv.lock")
    monkeypatch.setattr(mm_server, "INVENTORY_PATH", inv)
    monkeypatch.setattr(settings_store, "STATE_DIR", tmp_path)
    # без `with` lifespan (планировщик) не запускается
    return TestClient(mm_server.app)


def csrf(client):
    html = client.get("/", headers=AUTH).text
    return html.split('name="csrf-token" content="')[1].split('"')[0]


def test_requires_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/health").status_code == 200


def test_non_ascii_credentials_do_not_crash(client):
    assert client.get("/", auth=("админ", "пароль")).status_code == 401


def test_bruteforce_lockout(client):
    for _ in range(5):
        assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/", headers=AUTH).status_code == 429


def test_post_without_csrf_forbidden(client):
    r = client.post("/manual_deploy", headers=AUTH, follow_redirects=False,
                    data={"target_pcs": "A105", "deploy_type": "room_only"})
    assert r.status_code == 403


def test_manual_deploy_ok_and_audited(client):
    token = csrf(client)
    r = client.post("/manual_deploy", headers=AUTH, follow_redirects=False,
                    data={"target_pcs": "A105, A106", "deploy_type": "room_only", "csrf_token": token})
    assert r.status_code == 303
    with session_scope() as db:
        entry = db.query(DeployLog).one()
        assert entry.triggered_by == "admin"
        assert entry.target_pcs == "A105,A106"
        assert "ansible-playbook" in entry.command


def test_manual_deploy_rejects_injection(client):
    token = csrf(client)
    for pcs, tags in (("A105,-e", ""), ("A105", "x --extra-vars @f")):
        r = client.post("/manual_deploy", headers=AUTH, follow_redirects=False,
                        data={"target_pcs": pcs, "deploy_type": "full", "skip_tags": tags, "csrf_token": token})
        assert r.status_code == 400
    with session_scope() as db:
        assert db.query(DeployLog).count() == 0


def test_busy_blocks_second_deploy(client):
    token = csrf(client)
    data = {"target_pcs": "A105", "deploy_type": "room_only", "csrf_token": token}
    assert client.post("/manual_deploy", headers=AUTH, data=data, follow_redirects=False).status_code == 303
    assert client.post("/manual_deploy", headers=AUTH, data=data, follow_redirects=False).status_code == 400


def test_settings_whitelist(client):
    h = {**AUTH, "X-CSRF-Token": csrf(client)}
    bad = client.post("/api/settings", headers=h, json={"full_args": "-e x=y", "room_args": "--tags a"})
    assert bad.status_code == 400
    ok = client.post("/api/settings", headers=h,
                     json={"bot_enabled": True, "full_args": "--skip-tags a", "room_args": "--tags b"})
    assert ok.status_code == 200
    got = client.get("/api/settings", headers=AUTH).json()
    assert got == {"bot_enabled": True, "full_args": "--skip-tags a", "room_args": "--tags b"}


def test_cancel_only_active(client):
    token = csrf(client)
    with session_scope() as db:
        db.add(DeployLog(jira_key="k", task_type="t", target_pcs="A1", status=Status.SUCCESS, pid=1))
    r = client.post("/cancel/1", headers={**AUTH, "X-CSRF-Token": token})
    assert r.json() == {"cancelled": False}


def test_metrics(client):
    assert "deploy_total" in client.get("/metrics", headers=AUTH).text


def test_weak_password_rejected(monkeypatch):
    import config
    monkeypatch.setattr(config, "DASH_PASS", "changeme")
    with pytest.raises(RuntimeError):
        config.validate_config()
