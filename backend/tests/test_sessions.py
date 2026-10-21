"""Kiểm thử trang Sessions: thu thập (kaggle_probe), gộp dữ liệu (sessions.merge) và API."""
import json
import sys
from types import SimpleNamespace as NS

import psycopg
import pytest

from backend.app import kaggle_probe, sessions

from .helpers import add, login


# ---------- dữ liệu giả ----------
def item(ref="me/nb1", version=1, status="complete", **kw):
    base = {
        "ref": ref, "version": version, "title": "Notebook 1", "author": ref.split("/")[0],
        "last_run_time": "2026-10-21T01:00:00+00:00", "accelerator": "cpu", "machine_shape": "", "accelerator_checked": True,
        "docker_image": "", "enable_internet": False, "is_private": True, "kernel_type": "notebook",
        "language": "python", "total_votes": 0, "competitions": [], "datasets": [], "models": [],
        "kernel_sources": [], "status": status, "failure_message": None, "duration_seconds": None,
    }
    base.update(kw)
    return base


def set_scenario(tmp_path, token, result):
    p = tmp_path / "scenario.json"
    cur = json.loads(p.read_text())
    cur[token] = result
    p.write_text(json.dumps(cur))


def refresh(client):
    r = client.post("/api/sessions/refresh")
    assert r.status_code == 200, r.text
    return r.json()


def rows(client):
    return client.get("/api/sessions").json()["sessions"]


# ---------- API ----------
def test_refresh_and_list(client, tmp_path):
    login(client)
    add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa", {"listed": 2, "partial": False, "items": [
        item("me/old", 3, "complete", last_run_time="2026-10-20T01:00:00+00:00", duration_seconds=3600.0),
        item("me/train", 7, "running", accelerator="gpu", machine_shape="NvidiaTeslaT4",
             competitions=["titanic"], datasets=["me/data"], last_run_time="2026-10-21T02:00:00+00:00"),
    ]})
    r = refresh(client)
    assert r == {"total": 1, "done": 1, "skipped": 0, "failed": 0, "running": 1, "partial": False}
    data = client.get("/api/sessions").json()
    s = data["sessions"]
    assert [x["ref"] for x in s] == ["me/train", "me/old"]  # mới chạy nhất trước
    assert s[0]["status"] == "running" and s[0]["accelerator"] == "gpu" and s[0]["competitions"] == ["titanic"]
    assert s[0]["datasets"] == ["me/data"] and s[1]["duration_seconds"] == 3600.0
    assert data["accounts"][0]["sessions_checked_at"] and data["accounts"][0]["sessions_error"] is None
    assert data["accounts"][0]["gpu_total"] == 30.0  # kèm quota để hiển thị trên trang Sessions
    assert "token_enc" not in json.dumps(data)


def test_accounts_list_has_running_count(client, tmp_path):
    login(client)
    add(client, "acc1")
    assert client.get("/api/accounts").json()[0]["running_sessions"] == 0
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item("me/a", 1, "running"), item("me/b", 1, "queued")]})
    refresh(client)
    assert client.get("/api/accounts").json()[0]["running_sessions"] == 1


def test_refresh_preserves_first_seen_and_tracks_status_change(client, tmp_path):
    login(client)
    add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item(status="running")]})
    refresh(client)
    a = rows(client)[0]
    refresh(client)  # cùng dữ liệu: không đổi trạng thái
    b = rows(client)[0]
    assert b["first_seen_at"] == a["first_seen_at"] and b["status_changed_at"] == a["status_changed_at"]
    assert b["updated_at"] >= a["updated_at"]
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item(status="complete", duration_seconds=5400.0)]})
    refresh(client)
    c = rows(client)[0]
    assert c["status"] == "complete" and c["duration_seconds"] == 5400.0
    assert c["first_seen_at"] == a["first_seen_at"] and c["status_changed_at"] > a["status_changed_at"]
    assert len(rows(client)) == 1  # upsert, không nhân bản


def test_new_version_marks_old_active_run_unknown(client, tmp_path):
    login(client)
    add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item(version=1, status="running")]})
    refresh(client)
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item(version=2, status="queued",
                                                            last_run_time="2026-10-21T03:00:00+00:00")]})
    refresh(client)
    by_v = {r["version"]: r["status"] for r in rows(client)}
    assert by_v == {1: "unknown", 2: "queued"}


