import os
import stat
import tempfile

import pytest
from fastapi.testclient import TestClient

FAKE = """#!/bin/sh
# kaggle giả để kiểm thử: đọc token từ biến môi trường như CLI thật
if [ "$KAGGLE_API_TOKEN" = "bad-token-000" ]; then echo "401 Unauthorized" >&2; exit 1; fi
case "$1" in
  quota)
    echo "Kaggle quota for $KAGGLE_API_TOKEN"
    echo "GPU 16.33h used, 13.67h remaining"
    echo "TPU 0.00h used, 20.00h remaining"
    ;;
  kernels) echo "ref,title"; echo "me/nb1,Notebook 1" ;;
esac
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    fake = tmp_path / "kaggle"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_PASSWORD", "pw123")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("KAGGLE_BIN", str(fake))
    import importlib

    from backend.app import db, kaggle_client, main

    importlib.reload(db)
    importlib.reload(kaggle_client)
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def login(c):
    assert c.post("/api/login", json={"password": "pw123"}).status_code == 200


def test_auth_required(client):
    assert client.get("/api/accounts").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401


def test_add_and_quota(client):
    login(client)
    r = client.post("/api/accounts", json={"name": "acc1", "token": "token-aaaaaaaa"})
    assert r.status_code == 201 and r.json()["check"]["ok"]
    accs = client.get("/api/accounts").json()
    assert len(accs) == 1
    q = accs[0]["quota"]
    assert q["gpu_used"] == 16.33 and round(q["gpu_total"], 2) == 30.0
    assert q["tpu_used"] == 0.0 and round(q["tpu_total"], 2) == 20.0
    assert "token" not in accs[0] and "token_enc" not in accs[0]


def test_bad_token_reports_error(client):
    login(client)
    r = client.post("/api/accounts", json={"name": "bad", "token": "bad-token-000"})
    assert r.status_code == 201 and not r.json()["check"]["ok"]
    assert client.get("/api/accounts").json()[0]["last_error"]


def test_duplicate_name_and_delete(client):
    login(client)
    client.post("/api/accounts", json={"name": "a", "token": "token-aaaaaaaa"})
    assert client.post("/api/accounts", json={"name": "a", "token": "token-bbbbbbbb"}).status_code == 409
    aid = client.get("/api/accounts").json()[0]["id"]
    assert client.get(f"/api/accounts/{aid}/kernels").json()["csv"].startswith("ref,title")
    assert client.delete(f"/api/accounts/{aid}").status_code == 204
    assert client.get("/api/accounts").json() == []


def test_token_encrypted_at_rest(client):
    login(client)
    client.post("/api/accounts", json={"name": "a", "token": "super-secret-token"})
    import sqlite3

    raw = sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT token_enc FROM accounts").fetchone()[0]
    assert "super-secret-token" not in raw


def test_parser_rejects_garbage():
    from backend.app.kaggle_client import KaggleError, parse_quota

    with pytest.raises(KaggleError):
        parse_quota("nothing useful")
