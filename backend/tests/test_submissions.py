"""Kiểm thử tab Submissions: thu thập (kaggle_probe.collect_submissions) và API."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import psycopg
import pytest

from backend.app import kaggle_probe, main

from .helpers import add, login
from .test_sessions import set_scenario

NOW = datetime.now(timezone.utc)


def comp(slug="titanic", days=10, **kw):
    d = dict(ref=f"https://www.kaggle.com/competitions/{slug}", title=slug.title(), url=f"https://www.kaggle.com/competitions/{slug}",
             category="Featured", reward="$10,000", team_count=500, user_rank=42, max_daily_submissions=5,
             evaluation_metric="AUC", submissions_disabled=False, deadline=NOW + timedelta(days=days))
    d.update(kw)
    return NS(**d)


def sb(ref=1, status="COMPLETE", score="0.91", **kw):
    d = dict(ref=ref, date=NOW - timedelta(hours=ref), description=f"sub {ref}", file_name="s.csv",
             status=NS(name=status, value=0), public_score=score, private_score="", error_description="",
             team_name="t", submitted_by="me", url="https://x/s", total_bytes=10)
    d.update(kw)
    return NS(**d)


class Api:
    def __init__(self, comps, subs, fail=()):
        self.comps, self.subs, self.fail = comps, subs, set(fail)
        self.list_kw, self.sub_calls = None, []

    def competitions_list(self, **kw):
        self.list_kw = kw
        return NS(competitions=self.comps)

    def competition_submissions(self, slug, **kw):
        self.sub_calls.append((slug, kw))
        if slug in self.fail:
            raise RuntimeError("403 Forbidden")
        return self.subs.get(slug, [])


def scen(items):
    return {"competitions": [], "submissions": [], "listed": 0, "partial": False, "errors": {}, **items}


def api_comp(slug="titanic", **kw):
    return {"slug": slug, "title": slug.title(), "deadline": (NOW + timedelta(days=5)).isoformat(), "category": "Featured",
            "reward": "$1", "team_count": 9, "user_rank": 3, "max_daily_submissions": 5, "evaluation_metric": "AUC",
            "url": "u", "active": True, "submissions_disabled": False, "submissions_fetched": True, **kw}


def api_sub(ref=1, comp_="titanic", status="complete", score="0.9", date=None, **kw):
    return {"competition": comp_, "ref": ref, "date": (date or NOW).isoformat(), "description": f"d{ref}", "file_name": "f.csv",
            "status": status, "public_score": score, "private_score": None, "error_description": None, "team_name": "t",
            "submitted_by": "me", "url": "u", "total_bytes": 5, **kw}


def refresh(client):
    r = client.post("/api/submissions/refresh")
    assert r.status_code == 200, r.text
    return r.json()


# ---------- API ----------
def test_refresh_and_list(client, tmp_path):
    login(client); add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({
        "competitions": [api_comp("titanic"), api_comp("house", submissions_fetched=False)],
        "submissions": [api_sub(1), api_sub(2, status="pending", score=None), api_sub(3, status="error", error_description="bad cols")],
    }))
    r = refresh(client)
    assert r == {"total": 1, "done": 1, "skipped": 0, "failed": 0, "competitions": 2, "submissions": 3, "pending": 1, "partial": False}
    d = client.get("/api/submissions").json()
    assert {c["slug"] for c in d["competitions"]} == {"titanic", "house"}
    assert {s["ref"]: s["status"] for s in d["submissions"]} == {1: "complete", 2: "pending", 3: "error"}
    assert d["accounts"][0]["subs_checked_at"] and d["accounts"][0]["subs_error"] is None
    assert "token" not in json.dumps(d)
    probe_opts = json.loads((tmp_path / "probe.log").read_text().splitlines()[-1])["opts"]
    assert probe_opts["mode"] == "submissions" and probe_opts["only_active"] is True


def test_resubmit_updates_status_and_keeps_first_seen(client, tmp_path):
    login(client); add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp()], "submissions": [api_sub(7, status="pending", score=None)]}))
    refresh(client)
    a = client.get("/api/submissions").json()["submissions"][0]
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp()], "submissions": [api_sub(7, score="0.88")]}))
    refresh(client)
    subs = client.get("/api/submissions").json()["submissions"]
    assert len(subs) == 1 and subs[0]["status"] == "complete" and subs[0]["public_score"] == "0.88"
    assert subs[0]["first_seen_at"] == a["first_seen_at"]


def test_competition_that_ended_is_marked_inactive(client, tmp_path):
    login(client); add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp("a"), api_comp("b")]}))
    refresh(client)
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp("a")]}))
    refresh(client)
    act = {c["slug"]: c["active"] for c in client.get("/api/submissions").json()["competitions"]}
    assert act == {"a": True, "b": False}
    # quét dở (partial) thì không kết luận gì về competition vắng mặt
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp("c")], "partial": True}))
    refresh(client)
    act = {c["slug"]: c["active"] for c in client.get("/api/submissions").json()["competitions"]}
    assert act["a"] is True and act["c"] is True


def test_failure_isolated_per_account_and_shown(client, tmp_path):
    login(client); add(client, "good"); add(client, "bad", "token-bbbbbbbb")
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp()], "submissions": [api_sub()]}))
    set_scenario(tmp_path, "token-bbbbbbbb:submissions", {"error": "Xác thực Kaggle thất bại (token sai hoặc hết hạn)"})
    r = refresh(client)
    assert (r["done"], r["failed"]) == (1, 1)
    accs = {a["name"]: a for a in client.get("/api/submissions").json()["accounts"]}
    assert accs["bad"]["subs_error"].startswith("Xác thực") and accs["good"]["subs_error"] is None


def test_partial_when_some_competition_errors(client, tmp_path):
    login(client); add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp()], "errors": {"x": "403"}}))
    assert refresh(client)["partial"] is True


def test_budget_and_auth(client, monkeypatch):
    assert client.get("/api/submissions").status_code == 401
    assert client.post("/api/submissions/refresh").status_code == 401
    login(client); add(client, "a")
    monkeypatch.setattr(main, "REFRESH_BUDGET", 0)
    assert client.post("/api/submissions/refresh").json()["skipped"] == 1


def test_database_error_shown_on_account(client, tmp_path, pg_url):
    login(client); add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp()], "submissions": [api_sub()]}))
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("ALTER TABLE submissions DROP COLUMN public_score")
    assert refresh(client)["failed"] == 1
    err = client.get("/api/submissions").json()["accounts"][0]["subs_error"]
    assert err and "schema.sql" in err


def test_missing_tables_do_not_break_accounts_page(client, pg_url):
    login(client); add(client, "acc1")
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("DROP TABLE submissions, competition_entries")
    assert client.get("/api/accounts").status_code == 200
    r = client.get("/api/submissions")
    assert r.status_code == 503 and "schema.sql" in r.json()["detail"]


def test_deleting_account_cascades(client, tmp_path):
    login(client); add(client, "acc1")
    set_scenario(tmp_path, "token-aaaaaaaa:submissions", scen({"competitions": [api_comp()], "submissions": [api_sub()]}))
    refresh(client)
    client.delete(f"/api/accounts/{client.get('/api/accounts').json()[0]['id']}")
    d = client.get("/api/submissions").json()
    assert d["submissions"] == [] and d["competitions"] == []


def test_same_competition_two_accounts_kept_separate(client, tmp_path):
    login(client); add(client, "a"); add(client, "b", "token-bbbbbbbb")
    for t in ("token-aaaaaaaa", "token-bbbbbbbb"):
        set_scenario(tmp_path, t + ":submissions", scen({"competitions": [api_comp()], "submissions": [api_sub(1)]}))
    refresh(client)
    d = client.get("/api/submissions").json()
    assert len(d["competitions"]) == 2 and len(d["submissions"]) == 2  # cùng ref=1 nhưng khác tài khoản


def test_public_roles_cannot_read(client, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as c:
        for role in ("anon", "authenticated"):
            c.execute("RESET ROLE"); c.execute(f"SET ROLE {role}")
            for t in ("submissions", "competition_entries"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"SELECT * FROM {t}")


# ---------- collect_submissions ----------
def test_collect_filters_to_active_and_maps_fields():
    api = Api([comp("a", days=5), comp("old", days=-3), comp("nodl", deadline=None)],
              {"a": [sb(1), sb(2, "PENDING", None), sb(3, "ERROR", None, error_description="x")]})
    out = kaggle_probe.collect_submissions(api, {})
    assert api.list_kw["group"] == "entered" and api.list_kw["category"] == "all"
    assert [c["slug"] for c in out["competitions"]] == ["a", "nodl"]
    assert [s["status"] for s in out["submissions"]] == ["complete", "pending", "error"]
    s = out["submissions"][0]
    assert s["public_score"] == "0.91" and s["private_score"] is None and s["competition"] == "a"
    assert out["competitions"][0]["max_daily_submissions"] == 5 and out["competitions"][0]["active"] is True
    assert [c[0] for c in api.sub_calls] == ["a", "nodl"] and api.sub_calls[0][1]["page_size"] == 50


def test_collect_includes_ended_when_asked():
    api = Api([comp("a", days=5), comp("old", days=-3)], {})
    out = kaggle_probe.collect_submissions(api, {"only_active": False})
    assert {c["slug"]: c["active"] for c in out["competitions"]} == {"a": True, "old": False}


def test_collect_isolates_failing_competition_and_deadline():
    api = Api([comp("a"), comp("b"), comp("c")], {"a": [sb(1)], "c": [sb(2)]}, fail={"b"})
    out = kaggle_probe.collect_submissions(api, {})
    assert out["errors"] == {"b": "403 Forbidden"} and len(out["submissions"]) == 2
    assert {c["slug"]: c["submissions_fetched"] for c in out["competitions"]} == {"a": True, "b": False, "c": True}
    ticks = iter(range(0, 1000, 10))
    api2 = Api([comp(s) for s in "abcde"], {})
    out = kaggle_probe.collect_submissions(api2, {"deadline_seconds": 25}, clock=lambda: next(ticks))
    assert out["partial"] is True and len(api2.sub_calls) < 5


def test_probe_main_dispatches_on_mode(monkeypatch, capsys):
    import importlib
    ext = importlib.import_module("kaggle.api.kaggle_api_extended")
    api = Api([comp("a")], {"a": [sb(1)]})
    api.authenticate = lambda: None
    monkeypatch.setattr(ext, "KaggleApi", lambda: api)
    kaggle_probe.main(["probe", json.dumps({"mode": "submissions"})])
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith(kaggle_probe.SENTINEL)][-1]
    out = json.loads(line[len(kaggle_probe.SENTINEL):])
    assert out["submissions"][0]["ref"] == 1 and "items" not in out
