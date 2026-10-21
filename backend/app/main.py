"""API quản lý nhiều tài khoản Kaggle."""
import asyncio
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import kaggle_client as kc
from .crypto import decrypt, encrypt
from .db import get_db, init_db

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))
SESSION_TTL = 60 * 60 * 12
COOKIE = "kam_session"
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

# Phiên đăng nhập lưu trong bộ nhớ (đủ cho 1 tiến trình / 1 quản trị viên)
_sessions: dict[str, float] = {}
_login_fails: dict[str, list[float]] = {}


# ---------- tiện ích ----------
def _now_sql(db) -> str:
    return db.execute("SELECT datetime('now')").fetchone()[0]


def require_auth(request: Request) -> None:
    sid = request.cookies.get(COOKIE)
    exp = _sessions.get(sid or "")
    if not exp or exp < time.time():
        _sessions.pop(sid or "", None)
        raise HTTPException(401, "Chưa đăng nhập")


def _account_row(db, row) -> dict:
    snap = db.execute(
        "SELECT ts, gpu_used, gpu_total, tpu_used, tpu_total FROM quota_snapshots "
        "WHERE account_id=? ORDER BY id DESC LIMIT 1",
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
        "quota": dict(snap) if snap else None,
    }


async def refresh_account(account_id: int) -> dict:
    """Gọi `kaggle quota` cho một tài khoản và lưu snapshot."""
    with get_db() as db:
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Không có tài khoản")
    try:
        token = decrypt(row["token_enc"])
        q = await kc.fetch_quota(token, row["username"])
    except (kc.KaggleError, RuntimeError) as e:
        with get_db() as db:
            db.execute(
                "UPDATE accounts SET last_checked=datetime('now'), last_error=? WHERE id=?",
                (str(e), account_id),
            )
        return {"ok": False, "error": str(e)}
    with get_db() as db:
        db.execute(
            "INSERT INTO quota_snapshots(account_id,gpu_used,gpu_total,tpu_used,tpu_total,raw) "
            "VALUES (?,?,?,?,?,?)",
            (account_id, q.gpu_used, q.gpu_total, q.tpu_used, q.tpu_total, q.raw),
        )
        db.execute(
            "UPDATE accounts SET last_checked=datetime('now'), last_error=NULL WHERE id=?",
            (account_id,),
        )
    return {"ok": True}


async def refresh_all() -> list[dict]:
    with get_db() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM accounts")]
    results = await asyncio.gather(*(refresh_account(i) for i in ids), return_exceptions=True)
    return [r if isinstance(r, dict) else {"ok": False, "error": str(r)} for r in results]


async def _background_loop():
    while True:
        await asyncio.sleep(REFRESH_MINUTES * 60)
        try:
            await refresh_all()
        except Exception as e:  # không để job nền chết
            print("refresh_all lỗi:", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ADMIN_PASSWORD:
        raise RuntimeError("Thiếu ADMIN_PASSWORD")
    from .crypto import _fernet

    _fernet()  # báo lỗi sớm nếu thiếu SECRET_KEY
    init_db()
    task = asyncio.create_task(_background_loop())
    yield
    task.cancel()


app = FastAPI(title="Kaggle Account Management", lifespan=lifespan)


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
@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < 900]
    if len(fails) >= 5:
        raise HTTPException(429, "Sai quá nhiều lần, thử lại sau 15 phút")
    if not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        fails.append(now)
        _login_fails[ip] = fails
        raise HTTPException(401, "Sai mật khẩu")
    _login_fails.pop(ip, None)
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = now + SESSION_TTL
    response.set_cookie(
        COOKIE, sid, max_age=SESSION_TTL, httponly=True, samesite="strict",
        secure=os.environ.get("COOKIE_SECURE", "0") == "1",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(require_auth)])
def me():
    return {"ok": True, "refresh_minutes": REFRESH_MINUTES}


# ---------- tài khoản ----------
@app.get("/api/accounts", dependencies=[Depends(require_auth)])
def list_accounts():
    with get_db() as db:
        rows = db.execute("SELECT * FROM accounts ORDER BY name COLLATE NOCASE").fetchall()
        return [_account_row(db, r) for r in rows]


@app.post("/api/accounts", status_code=201, dependencies=[Depends(require_auth)])
async def add_account(body: AccountIn):
    with get_db() as db:
        try:
            cur = db.execute(
                "INSERT INTO accounts(name, username, token_enc, note) VALUES (?,?,?,?)",
                (body.name.strip(), (body.username or "").strip() or None, encrypt(body.token.strip()), body.note),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "Tên tài khoản đã tồn tại")
            raise
        new_id = cur.lastrowid
    result = await refresh_account(new_id)  # kiểm tra token ngay
    return {"id": new_id, "check": result}


@app.patch("/api/accounts/{account_id}", dependencies=[Depends(require_auth)])
async def update_account(account_id: int, body: AccountPatch):
    sets, vals = [], []
    if body.name is not None:
        sets.append("name=?"); vals.append(body.name.strip())
    if body.username is not None:
        sets.append("username=?"); vals.append(body.username.strip() or None)
    if body.note is not None:
        sets.append("note=?"); vals.append(body.note)
    if body.token is not None:
        sets.append("token_enc=?"); vals.append(encrypt(body.token.strip()))
    if not sets:
        return {"ok": True}
    with get_db() as db:
        cur = db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", (*vals, account_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Không có tài khoản")
    if body.token is not None:
        return {"ok": True, "check": await refresh_account(account_id)}
    return {"ok": True}


@app.delete("/api/accounts/{account_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_account(account_id: int):
    with get_db() as db:
        cur = db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
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
            "SELECT ts, gpu_used, gpu_total, tpu_used, tpu_total FROM quota_snapshots "
            "WHERE account_id=? ORDER BY id DESC LIMIT ?",
            (account_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/accounts/{account_id}/kernels", dependencies=[Depends(require_auth)])
async def kernels(account_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Không có tài khoản")
    try:
        out = await kc.list_kernels(decrypt(row["token_enc"]), row["username"])
    except (kc.KaggleError, RuntimeError) as e:
        raise HTTPException(502, str(e))
    return {"csv": out}


# ---------- frontend ----------
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")
