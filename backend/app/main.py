"""API quản lý nhiều tài khoản Kaggle (Vercel + Supabase Data API)."""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import auth, db
from . import kaggle_client as kc
from .crypto import decrypt, encrypt
from .db import Conflict, DBError, eq

SESSION_TTL = 60 * 60 * 12
COOKIE = "kam_session"
# Tổng thời gian tối đa cho một lần "làm mới" (giây); phải nhỏ hơn maxDuration trong vercel.json
REFRESH_BUDGET = float(os.environ.get("REFRESH_BUDGET_SECONDS", "50"))
PUBLIC_DIR = Path(__file__).resolve().parents[2] / "public"
ON_VERCEL = bool(os.environ.get("VERCEL"))

LOGIN_MAX_PER_IP = 5
LOGIN_MAX_GLOBAL = 30
LOGIN_WINDOW_MIN = 15

# Không bao giờ lấy token_enc khi trả danh sách ra giao diện
ACC_COLS = "id,name,username,note,created_at,last_checked,last_error,gpu_used,gpu_total,tpu_used,tpu_total,refresh_at"


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = []
    if not db.config_ok():
        missing += ["SUPABASE_URL", "SUPABASE_SECRET_KEY"]
    missing += [k for k in ("SECRET_KEY", "ADMIN_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise RuntimeError("Thiếu biến môi trường: " + ", ".join(missing))
    from .crypto import _fernet

    _fernet()  # báo lỗi sớm nếu SECRET_KEY sai định dạng
    yield


app = FastAPI(title="Kaggle Account Management", lifespan=lifespan)


@app.exception_handler(DBError)
async def db_error_handler(request: Request, exc: DBError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


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


def _account_view(row: dict) -> dict:
    has_quota = any(row.get(k) is not None for k in ("gpu_total", "tpu_total"))
    quota = None
    if has_quota:
        quota = {k: row.get(k) for k in ("gpu_used", "gpu_total", "tpu_used", "tpu_total", "refresh_at")}
    return {
        "id": row["id"], "name": row["name"], "username": row["username"], "note": row["note"],
        "created_at": row["created_at"], "last_checked": row["last_checked"],
        "last_error": row["last_error"], "quota": quota,
    }


def _load_account(account_id: int) -> dict | None:
    rows = db.select("accounts", "*", {"id": eq(account_id)})
    return rows[0] if rows else None


def _save_error(account_id: int, msg: str) -> None:
    db.update("accounts", {"id": eq(account_id)}, {"last_checked": db.iso(db.utcnow()), "last_error": msg})


def _save_quota(account_id: int, q: kc.Quota) -> None:
    vals = {
        "gpu_used": q.gpu_used, "gpu_total": q.gpu_total, "tpu_used": q.tpu_used, "tpu_total": q.tpu_total,
        "refresh_at": db.iso(q.refresh_at) if q.refresh_at else None,
    }
    db.update("accounts", {"id": eq(account_id)}, {**vals, "last_checked": db.iso(db.utcnow()), "last_error": None})
    db.insert_quiet("quota_snapshots", {"account_id": account_id, **vals, "raw": q.raw})
    db.delete("quota_snapshots", {"account_id": eq(account_id), "ts": f"lt.{db.ago(days=90)}"})


async def refresh_account(account_id: int, budget_end: float | None = None, sem: asyncio.Semaphore | None = None) -> dict:
    """Chạy `kaggle quota` cho một tài khoản và lưu kết quả."""
    row = await asyncio.to_thread(_load_account, account_id)
    if not row:
        raise HTTPException(404, "Không có tài khoản")
    timeout = kc.DEFAULT_TIMEOUT
    if budget_end is not None:
        timeout = min(timeout, budget_end - time.monotonic())
        if timeout < 3:
            return {"ok": False, "skipped": True}
    try:
        q = await kc.fetch_quota(decrypt(row["token_enc"]), row["username"], timeout, sem)
    except (kc.KaggleError, RuntimeError) as e:
        await asyncio.to_thread(_save_error, account_id, str(e))
        return {"ok": False, "error": str(e)}
    await asyncio.to_thread(_save_quota, account_id, q)
    return {"ok": True}


async def refresh_all() -> dict:
    """Làm mới lần lượt từ tài khoản cũ nhất, dừng khi hết ngân sách thời gian."""
    rows = await asyncio.to_thread(db.select, "accounts", "id,last_checked")
    # Tài khoản chưa kiểm tra lần nào đứng trước, rồi đến cũ nhất
    rows.sort(key=lambda r: (r["last_checked"] is not None, r["last_checked"] or "", r["id"]))
    budget_end = time.monotonic() + REFRESH_BUDGET
    sem = asyncio.Semaphore(kc.CONCURRENCY)
    results = await asyncio.gather(
        *(refresh_account(r["id"], budget_end, sem) for r in rows), return_exceptions=True
    )
    done = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    skipped = sum(1 for r in results if isinstance(r, dict) and r.get("skipped"))
    return {"total": len(rows), "done": done, "failed": len(rows) - done - skipped, "skipped": skipped}


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
    db.select("accounts", "id", limit=1)  # lỗi sẽ thành 503 kèm gợi ý cách sửa
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    cutoff = f"gt.{db.ago(minutes=LOGIN_WINDOW_MIN)}"
    per_ip = db.count("login_fails", {"ip": eq(ip), "ts": cutoff})
    total = db.count("login_fails", {"ts": cutoff})
    if per_ip >= LOGIN_MAX_PER_IP or total >= LOGIN_MAX_GLOBAL:
        raise HTTPException(429, f"Sai quá nhiều lần, thử lại sau {LOGIN_WINDOW_MIN} phút")
    if not auth.check_password(body.password):
        db.insert_quiet("login_fails", {"ip": ip})
        db.delete("login_fails", {"ts": f"lt.{db.ago(days=1)}"})  # dọn bản ghi cũ
        raise HTTPException(401, "Sai mật khẩu")
    if per_ip:
        db.delete("login_fails", {"ip": eq(ip)})
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
    rows = db.select("accounts", ACC_COLS)
    rows.sort(key=lambda r: r["name"].lower())
    return [_account_view(r) for r in rows]


@app.post("/api/accounts", status_code=201, dependencies=[Depends(require_auth)])
async def add_account(body: AccountIn):
    row = {
        "name": body.name.strip(), "username": (body.username or "").strip() or None,
        "token_enc": encrypt(body.token.strip()), "note": body.note,
    }
    try:
        new = await asyncio.to_thread(db.insert, "accounts", row)
    except Conflict:
        raise HTTPException(409, "Tên tài khoản đã tồn tại")
    result = await refresh_account(new["id"])  # kiểm tra token ngay
    return {"id": new["id"], "check": result}


@app.patch("/api/accounts/{account_id}", dependencies=[Depends(require_auth)])
async def update_account(account_id: int, body: AccountPatch):
    vals = {}
    if body.name is not None:
        vals["name"] = body.name.strip()
    if body.username is not None:
        vals["username"] = body.username.strip() or None
    if body.note is not None:
        vals["note"] = body.note
    if body.token is not None:
        vals["token_enc"] = encrypt(body.token.strip())
    if not vals:
        return {"ok": True}
    try:
        rows = await asyncio.to_thread(db.update, "accounts", {"id": eq(account_id)}, vals)
    except Conflict:
        raise HTTPException(409, "Tên tài khoản đã tồn tại")
    if not rows:
        raise HTTPException(404, "Không có tài khoản")
    if body.token is not None:
        return {"ok": True, "check": await refresh_account(account_id)}
    return {"ok": True}


@app.delete("/api/accounts/{account_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_account(account_id: int):
    if not db.delete("accounts", {"id": eq(account_id)}):
        raise HTTPException(404, "Không có tài khoản")


@app.post("/api/accounts/{account_id}/refresh", dependencies=[Depends(require_auth)])
async def refresh_one(account_id: int):
    return await refresh_account(account_id)


@app.post("/api/refresh-all", dependencies=[Depends(require_auth)])
async def refresh_everything():
    return await refresh_all()


@app.get("/api/accounts/{account_id}/history", dependencies=[Depends(require_auth)])
def history(account_id: int, limit: int = 200):
    rows = db.select(
        "quota_snapshots", "ts,gpu_used,gpu_total,tpu_used,tpu_total,refresh_at",
        {"account_id": eq(account_id)}, order="id.desc", limit=min(max(limit, 1), 1000),
    )
    return list(reversed(rows))


@app.get("/api/accounts/{account_id}/kernels", dependencies=[Depends(require_auth)])
async def kernels(account_id: int):
    row = await asyncio.to_thread(_load_account, account_id)
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