def test_known_terminal_rows_are_sent_to_probe(client, tmp_path):
    login(client)
    add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item(status="complete", duration_seconds=120.0)]})
    refresh(client)
    refresh(client)
    calls = [json.loads(l) for l in (tmp_path / "probe.log").read_text().splitlines()]
    assert calls[0]["opts"]["known"] == {}
    known = calls[1]["opts"]["known"]["me/nb1"]
    assert known["status"] == "complete" and known["duration_seconds"] == 120.0 and known["version"] == 1
    assert calls[1]["token"] == "token-aaaaaaaa"  # token đi qua biến môi trường của tiến trình con


def test_probe_failure_is_recorded_per_account(client, tmp_path):
    login(client)
    add(client, "good"); add(client, "broken", "token-bbbbbbbb")
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item()]})
    set_scenario(tmp_path, "token-bbbbbbbb", {"error": "Xác thực Kaggle thất bại (token sai hoặc hết hạn)"})
    r = refresh(client)
    assert (r["done"], r["failed"]) == (1, 1)
    accs = {a["name"]: a for a in client.get("/api/sessions").json()["accounts"]}
    assert accs["broken"]["sessions_error"].startswith("Xác thực Kaggle")
    assert accs["good"]["sessions_error"] is None and len(rows(client)) == 1


def test_probe_crash_is_reported(client, tmp_path):
    login(client)
    add(client, "acc1", "token-zzzzzzzz")  # không có kịch bản: script giả thoát với lỗi
    r = refresh(client)
    assert r["failed"] == 1
    assert "không có kịch bản" in client.get("/api/sessions").json()["accounts"][0]["sessions_error"]


def test_refresh_respects_time_budget(client, tmp_path, monkeypatch):
    login(client)
    add(client, "a"); add(client, "b", "token-bbbbbbbb")
    from backend.app import main

    monkeypatch.setattr(main, "REFRESH_BUDGET", 0)
    assert client.post("/api/sessions/refresh").json()["skipped"] == 2


def test_oldest_checked_account_goes_first(client, tmp_path):
    login(client)
    add(client, "a"); add(client, "b", "token-bbbbbbbb")
    for t in ("token-aaaaaaaa", "token-bbbbbbbb"):
        set_scenario(tmp_path, t, {"items": [item()]})
    refresh(client)
    (tmp_path / "probe.log").write_text("")
    refresh(client)
    first_run = [json.loads(l)["token"] for l in (tmp_path / "probe.log").read_text().splitlines()]
    assert sorted(first_run) == ["token-aaaaaaaa", "token-bbbbbbbb"]  # cả hai đều được hỏi lại


def test_sessions_page_needs_login(client):
    assert client.get("/api/sessions").status_code == 401
    assert client.post("/api/sessions/refresh").status_code == 401


def test_deleting_account_removes_its_sessions(client, tmp_path):
    login(client)
    add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item()]})
    refresh(client)
    aid = client.get("/api/accounts").json()[0]["id"]
    client.delete(f"/api/accounts/{aid}")
    assert rows(client) == []


def test_old_sessions_pruned(client, tmp_path, pg_url):
    login(client)
    add(client, "acc1")
    aid = client.get("/api/accounts").json()[0]["id"]
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(
            "INSERT INTO kernel_sessions(account_id, ref, version, updated_at) VALUES (%s,'me/old',1, now() - interval '100 days')",
            (aid,),
        )
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item()]})
    refresh(client)
    assert [r["ref"] for r in rows(client)] == ["me/nb1"]


# ---------- schema chưa được cập nhật: trang Tài khoản vẫn phải dùng được ----------
def test_accounts_page_survives_missing_sessions_table(client, pg_url):
    login(client)
    add(client, "acc1")
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("DROP TABLE kernel_sessions")
    r = client.get("/api/accounts")
    assert r.status_code == 200 and r.json()[0]["running_sessions"] == 0
    r = client.get("/api/sessions")
    assert r.status_code == 503 and "schema.sql" in r.json()["detail"]


def test_missing_columns_hint(client, pg_url):
    login(client)
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("ALTER TABLE accounts DROP COLUMN sessions_checked_at")
    r = client.get("/api/sessions")
    assert r.status_code == 503 and "cột" in r.json()["detail"] and "schema.sql" in r.json()["detail"]
    assert client.get("/api/accounts").status_code == 200  # trang Tài khoản không bị ảnh hưởng


