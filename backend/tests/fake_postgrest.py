"""Giả lập tối thiểu Supabase Data API (PostgREST) trên Postgres thật, chỉ dùng cho kiểm thử.

Hỗ trợ đúng phần ứng dụng dùng: GET/POST/PATCH/DELETE trên 3 bảng, bộ lọc eq/lt/gt/lte/gte,
select/order/limit, Prefer return=representation|minimal và count=exact (Content-Range),
lỗi 409 (23505) cho trùng khóa duy nhất. Mọi truy vấn chạy với `SET ROLE service_role`
nên các lệnh GRANT/REVOKE trong supabase/schema.sql được kiểm thử thật.

Đây KHÔNG phải PostgREST thật: hành vi trên Supabase thật chưa được kiểm chứng bằng bài test này.
"""
import json
from datetime import datetime

import httpx
import psycopg
from psycopg import errors, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

TABLES = {"accounts", "quota_snapshots", "login_fails", "kernel_sessions"}
OPS = {"eq": "=", "lt": "<", "gt": ">", "lte": "<=", "gte": ">="}
RESERVED = {"select", "order", "limit", "offset", "on_conflict"}


def _json(obj, status=200, headers=None):
    body = json.dumps(obj, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))
    return httpx.Response(status, content=body, headers={"content-type": "application/json", **(headers or {})})


class FakePostgREST:
    def __init__(self, pg_url: str, api_key: str):
        self.pg_url, self.api_key = pg_url, api_key
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _where(self, params):
        conds, vals = [], []
        for k, v in params:
            if k in RESERVED:
                continue
            op, _, val = v.partition(".")
            if op not in OPS:
                raise ValueError(f"toán tử không hỗ trợ: {op}")
            conds.append(sql.SQL("{} {} %s").format(sql.Identifier(k), sql.SQL(OPS[op])))
            vals.append(val)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds) if conds else sql.SQL("")
        return where, vals

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.headers.get("apikey") != self.api_key:
            return _json({"message": "Invalid API key"}, 401)
        table = req.url.path.removeprefix("/rest/v1/")
        if table not in TABLES:
            return _json({"code": "PGRST205", "message": f"Could not find the table 'public.{table}' in the schema cache"}, 404)
        params = list(req.url.params.multi_items())
        prefer = req.headers.get("prefer", "")
        try:
            with psycopg.connect(self.pg_url, row_factory=dict_row, autocommit=True) as c:
                c.execute("SET TIME ZONE 'UTC'")
                c.execute("SET ROLE service_role")
                return self._run(c, req, table, params, prefer)
        except errors.UniqueViolation as e:
            return _json({"code": "23505", "message": str(e).splitlines()[0]}, 409)
        except errors.UndefinedTable as e:
            return _json({"code": "42P01", "message": str(e).splitlines()[0]}, 404)
        except errors.UndefinedColumn as e:
            return _json({"code": "42703", "message": str(e).splitlines()[0]}, 400)
        except errors.InsufficientPrivilege as e:
            return _json({"code": "42501", "message": str(e).splitlines()[0]}, 403)

    def _run(self, c, req, table, params, prefer):
        t = sql.Identifier(table)
        where, wvals = self._where(params)
        p = dict(params)
        if req.method == "GET":
            cols = p.get("select", "*")
            colsql = sql.SQL("*") if cols == "*" else sql.SQL(", ").join(sql.Identifier(x) for x in cols.split(","))
            q = sql.SQL("SELECT {} FROM {}").format(colsql, t) + where
            if "order" in p:
                col, *mods = p["order"].split(".")
                nulls = " NULLS LAST" if "nullslast" in mods else " NULLS FIRST" if "nullsfirst" in mods else ""
                q += sql.SQL(" ORDER BY {} {}").format(
                    sql.Identifier(col), sql.SQL(("DESC" if "desc" in mods else "ASC") + nulls)
                )
            if "limit" in p:
                q += sql.SQL(" LIMIT {}").format(sql.Literal(int(p["limit"])))
            rows = c.execute(q, wvals).fetchall()
            headers = {}
            if "count=exact" in prefer:
                total = c.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(t) + where, wvals).fetchone()["n"]
                headers["content-range"] = (f"0-{len(rows) - 1}/{total}" if rows else f"*/{total}")
            return _json(rows, 200, headers)
        if req.method == "POST":
            data = json.loads(req.content)
            data = data if isinstance(data, list) else [data]
            if any(set(r) != set(data[0]) for r in data):  # PostgREST thật từ chối: PGRST102
                return _json({"code": "PGRST102", "message": "All object keys must match"}, 400)
            upsert = "on_conflict" in p and "resolution=merge-duplicates" in prefer
            out = []
            for row in data:
                cols = list(row)
                q = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    t, sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(sql.Placeholder() * len(cols)),
                )
                if upsert:
                    keys = p["on_conflict"].split(",")
                    sets = [x for x in cols if x not in keys]
                    q += sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
                        sql.SQL(", ").join(map(sql.Identifier, keys)),
                        sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(x)) for x in sets),
                    )
                q += sql.SQL(" RETURNING *")
                vals = [Jsonb(v) if isinstance(v, (list, dict)) else v for v in row.values()]
                out += c.execute(q, vals).fetchall()
            return _json(out, 201) if "return=representation" in prefer else httpx.Response(201)
        if req.method == "PATCH":
            data = json.loads(req.content)
            q = sql.SQL("UPDATE {} SET {}").format(
                t, sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in data)
            ) + where + sql.SQL(" RETURNING *")
            return _json(c.execute(q, [*data.values(), *wvals]).fetchall())
        if req.method == "DELETE":
            q = sql.SQL("DELETE FROM {}").format(t) + where + sql.SQL(" RETURNING *")
            return _json(c.execute(q, wvals).fetchall())
        return _json({"message": "method not allowed"}, 405)
