"""Kiểm thử với Postgres thật (pixeltable-pgserver) và một `kaggle` giả.

`kaggle` giả mô phỏng đúng định dạng của `kaggle quota --format json` (đối chiếu với mã
nguồn kaggle 2.2.x), có in thêm chữ hướng dẫn xác thực trước JSON như CLI thật hay làm.
"""
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FAKE_KAGGLE = r'''
import json, os, sys
# Tiến trình con không được thấy bí mật của ứng dụng
for k in ("DATABASE_URL", "SECRET_KEY", "ADMIN_PASSWORD"):
    if k in os.environ:
        sys.stderr.write("LEAK " + k); sys.exit(3)
if "kg_" not in os.environ.get("HOME", ""):
    sys.stderr.write("HOME không được cô lập"); sys.exit(3)
tok = os.environ.get("KAGGLE_API_TOKEN", "")
if tok.startswith("bad"):
    sys.stderr.write("401 Unauthorized"); sys.exit(1)
print("Authentication required to call the Kaggle API.\n  Option A: Environment variable")
args = sys.argv[1:]
if args[:1] == ["quota"]:
    print(json.dumps([
        {"resource": "GPU", "used": "16.33h", "remaining": "13.67h", "total": "30.00h", "refreshAt": "2026-10-24T00:00:00+00:00"},
        {"resource": "TPU", "used": "0.00h", "remaining": "20.00h", "total": "20.00h", "refreshAt": "2026-10-24T00:00:00+00:00"},
    ], indent=2))
elif args[:2] == ["kernels", "list"]:
    print("ref,title\nme/nb1,Notebook 1")
'''


@pytest.fixture(scope="session")
def pg_url():
    import pixeltable_pgserver as pg

    srv = pg.get_server(tempfile.mkdtemp(prefix="pgdata_"))
    yield srv.get_uri()
    srv.cleanup()


@pytest.fixture()
def client(pg_url, tmp_path, monkeypatch):
    import psycopg
    from cryptography.fernet import Fernet

    fake = tmp_path / "fake_kaggle.py"
    fake.write_text(FAKE_KAGGLE)
    monkeypatch.setenv("KAGGLE_CMD", f"{sys.executable} {fake}")
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_PASSWORD", "pw123")
    # Bảng sạch cho mỗi test
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    from backend.app import db, main

    monkeypatch.setattr(db, "_schema_ready", False)
    with TestClient(main.app) as c:
        yield c


def login(c, pw="pw123"):
    return c.post("/api/login", json={"password": pw})


def add(c, name, token="token-aaaaaaaa"):
    return c.post("/api/accounts", json={"name": name, "token": token})


def test_health_and_rls(client, pg_url):
    assert client.get("/api/health").json() == {"ok": True}
    import psycopg

    with psycopg.connect(pg_url) as c:
        rows = c.execute(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
            "('accounts','quota_snapshots','login_fails')"
        ).fetchall()
    assert len(rows) == 3 and all(r[1] for r in rows), "RLS phải bật trên cả 3 bảng"


def test_auth_required(client):
    assert client.get("/api/accounts").status_code == 401
    assert login(client, "nope").status_code == 401
    assert client.get("/api/accounts").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/accounts").status_code == 200


def test_add_and_quota(client):
    login(client)
    r = add(client, "acc1")
    assert r.status_code == 201 and r.json()["check"]["ok"], r.text
    accs = client.get("/api/accounts").json()
    q = accs[0]["quota"]
    assert q["gpu_used"] == 16.33 and q["gpu_total"] == 30.0
    assert q["tpu_used"] == 0.0 and q["tpu_total"] == 20.0
    assert q["refresh_at"].startswith("2026-10-24")
    assert not any("token" in k for k in accs[0])


def test_bad_token_reports_error(client):
    login(client)
    r = add(client, "bad", "bad-token-000")
    assert r.status_code == 201 and not r.json()["check"]["ok"]
    a = client.get("/api/accounts").json()[0]
    assert "401" in a["last_error"] and a["quota"] is None