def test_public_roles_cannot_read_sessions(client, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as c:
        for role in ("anon", "authenticated"):
            c.execute("RESET ROLE")
            c.execute(f"SET ROLE {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT * FROM kernel_sessions")


# ---------- sessions.merge (logic thuần) ----------
def test_merge_rows_always_have_identical_keys():
    now = "2026-10-21T04:00:00+00:00"
    known = [{**{k: None for k in sessions.ROW_KEYS}, "id": 9, "account_id": 1, "ref": "me/a", "version": 1,
              "status": "running", "first_seen_at": "2026-10-20T00:00:00+00:00"}]
    out = sessions.merge(1, [item("me/a", 2, "queued"), item("me/b", 1, "error", failure_message="OOM")], known, now)
    assert len(out) == 3 and all(set(r) == set(sessions.ROW_KEYS) for r in out)
    stale = next(r for r in out if r["ref"] == "me/a" and r["version"] == 1)
    assert stale["status"] == "unknown" and stale["first_seen_at"] == "2026-10-20T00:00:00+00:00"
    assert next(r for r in out if r["ref"] == "me/b")["failure_message"] == "OOM"


def test_merge_keeps_old_status_when_status_not_fetched():
    now = "2026-10-21T04:00:00+00:00"
    prev = {**{k: None for k in sessions.ROW_KEYS}, "account_id": 1, "ref": "me/a", "version": 1,
            "status": "running", "status_changed_at": "2026-10-21T01:00:00+00:00",
            "first_seen_at": "2026-10-21T01:00:00+00:00", "last_run_time": "2026-10-21T01:00:00+00:00"}
    out = sessions.merge(1, [item("me/a", 1, None)], [prev], now)  # vượt status_limit/hết giờ: status=None
    assert out[0]["status"] == "running" and out[0]["status_changed_at"] == "2026-10-21T01:00:00+00:00"
    brand_new = sessions.merge(1, [item("me/z", 1, None)], [], now)
    assert brand_new[0]["status"] == "unknown"


# ---------- kaggle_probe.collect (SDK giả) ----------
def meta(ref="me/nb", version=1, gpu=False, tpu=False, shape="", lrt=None, **kw):
    base = dict(
        ref=ref, slug=ref.split("/")[-1], author=ref.split("/")[0], title="T " + ref, current_version_number=version,
        enable_gpu=gpu, enable_tpu=tpu, machine_shape=shape, docker_image="img", enable_internet=True,
        is_private=False, kernel_type="notebook", language="python", total_votes=3,
        last_run_time=lrt, competition_data_sources=["titanic"], dataset_data_sources=["me/d"],
        model_data_sources=[], kernel_data_sources=[],
    )
    base.update(kw)
    return NS(**base)


class FakeApi:
    def __init__(self, kernels, statuses=None, logs=None):
        self.kernels, self.statuses, self.logs = kernels, statuses or {}, logs or {}
        self.status_calls, self.log_calls = [], []

    def kernels_list(self, **kw):
        self.list_kw = kw
        return self.kernels

    def kernels_status(self, ref):
        self.status_calls.append(ref)
        v = self.statuses[ref]
        if isinstance(v, Exception):
            raise v
        return NS(status=NS(name=v[0]), failure_message=v[1] if len(v) > 1 else "")

    def kernels_logs(self, ref):
        self.log_calls.append(ref)
        v = self.logs.get(ref, "")
        if isinstance(v, Exception):
            raise v
        return v

    def get_config_value(self, k):
        return "me"


def test_collect_maps_metadata_and_accelerators():
    from datetime import datetime, timezone

    t = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)
    api = FakeApi(
        [meta("me/a", gpu=True, shape="NvidiaTeslaT4", lrt=t), meta("me/b", tpu=True), meta("me/c"),
         meta("me/d", shape="NvidiaTeslaP100")],
        {r: ("RUNNING",) for r in ("me/a", "me/b", "me/c", "me/d")},
    )
    out = kaggle_probe.collect(api, {"limit": 500}, meta_fn=lambda a, r, v: meta(r))  # xác minh: me/c đúng là CPU
    assert api.list_kw["page_size"] == 100 and api.list_kw["mine"] and api.list_kw["sort_by"] == "dateRun"
    acc = {i["ref"]: i["accelerator"] for i in out["items"]}
    assert acc == {"me/a": "gpu", "me/b": "tpu", "me/c": "cpu", "me/d": "gpu"}  # d: suy ra GPU từ machine_shape
    a = out["items"][0]
    assert a["status"] == "running" and a["last_run_time"] == t.isoformat() and a["competitions"] == ["titanic"]
    assert out["user"] == "me" and out["partial"] is False


def test_collect_ref_fallback_when_ref_has_no_owner():
    api = FakeApi([meta("justslug", author="bob", slug="justslug")], {"bob/justslug": ("COMPLETE",)})
    assert kaggle_probe.collect(api, {})["items"][0]["ref"] == "bob/justslug"


