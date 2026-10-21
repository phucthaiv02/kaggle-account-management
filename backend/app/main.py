"""API quản lý nhiều tài khoản Kaggle (chạy trên Vercel + Supabase)."""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import auth
from . import kaggle_client as kc
from .crypto import decrypt, encrypt
from .db import get_db

SESSION_TTL = 60 * 60 * 12
COOKIE = "kam_session"
# Tổng thời gian tối đa cho một lần "làm mới" (giây); phải nhỏ hơn maxDuration trong vercel.json
REFRESH_BUDGET = float(os.environ.get("REFRESH_BUDGET_SECONDS", "50"))
PUBLIC_DIR = Path(__file__).resolve().parents[2] / "public"
ON_VERCEL = bool(os.environ.get("VERCEL"))

LOGIN_MAX_PER_IP = 5
LOGIN_MAX_GLOBAL = 30
LOGIN_WINDOW_MIN = 15


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = [k for k in ("DATABASE_URL", "SECRET_KEY", "ADMIN_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise RuntimeError("Thiếu biến môi trường: " + ", ".join(missing))
    from .crypto import _fernet

    _fernet()  # báo lỗi sớm nếu SECRET_KEY sai định dạng
    yield


app = FastAPI(title="Kaggle Account Management", lifespan=lifespan)


# ---------- tiện ích ----------
def require_auth(request: Request) -> None:
    if not auth.verify_session(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Chưa đăng nhập")


def client_ip(request: Request) -> str:
    # Trên Vercel, x-forwarded-for do hạ tầng đặt; cục bộ lấy địa chỉ kết nối trực tiếp.
    fwd = request.headers.get("x-forwarded-for", "")
    if ON_VERCEL and fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _account_row(db, row) -> dict:
    snap = db.execute(
        "SELECT ts, gpu_used, gpu_total, tpu_used, tpu_total, refresh_at FROM quota_snapshots "
        "WHERE account_id=%s ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "name": row["name"],
        "username": row["username"],
        "note": row["note"],
        "created_at": row["created_at"],
        "last_checked": row["last_checked"],
        "last_error": row["last_error"],
        "quota": snap,
    }


async def refresh_account(account_id: int, budget_end: float | None = None, sem: asyncio.Semaphore | None = None) -> dict:
    """Chạy `kaggle quota` cho một tài khoản và lưu snapshot."""
    with get_db() as db:
        row = db.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Không có tài khoản")
    timeout = kc.DEFAULT_TIMEOUT
    if budget_end is not None:
        timeout = min(timeout, budget_end - time.monotonic())
        if timeout < 3:
            return {"ok": False, "skipped": True}
    try:
        token = decrypt(row["token_enc"])
        q = await kc.fetch_quota(token, row["username"], timeout, sem)
    except (kc.KaggleError, RuntimeError) as e:
        with get_db() as db:
            db.execute(
                "UPDATE accounts SET last_checked=now(), last_error=%s WHERE id=%s",
                (str(e), account_id),
            )
        return {"ok": False, "error": str(e)}
    with get_db() as db:
        db.execute(
            "INSERT INTO quota_snapshots(account_id,gpu_used,gpu_total,tpu_used,tpu_total,refresh_at,raw) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (account_id, q.gpu_used, q.gpu_total, q.tpu_used, q.tpu_total, q.refresh_at, q.raw),
        )
        db.execute("UPDATE accounts SET last_checked=now(), last_error=NULL WHERE id=%s", (account_id,))
        db.execute("DELETE FROM quota_snapshots WHERE account_id=%s AND ts < now() - interval '90 days'", (account_id,))
    return {"ok": True}


async def refresh_all() -> dict:
    """Làm mới lần lượt từ tài khoản cũ nhất, dừng khi hết ngân sách thời gian."""
    with get_db() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM accounts ORDER BY last_checked ASC NULLS FIRST, id")]
    budget_end = time.monotonic() + REFRESH_BUDGET
    sem = asyncio.Semaphore(kc.CONCURRENCY)
    results = await asyncio.gather(
        *(refresh_account(i, budget_end, sem) for i in ids), return_exceptions=True
    )
    done = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    skipped = sum(1 for r in results if isinstance(r, dict) and r.get("skipped"))
    failed = len(results) - done - skipped
    return {"total": len(ids), "done": done, "failed": failed, "skipped": skipped}


# ---------- schema ----------
class LoginIn(BaseModel):
    password: str


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    token: str = Field(min_length=8)
    username: str | None = None
    note: str = ""


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    token: str | None = Field(default=None, min_length=8)
    username: str | None = None
    note: str | None = None


# ---------- đăng nhập ----------
@app.get("/api/health")
def health():
    try:
        with get_db() as db:
            db.execute("SELECT 1")
        return {"ok": True}
    except Exception:
        raise HTTPException(503, "Không kết nối được database")


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    with get_db() as db:
        db.execute("DELETE FROM login_fails WHERE ts < now() - interval '1 day'")
        win = f"{LOGIN_WINDOW_MIN} minutes"
        per_ip = db.execute(
            "SELECT count(*) AS n FROM login_fails WHERE ip=%s AND ts > now() - %s::interval", (ip, win)
        ).fetchone()["n"]
        total = db.execute(
            "SELECT count(*) AS n FROM login_fails WHERE ts > now() - %s::interval", (win,)
        ).fetchone()["n"]
        if per_ip >= LOGIN_MAX_PER_IP or total >= LOGIN_MAX_GLOBAL:
            raise HTTPException(429, f"Sai quá nhiều lần, thử lại sau {LOGIN_WINDOW_MIN} phút")
        if not auth.check_password(body.password):
            db.execute("INSERT INTO login_fails(ip) VALUES (%s)", (ip,))
            db.commit()  # giữ lại bản ghi dù trả lỗi
            raise HTTPException(401, "Sai mật khẩu")
        db.execute("DELETE FROM login_fails WHERE ip=%s", (ip,))
    response.set_cookie(
        COOKIE, auth.make_session(SESSION_TTL), max_age=SESSION_TTL, httponly=True,
        samesite="strict", secure=ON_VERCEL or os.environ.get("COOKIE_SECURE", "0") == "1",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------- tài khoản ----------
@app.get("/api/accounts", dependencies=[Depends(require_auth)])
def list_accounts():
    with get_db() as db:
        rows = db.execute("SELECT * FROM accounts ORDER BY lower(name)").fetchall()
        return [_account_row(db, r) for r in rows]


@app.post("/api/accounts", status_code=201, dependencies=[Depends(require_auth)])
async def add_account(body: AccountIn):
    try:
        with get_db() as db:
            new_id = db.execute(
                "INSERT INTO accounts(name, username, token_enc, note) VALUES (%s,%s,%s,%s) RETURNING id",
                (body.name.strip(), (body.username or "").strip() or None, encrypt(body.token.strip()), body.note),
            ).fetchone()["id"]
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Tên tài khoản đã tồn tại")
    result = await refresh_account(new_id)  # kiểm tra token ngay
    return {"id": new_id, "check": result}


@app.patch("/api/accounts/{account_id}", dependencies=[Depends(require_auth)])
async def update_account(account_id: int, body: AccountPatch):
    sets, vals = [], []
    if body.name is not None:
        sets.append("name=%s"); vals.append(body.name.strip())
    if body.username is not None:
        sets.append("username=%s"); vals.append(body.username.strip() or None)
    if body.note is not None:
        sets.append("note=%s"); vals.append(body.note)
    if body.token is not None:
        sets.append("token_enc=%s"); vals.append(encrypt(body.token.strip()))
    if not sets:
        return {"ok": True}
    with get_db() as db:
        cur = db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=%s", (*vals, account_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Không có tài khoản")
    if body.token is not None:
        return {"ok": True, "check": await refresh_account(account_id)}
    return {"ok": True}


@app.delete("/api/accounts/{account_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_account(account_id: int):
    with get_db() as db:
        cur = db.execute("DELETE FROM accounts WHERE id=%s", (account_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Không có tài khoản")


@app.post("/api/accounts/{account_id}/refresh", dependencies=[Depends(require_auth)])
async def refresh_one(account_id: int):
    return await refresh_account(account_id)


@app.post("/api/refresh-all", dependencies=[Depends(require_auth)])
async def refresh_everything():
    return await refresh_all()


@app.get("/api/accounts/{account_id}/history", dependencies=[Depends(require_auth)])
def history(account_id: int, limit: int = 200):
    with get_db() as db:
        rows = db.execute(
            "SELECT ts, gpu_used, gpu_total, tpu_used, tpu_total, refresh_at FROM quota_snapshots "
            "WHERE account_id=%s ORDER BY id DESC LIMIT %s",
            (account_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return list(reversed(rows))


@app.get("/api/accounts/{account_id}/kernels", dependencies=[Depends(require_auth)])
async def kernels(account_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Không có tài khoản")
    try:
        out = await kc.list_kernels(decrypt(row["token_enc"]), row["username"])
    except (kc.KaggleError, RuntimeError) as e:
        raise HTTPException(502, str(e))
    return {"csv": out}


# ---------- frontend ----------
# Trên Vercel, thư mục public/ được phục vụ từ CDN. Route này để chạy cục bộ.
@app.get("/", include_in_schema=False)
def index():
    f = PUBLIC_DIR / "index.html"
    if not f.exists():
        raise HTTPException(404, "Không tìm thấy public/index.html")
    return FileResponse(f)