def test_duplicate_name_and_delete(client):
    login(client)
    add(client, "a")
    assert add(client, "a", "token-bbbbbbbb").status_code == 409
    aid = client.get("/api/accounts").json()[0]["id"]
    assert client.get(f"/api/accounts/{aid}/kernels").json()["csv"].count("Notebook 1") == 1
    assert len(client.get(f"/api/accounts/{aid}/history").json()) == 1
    assert client.delete(f"/api/accounts/{aid}").status_code == 204
    assert client.get("/api/accounts").json() == []


def test_update_token_rechecks(client):
    login(client)
    add(client, "a", "bad-token-000")
    aid = client.get("/api/accounts").json()[0]["id"]
    r = client.patch(f"/api/accounts/{aid}", json={"token": "good-token-111"})
    assert r.json()["check"]["ok"]
    assert client.get("/api/accounts").json()[0]["last_error"] is None


def test_token_encrypted_at_rest(client, pg_url):
    login(client)
    add(client, "a", "super-secret-token")
    import psycopg

    with psycopg.connect(pg_url) as c:
        raw = c.execute("SELECT token_enc FROM accounts").fetchone()[0]
    assert "super-secret-token" not in raw


def test_refresh_all_summary_and_budget(client, monkeypatch):
    login(client)
    add(client, "a"); add(client, "b"); add(client, "c", "bad-token-000")
    assert client.post("/api/refresh-all").json() == {"total": 3, "done": 2, "failed": 1, "skipped": 0}
    from backend.app import main

    monkeypatch.setattr(main, "REFRESH_BUDGET", 0)  # hết ngân sách thời gian → bỏ qua hết
    assert client.post("/api/refresh-all").json() == {"total": 3, "done": 0, "failed": 0, "skipped": 3}


def test_login_rate_limit(client):
    for _ in range(5):
        assert login(client, "x").status_code == 401
    assert login(client, "x").status_code == 429
    assert login(client).status_code == 429  # đúng mật khẩu vẫn bị chặn trong lúc khóa


def test_session_cookie_signature(client, monkeypatch):
    from backend.app import auth

    c = auth.make_session(60)
    assert auth.verify_session(c)
    exp, sig = c.split(".")
    assert not auth.verify_session(f"{int(exp) + 999}.{sig}")  # sửa hạn dùng
    assert not auth.verify_session(f"{exp}.{'0' * 64}")
    assert not auth.verify_session(None) and not auth.verify_session("abc")
    assert not auth.verify_session(auth.make_session(-1))  # hết hạn
    monkeypatch.setenv("ADMIN_PASSWORD", "doi-mat-khau")  # đổi mật khẩu → mọi phiên cũ vô hiệu
    assert not auth.verify_session(c)


def test_session_survives_without_server_memory(client):
    """Phiên là cookie có chữ ký: một 'tiến trình mới' vẫn chấp nhận."""
    login(client)
    cookie = client.cookies.get("kam_session")
    from backend.app import main

    with TestClient(main.app) as fresh:
        fresh.cookies.set("kam_session", cookie)
        assert fresh.get("/api/accounts").status_code == 200


def test_parse_quota_variants():
    from backend.app.kaggle_client import KaggleError, parse_quota

    noisy = 'Authentication required\n[not json]\n[{"resource":"GPU","used":"1.50h","remaining":"28.50h","total":"30.00h","refreshAt":"2026-10-24T00:00:00Z"}]\ntrailer'
    q = parse_quota(noisy)
    assert (q.gpu_used, q.gpu_total, q.tpu_used) == (1.5, 30.0, None)
    assert q.refresh_at.year == 2026
    for bad in ("nothing useful", "No quota information available", "[]", '[{"resource":"CPU"}]'):
        with pytest.raises(KaggleError):
            parse_quota(bad)


def test_requirements_match_pyproject():
    import tomllib

    root = Path(__file__).resolve().parents[2]
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    reqs = [l.strip() for l in (root / "requirements.txt").read_text().splitlines() if l.strip()]
    assert sorted(deps) == sorted(reqs)
    assert tomllib.loads((root / "pyproject.toml").read_text())["tool"]["vercel"]["entrypoint"] == "backend.app.main:app"