def test_collect_skips_status_for_known_finished_run():
    from datetime import datetime, timezone

    t = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)
    api = FakeApi([meta("me/a", version=4, lrt=t), meta("me/b", version=2, lrt=t)], {"me/b": ("RUNNING",)})
    known = {
        "me/a": {"version": 4, "last_run_time": "2026-10-21T01:00:00Z", "status": "complete", "duration_seconds": 99.0},
        "me/b": {"version": 2, "last_run_time": "2026-10-21T01:00:00+00:00", "status": "running"},  # đang chạy: phải hỏi lại
    }
    out = kaggle_probe.collect(api, {"known": known})
    assert api.status_calls == ["me/b"]
    a = out["items"][0]
    assert a["status"] == "complete" and a["duration_seconds"] == 99.0


def test_collect_rechecks_when_a_new_run_exists():
    api = FakeApi([meta("me/a", version=5)], {"me/a": ("QUEUED",)})
    known = {"me/a": {"version": 4, "last_run_time": None, "status": "complete"}}
    out = kaggle_probe.collect(api, {"known": known})
    assert api.status_calls == ["me/a"] and out["items"][0]["status"] == "queued"


def test_collect_isolates_one_failing_kernel_and_honors_status_limit():
    api = FakeApi(
        [meta("me/a"), meta("me/b"), meta("me/c")],
        {"me/a": RuntimeError("403 Forbidden"), "me/b": ("ERROR", "OOM killed")},
    )
    out = kaggle_probe.collect(api, {"status_limit": 2})
    a, b, c = out["items"]
    assert a["status"] is None and "403" in a["status_error"]
    assert b["status"] == "error" and b["failure_message"] == "OOM killed"
    assert c["status"] is None and api.status_calls == ["me/a", "me/b"]  # me/c vượt status_limit


def test_collect_duration_from_logs_with_budget():
    logs = {
        "me/a": json.dumps([{"time": 1.5, "data": "x"}, {"time": 3725.2, "data": "done"}, {"time": 10, "data": "y"}]),
        "me/b": "không phải JSON",
        "me/c": json.dumps([{"time": 50.0}]),
    }
    api = FakeApi(
        [meta("me/a"), meta("me/b"), meta("me/c")],
        {k: ("COMPLETE",) for k in ("me/a", "me/b", "me/c")}, logs,
    )
    out = kaggle_probe.collect(api, {"duration_budget": 2})
    d = {i["ref"]: i["duration_seconds"] for i in out["items"]}
    assert d == {"me/a": 3725.2, "me/b": -1.0, "me/c": None}  # -1: đã thử nhưng không xác định; c vượt ngân sách
    assert api.log_calls == ["me/a", "me/b"]


def test_collect_log_error_is_not_counted_as_attempt():
    api = FakeApi([meta("me/a"), meta("me/b")], {"me/a": ("COMPLETE",), "me/b": ("COMPLETE",)},
                  {"me/a": RuntimeError("timeout"), "me/b": json.dumps([{"time": 7.0}])})
    out = kaggle_probe.collect(api, {"duration_budget": 1})
    assert out["items"][0]["duration_seconds"] is None and out["items"][1]["duration_seconds"] == 7.0


def test_collect_stops_at_deadline_and_reports_partial():
    ticks = iter(range(0, 1000, 10))
    api = FakeApi([meta(f"me/{i}") for i in range(5)], {f"me/{i}": ("RUNNING",) for i in range(5)})
    out = kaggle_probe.collect(api, {"deadline_seconds": 25}, clock=lambda: next(ticks))
    assert out["partial"] is True and len(api.status_calls) < 5 and len(out["items"]) == 5


def test_parse_duration_variants():
    p = kaggle_probe.parse_duration
    assert p('[{"time": 2}, {"time": 9.5}]') == 9.5
    assert p('{"log": [{"time": 4}]}') == 4.0
    assert p("[]") is None and p("nope") is None and p(None) is None
    assert p('[{"time": true}, {"data": "x"}]') is None


def test_probe_main_reports_auth_failure(monkeypatch, capsys):
    import importlib

    ext = importlib.import_module("kaggle.api.kaggle_api_extended")

    class Bad:
        def authenticate(self):
            raise SystemExit(1)  # đúng hành vi của KaggleApi.authenticate khi sai token

    monkeypatch.setattr(ext, "KaggleApi", Bad)
    kaggle_probe.main(["probe", "{}"])
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith(kaggle_probe.SENTINEL)][-1]
    assert "Xác thực Kaggle thất bại" in json.loads(line[len(kaggle_probe.SENTINEL):])["error"]


