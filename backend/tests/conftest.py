"""Fixture dùng chung: Postgres thật (pixeltable-pgserver) + Data API giả lập + `kaggle` giả."""
import json
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

from .fake_postgrest import FakePostgREST
from .helpers import API_KEY, FAKE_KAGGLE, FAKE_PROBE, reset_database


@pytest.fixture(scope="session")
def pg_url():
    import pixeltable_pgserver as pg

    srv = pg.get_server(tempfile.mkdtemp(prefix="pgdata_"))
    yield srv.get_uri()
    srv.cleanup()


@pytest.fixture()
def api(pg_url):
    return FakePostgREST(pg_url, API_KEY)


@pytest.fixture()
def client(pg_url, api, tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from backend.app import db

    fake = tmp_path / "fake_kaggle.py"
    fake.write_text(FAKE_KAGGLE)
    probe = tmp_path / "fake_probe.py"
    probe.write_text(FAKE_PROBE)
    monkeypatch.setenv("KAGGLE_CMD", f"{sys.executable} {fake}")
    monkeypatch.setenv("KAGGLE_PROBE_CMD", f"{sys.executable} {probe}")
    monkeypatch.setenv("FAKE_PROBE_LOG", str(tmp_path / "probe.log"))
    monkeypatch.setenv("FAKE_PROBE_SCENARIO", str(tmp_path / "scenario.json"))
    (tmp_path / "scenario.json").write_text(json.dumps({}))
    monkeypatch.setenv("SUPABASE_URL", "https://abcd.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", API_KEY)
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_PASSWORD", "pw123")
    reset_database(pg_url)
    monkeypatch.setattr(db, "_transport", api.transport())
    db.reset_client()
    from backend.app import main

    with TestClient(main.app) as c:
        yield c
    db.reset_client()
