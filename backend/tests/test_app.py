"""Kiểm thử app → Supabase Data API (giả lập trên Postgres thật) + `kaggle` giả.

Fixture dùng chung nằm trong conftest.py. Data API là bản giả lập tối thiểu (fake_postgrest.py),
không phải PostgREST thật; supabase/schema.sql được chạy nguyên văn và truy vấn chạy bằng role
service_role thật nên các lệnh GRANT/REVOKE/RLS được kiểm thử.
"""
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from .fake_postgrest import FakePostgREST
from .helpers import API_KEY, ROOT, SCHEMA_SQL, add, login, reset_database

# ---------- schema & bảo mật ----------
def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_public_roles_cannot_read_tables_but_service_role_can(client, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("SET ROLE service_role")
        c.execute("SELECT * FROM accounts").fetchall()
        for role in ("anon", "authenticated"):
            c.execute("RESET ROLE")
            c.execute(f"SET ROLE {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT * FROM accounts")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT * FROM login_fails")
    with psycopg.connect(pg_url) as c:
        rls = c.execute(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN ('accounts','quota_snapshots','login_fails','kernel_sessions')"
        ).fetchall()
    assert len(rls) == 4 and all(r[1] for r in rls), "RLS phải bật trên cả 4 bảng"


def test_schema_is_idempotent(pg_url, client):
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(SCHEMA_SQL)  # chạy lại lần 2 không lỗi


def test_wrong_key_gives_clear_error(client, monkeypatch):
    from backend.app import db

    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_wrong")
    db.reset_client()
    r = client.get("/api/health")
    assert r.status_code == 503 and "khóa" in r.json()["detail"]


def test_missing_tables_hint(client, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("DROP TABLE accounts CASCADE")
    r = client.get("/api/health")
    assert r.status_code == 503 and "schema.sql" in r.json()["detail"]


def test_missing_grant_hint(client, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("REVOKE ALL ON accounts FROM service_role")
    r = client.get("/api/health")
    assert r.status_code == 503 and "GRANT" in r.json()["detail"]


def test_key_goes_in_apikey_header_only(client, api):
    client.get("/api/health")
    h = api.requests[-1].headers
    assert h["apikey"] == API_KEY and "authorization" not in h  # khóa sb_secret_ không phải JWT


def test_legacy_jwt_key_also_sends_bearer(pg_url, monkeypatch):
    from backend.app import db

    legacy = "eyJhbGciOi.legacy.jwt"
    fake = FakePostgREST(pg_url, legacy)
    monkeypatch.setenv("SUPABASE_URL", "https://abcd.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", legacy)
    monkeypatch.setattr(db, "_transport", fake.transport())
    reset_database(pg_url)
    db.reset_client()
    db.select("accounts", "id", limit=1)
    h = fake.requests[-1].headers
    assert h["apikey"] == legacy and h["authorization"] == f"Bearer {legacy}"
    db.reset_client()


# ---------- đăng nhập ----------
def test_auth_required(client):
    assert client.get("/api/accounts").status_code == 401
    assert login(client, "nope").status_code == 401
    assert client.get("/api/accounts").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/accounts").status_code == 200


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
    login(client)
    cookie = client.cookies.get("kam_session")
    from backend.app import main

    with TestClient(main.app) as fresh:
        fresh.cookies.set("kam_session", cookie)
        assert fresh.get("/api/accounts").status_code == 200


# ---------- tài khoản & quota ----------
def test_add_and_quota(client):
    login(client)
    r = add(client, "acc1")
    assert r.status_code == 201 and r.json()["check"]["ok"], r.text
    accs = client.get("/api/accounts").json()
    q = accs[0]["quota"]
    assert q["gpu_used"] == 16.33 and q["gpu_total"] == 30.0
    assert q["tpu_used"] == 0.0 and q["tpu_total"] == 20.0
    assert q["refresh_at"].startswith("2026-10-24")
    assert accs[0]["last_checked"] and accs[0]["last_error"] is None
    assert not any("token" in k for k in accs[0])


def test_list_never_selects_token_column(client, api):
    login(client)
    add(client, "acc1")
    api.requests.clear()
    client.get("/api/accounts")
    gets = [r for r in api.requests if r.method == "GET" and r.url.path.endswith("/accounts")]
    assert gets and all("token_enc" not in r.url.params["select"] for r in gets)


def test_token_encrypted_at_rest(client, pg_url):
    login(client)
    add(client, "a", "super-secret-token")
    with psycopg.connect(pg_url) as c:
        raw = c.execute("SELECT token_enc FROM accounts").fetchone()[0]
    assert "super-secret-token" not in raw


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
    assert client.delete(f"/api/accounts/{aid}").status_code == 404


def test_update_name_conflict_and_token_recheck(client):
    login(client)
    add(client, "a", "bad-token-000"); add(client, "b")
    ids = {a["name"]: a["id"] for a in client.get("/api/accounts").json()}
    assert client.patch(f"/api/accounts/{ids['a']}", json={"name": "b"}).status_code == 409
    assert client.patch("/api/accounts/99999", json={"note": "x"}).status_code == 404
    r = client.patch(f"/api/accounts/{ids['a']}", json={"token": "good-token-111"})
    assert r.json()["check"]["ok"]
    assert client.get("/api/accounts").json()[0]["last_error"] is None


def test_accounts_sorted_case_insensitively(client):
    login(client)
    for n in ("banana", "Apple", "cherry"):
        add(client, n)
    assert [a["name"] for a in client.get("/api/accounts").json()] == ["Apple", "banana", "cherry"]


def test_refresh_all_summary_and_budget(client, monkeypatch):
    login(client)
    add(client, "a"); add(client, "b"); add(client, "c", "bad-token-000")
    assert client.post("/api/refresh-all").json() == {"total": 3, "done": 2, "failed": 1, "skipped": 0}
    from backend.app import main

    monkeypatch.setattr(main, "REFRESH_BUDGET", 0)  # hết ngân sách thời gian → bỏ qua hết
    assert client.post("/api/refresh-all").json() == {"total": 3, "done": 0, "failed": 0, "skipped": 3}


def test_history_grows_and_old_snapshots_pruned(client, pg_url):
    login(client)
    add(client, "a")
    aid = client.get("/api/accounts").json()[0]["id"]
    with psycopg.connect(pg_url, autocommit=True) as c:  # chèn 1 snapshot cũ 100 ngày
        c.execute("INSERT INTO quota_snapshots(account_id, ts) VALUES (%s, now() - interval '100 days')", (aid,))
    client.post(f"/api/accounts/{aid}/refresh")
    h = client.get(f"/api/accounts/{aid}/history").json()
    assert len(h) == 2  # 2 lần làm mới thật; snapshot 100 ngày tuổi đã bị dọn


# ---------- bộ đọc quota & cấu hình ----------
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

    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    reqs = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip()]
    assert sorted(py["project"]["dependencies"]) == sorted(reqs)
    assert py["tool"]["vercel"]["entrypoint"] == "backend.app.main:app"