def test_probe_main_success_prints_single_sentinel_line(monkeypatch, capsys):
    import importlib

    ext = importlib.import_module("kaggle.api.kaggle_api_extended")

    api = FakeApi([meta("me/a")], {"me/a": ("RUNNING",)})
    api.authenticate = lambda: print("Authentication required... (nhiễu)")
    monkeypatch.setattr(ext, "KaggleApi", lambda: api)
    kaggle_probe.main(["probe", json.dumps({"limit": 5})])
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith(kaggle_probe.SENTINEL)]
    assert len(lines) == 1 and json.loads(lines[0][len(kaggle_probe.SENTINEL):])["items"][0]["status"] == "running"


def test_fetch_sessions_ignores_noise_and_rejects_garbage(tmp_path, monkeypatch):
    import asyncio

    from backend.app import kaggle_client as kc

    ok = tmp_path / "ok.py"
    ok.write_text('print("noise [1,2]"); print("@@KAM@@" + \'{"items": [], "listed": 0}\'); print("tail")')
    bad = tmp_path / "bad.py"
    bad.write_text('print("không có dòng kết quả")')
    monkeypatch.setenv("KAGGLE_PROBE_CMD", f"{sys.executable} {ok}")
    assert asyncio.run(kc.fetch_sessions("tok-12345678", None, {}, 10))["listed"] == 0
    monkeypatch.setenv("KAGGLE_PROBE_CMD", f"{sys.executable} {bad}")
    with pytest.raises(kc.KaggleError):
        asyncio.run(kc.fetch_sessions("tok-12345678", None, {}, 10))


def test_collect_does_not_trust_cpu_from_list_and_verifies_per_version():
    # Danh sách báo "không GPU" cho me/a nhưng metadata của phiên bản thì có GPU (đúng lỗi người dùng gặp)
    api = FakeApi([meta("me/a", version=3), meta("me/b", version=1)], {"me/a": ("RUNNING",), "me/b": ("COMPLETE",)})
    calls = []

    def meta_fn(_api, ref, version):
        calls.append((ref, version))
        return meta(ref, gpu=ref == "me/a", shape="NvidiaTeslaT4" if ref == "me/a" else "")

    out = kaggle_probe.collect(api, {}, meta_fn=meta_fn)
    a, b = out["items"]
    assert calls == [("me/a", 3), ("me/b", 1)]
    assert (a["accelerator"], a["machine_shape"], a["accelerator_checked"]) == ("gpu", "NvidiaTeslaT4", True)
    assert (b["accelerator"], b["accelerator_checked"]) == ("cpu", True)  # CPU chỉ khi đã xác minh


def test_collect_unverified_is_unknown_not_cpu_and_budgeted():
    api = FakeApi([meta(f"me/{i}") for i in range(3)], {f"me/{i}": ("COMPLETE",) for i in range(3)})

    def boom(_api, ref, version):
        raise RuntimeError("403")

    out = kaggle_probe.collect(api, {}, meta_fn=boom)
    assert [i["accelerator"] for i in out["items"]] == ["unknown"] * 3
    seen = []
    out = kaggle_probe.collect(api, {"accel_budget": 1}, meta_fn=lambda a, r, v: seen.append(r) or meta(r))
    assert seen == ["me/0"] and [i["accelerator"] for i in out["items"]] == ["cpu", "unknown", "unknown"]
    assert out["partial"] is True


def test_collect_reuses_verified_accelerator_for_same_version():
    api = FakeApi([meta("me/a", version=2)], {"me/a": ("COMPLETE",)})
    known = {"me/a": {"version": 2, "status": "running", "accelerator": "gpu", "accelerator_checked": True,
                      "machine_shape": "NvidiaTeslaT4", "last_run_time": None}}
    out = kaggle_probe.collect(api, {"known": known}, meta_fn=lambda *a: pytest.fail("không được hỏi lại"))
    assert out["items"][0]["accelerator"] == "gpu" and out["items"][0]["machine_shape"] == "NvidiaTeslaT4"


def test_database_error_during_store_is_shown_on_account(client, tmp_path, pg_url):
    login(client)
    add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa", {"items": [item()]})
    with psycopg.connect(pg_url, autocommit=True) as c:  # mô phỏng chưa chạy lại schema mới
        c.execute("ALTER TABLE kernel_sessions DROP COLUMN accelerator_checked")
    assert refresh(client)["failed"] == 1
    err = client.get("/api/sessions").json()["accounts"][0]["sessions_error"]
    assert err and "schema.sql" in err
