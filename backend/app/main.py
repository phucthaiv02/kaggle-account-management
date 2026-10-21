"""API quản lý nhiều tài khoản Kaggle (Vercel + Supabase Data API)."""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import auth, db, sessions
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

# Theo dõi session: số notebook liệt kê / hỏi trạng thái mỗi lần, và thời gian tối đa cho mỗi tài khoản
SESSIONS_LIMIT = int(os.environ.get("SESSIONS_LIMIT", "30"))
SESSIONS_STATUS_LIMIT = int(os.environ.get("SESSIONS_STATUS_LIMIT", "20"))
SESSIONS_DURATION_BUDGET = int(os.environ.get("SESSIONS_DURATION_BUDGET", "3"))
SESSIONS_TIMEOUT = float(os.environ.get("SESSIONS_TIMEOUT", "40"))
SESSIONS_ACCEL_BUDGET = int(os.environ.get("SESSIONS_ACCEL_BUDGET", "8"))

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


def _account_view(row: dict, running: int = 0) -> dict:
    has_quota = any(row.get(k) is not None for k in ("gpu_total", "tpu_total"))
    quota = None
    if has_quota:
        quota = {k: row.get(k) for k in ("gpu_used", "gpu_total", "tpu_used", "tpu_total", "refresh_at")}
    return {
        "id": row["id"], "name": row["name"], "username": row["username"], "note": row["note"],
        "created_at": row["created_at"], "last_checked": row["last_checked"],
        "last_error": row["last_error"], "quota": quota, "running_sessions": running,
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
    running: dict[int, int] = {}
    try:  # chưa chạy lại schema.sql (chưa có bảng session) thì trang này vẫn phải dùng được
        for r in db.select("kernel_sessions", "account_id", {"status": eq("running")}):
            running[r["account_id"]] = running.get(r["account_id"], 0) + 1
    except DBError:
        pass
    return [_account_view(r, running.get(r["id"], 0)) for r in rows]


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


# ---------- theo dõi session ----------
def _save_sessions_state(account_id: int, error: str | None) -> None:
    db.update("accounts", {"id": eq(account_id)}, {"sessions_checked_at": db.iso(db.utcnow()), "sessions_error": error})


def _store_sessions(account_id: int, rows: list[dict]) -> None:
    db.upsert("kernel_sessions", rows, "account_id,ref,version")
    _save_sessions_state(account_id, None)
    db.delete("kernel_sessions", {"account_id": eq(account_id), "updated_at": f"lt.{db.ago(days=90)}"})


async def refresh_sessions_account(acc: dict, budget_end: float, sem: asyncio.Semaphore) -> dict:
    timeout = min(SESSIONS_TIMEOUT, budget_end - time.monotonic())
    if timeout < 8:
        return {"ok": False, "skipped": True}
    try:
        known = await asyncio.to_thread(
            db.select, "kernel_sessions", "*", {"account_id": eq(acc["id"])}, None, 2000
        )
        opts = sessions.probe_opts(
            known, SESSIONS_LIMIT, SESSIONS_STATUS_LIMIT, SESSIONS_DURATION_BUDGET, max(5.0, timeout - 8),
            SESSIONS_ACCEL_BUDGET,
        )
        res = await kc.fetch_sessions(decrypt(acc["token_enc"]), acc["username"], opts, timeout, sem)
        if res.get("error"):
            raise kc.KaggleError(res["error"])
        rows = sessions.merge(acc["id"], res.get("items", []), known, db.iso(db.utcnow()))
        await asyncio.to_thread(_store_sessions, acc["id"], rows)
    except Exception as e:  # mọi lỗi (Kaggle, database, giải mã token...) đều phải hiện ở thanh tài khoản
        msg = str(e) or type(e).__name__
        try:
            await asyncio.to_thread(_save_sessions_state, acc["id"], msg[:500])
        except Exception:
            pass
        return {"ok": False, "error": msg}
    return {
        "ok": True, "n": len(res.get("items", [])), "partial": bool(res.get("partial")),
        "running": sum(1 for r in rows if r["status"] == "running"),
    }


async def refresh_sessions_all() -> dict:
    accs = await asyncio.to_thread(db.select, "accounts", "id,username,token_enc,sessions_checked_at")
    # Chưa từng cập nhật đứng trước, rồi đến cũ nhất
    accs.sort(key=lambda a: (a["sessions_checked_at"] is not None, a["sessions_checked_at"] or "", a["id"]))
    budget_end = time.monotonic() + REFRESH_BUDGET
    sem = asyncio.Semaphore(kc.CONCURRENCY)
    results = await asyncio.gather(*(refresh_sessions_account(a, budget_end, sem) for a in accs), return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
    skipped = sum(1 for r in results if isinstance(r, dict) and r.get("skipped"))
    return {
        "total": len(accs), "done": len(ok), "skipped": skipped, "failed": len(accs) - len(ok) - skipped,
        "running": sum(r["running"] for r in ok), "partial": any(r.get("partial") for r in ok),
    }


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
def list_sessions():
    rows = db.select("kernel_sessions", "*", order="last_run_time.desc.nullslast", limit=1000)
    accounts = db.select(
        "accounts", "id,name,sessions_checked_at,sessions_error,gpu_used,gpu_total,tpu_used,tpu_total,refresh_at"
    )
    accounts.sort(key=lambda a: a["name"].lower())
    return {"now": db.iso(db.utcnow()), "accounts": accounts, "sessions": rows}


@app.post("/api/sessions/refresh", dependencies=[Depends(require_auth)])
async def refresh_sessions():
    return await refresh_sessions_all()


# ---------- Submissions ----------
SUBS_PER_COMP = int(os.environ.get("SUBMISSIONS_PER_COMPETITION", "50"))
SUBS_MAX_COMPS = int(os.environ.get("SUBMISSIONS_MAX_COMPETITIONS", "20"))
SUBS_TIMEOUT = float(os.environ.get("SUBMISSIONS_TIMEOUT", "40"))

COMP_KEYS = (
    "account_id", "slug", "title", "deadline", "category", "reward", "team_count", "user_rank",
    "max_daily_submissions", "evaluation_metric", "url", "active", "submissions_disabled",
    "submissions_fetched", "updated_at",
)
SUB_KEYS = (
    "account_id", "competition", "ref", "submitted_at", "description", "file_name", "status", "public_score",
    "private_score", "error_description", "team_name", "submitted_by", "url", "total_bytes", "updated_at",
)


def _save_subs_state(account_id: int, error: str | None) -> None:
    db.update("accounts", {"id": eq(account_id)}, {"subs_checked_at": db.iso(db.utcnow()), "subs_error": error})


def build_submission_rows(account_id: int, res: dict, now: str) -> tuple[list[dict], list[dict]]:
    """Chuyển kết quả của kaggle_probe thành hàng DB (mọi hàng cùng tập khóa, xem db.upsert)."""
    comps = [
        {
            "account_id": account_id, "slug": c["slug"], "title": c.get("title"), "deadline": c.get("deadline"),
            "category": c.get("category"), "reward": c.get("reward"), "team_count": c.get("team_count"),
            "user_rank": c.get("user_rank"), "max_daily_submissions": c.get("max_daily_submissions"),
            "evaluation_metric": c.get("evaluation_metric"), "url": c.get("url"), "active": bool(c.get("active", True)),
            "submissions_disabled": bool(c.get("submissions_disabled")),
            "submissions_fetched": bool(c.get("submissions_fetched")), "updated_at": now,
        }
        for c in res.get("competitions", [])
    ]
    subs = [
        {
            "account_id": account_id, "competition": s["competition"], "ref": s["ref"], "submitted_at": s.get("date"),
            "description": s.get("description"), "file_name": s.get("file_name"), "status": s.get("status") or "unknown",
            "public_score": s.get("public_score"), "private_score": s.get("private_score"),
            "error_description": s.get("error_description"), "team_name": s.get("team_name"),
            "submitted_by": s.get("submitted_by"), "url": s.get("url"), "total_bytes": s.get("total_bytes"),
            "updated_at": now,
        }
        for s in res.get("submissions", [])
    ]
    assert all(set(r) == set(COMP_KEYS) for r in comps) and all(set(r) == set(SUB_KEYS) for r in subs)
    return comps, subs


def _store_submissions(account_id: int, comps: list[dict], subs: list[dict], complete: bool) -> None:
    db.upsert("competition_entries", comps, "account_id,slug")
    db.upsert("submissions", subs, "account_id,competition,ref")
    if complete:  # danh sách đầy đủ: competition không còn trong danh sách "còn hạn" thì đánh dấu đã kết thúc
        keep = {c["slug"] for c in comps}
        for r in db.select("competition_entries", "id,slug,active", {"account_id": eq(account_id)}):
            if r["active"] and r["slug"] not in keep:
                db.update("competition_entries", {"id": eq(r["id"])}, {"active": False})
    _save_subs_state(account_id, None)


async def refresh_submissions_account(acc: dict, budget_end: float, sem: asyncio.Semaphore) -> dict:
    timeout = min(SUBS_TIMEOUT, budget_end - time.monotonic())
    if timeout < 8:
        return {"ok": False, "skipped": True}
    try:
        opts = {
            "mode": "submissions", "only_active": True, "max_competitions": SUBS_MAX_COMPS,
            "per_competition": SUBS_PER_COMP, "deadline_seconds": max(5.0, timeout - 8),
        }
        res = await kc.fetch_sessions(decrypt(acc["token_enc"]), acc["username"], opts, timeout, sem)
        if res.get("error"):
            raise kc.KaggleError(res["error"])
        comps, subs = build_submission_rows(acc["id"], res, db.iso(db.utcnow()))
        await asyncio.to_thread(_store_submissions, acc["id"], comps, subs, not res.get("partial"))
    except Exception as e:  # mọi lỗi đều hiện ở thanh tài khoản
        msg = str(e) or type(e).__name__
        try:
            await asyncio.to_thread(_save_subs_state, acc["id"], msg[:500])
        except Exception:
            pass
        return {"ok": False, "error": msg}
    return {
        "ok": True, "competitions": len(comps), "submissions": len(subs), "partial": bool(res.get("partial")),
        "errors": res.get("errors") or {}, "pending": sum(1 for s in subs if s["status"] == "pending"),
    }


async def refresh_submissions_all() -> dict:
    accs = await asyncio.to_thread(db.select, "accounts", "id,username,token_enc,subs_checked_at")
    accs.sort(key=lambda a: (a["subs_checked_at"] is not None, a["subs_checked_at"] or "", a["id"]))
    budget_end = time.monotonic() + REFRESH_BUDGET
    sem = asyncio.Semaphore(kc.CONCURRENCY)
    results = await asyncio.gather(*(refresh_submissions_account(a, budget_end, sem) for a in accs), return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
    skipped = sum(1 for r in results if isinstance(r, dict) and r.get("skipped"))
    return {
        "total": len(accs), "done": len(ok), "skipped": skipped, "failed": len(accs) - len(ok) - skipped,
        "competitions": sum(r["competitions"] for r in ok), "submissions": sum(r["submissions"] for r in ok),
        "pending": sum(r["pending"] for r in ok), "partial": any(r["partial"] or r["errors"] for r in ok),
    }


@app.get("/api/submissions", dependencies=[Depends(require_auth)])
def list_submissions():
    comps = db.select("competition_entries", "*", order="deadline.asc.nullslast", limit=1000)
    subs = db.select("submissions", "*", order="submitted_at.desc.nullslast", limit=5000)
    accounts = db.select("accounts", "id,name,subs_checked_at,subs_error")
    accounts.sort(key=lambda a: a["name"].lower())
    return {"now": db.iso(db.utcnow()), "accounts": accounts, "competitions": comps, "submissions": subs}


@app.post("/api/submissions/refresh", dependencies=[Depends(require_auth)])
async def refresh_submissions():
    return await refresh_submissions_all()


# ---------- frontend ----------
# Trên Vercel, thư mục public/ được phục vụ từ CDN. Route này để chạy cục bộ.
@app.get("/", include_in_schema=False)
def index():
    f = PUBLIC_DIR / "index.html"
    if not f.exists():
        raise HTTPException(404, "Không tìm thấy public/index.html")
    return FileResponse(f)
