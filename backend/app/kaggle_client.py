"""Gọi Kaggle CLI 2.x cho từng tài khoản, tách biệt hoàn toàn thông tin xác thực.

- Chạy bằng `python -m kaggle` để không phụ thuộc PATH (quan trọng trên Vercel).
- Mỗi lần gọi dùng một thư mục HOME tạm riêng, nên không đọc nhầm ~/.kaggle của máy
  chủ và không lẫn token giữa các tài khoản.
- Token chỉ truyền qua biến môi trường KAGGLE_API_TOKEN của tiến trình con.
- Các bí mật của ứng dụng (DATABASE_URL, SECRET_KEY...) không được chuyển cho tiến trình con.

Đầu ra của `kaggle quota --format json` (đã đối chiếu với mã nguồn kaggle 2.2.x):
  [{"resource": "GPU", "used": "16.33h", "remaining": "13.67h",
    "total": "30.00h", "refreshAt": "2026-10-24T00:00:00+00:00"}, {... "TPU" ...}]
"""
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime

def _cmd() -> list[str]:
    """Cho phép thay lệnh khi kiểm thử (KAGGLE_CMD). Mặc định: python -m kaggle."""
    if os.environ.get("KAGGLE_CMD"):
        return shlex.split(os.environ["KAGGLE_CMD"])
    return [sys.executable, "-m", "kaggle"]


DEFAULT_TIMEOUT = int(os.environ.get("KAGGLE_TIMEOUT", "30"))
CONCURRENCY = int(os.environ.get("KAGGLE_CONCURRENCY", "3"))

_SENSITIVE_ENV = {"DATABASE_URL", "SECRET_KEY", "ADMIN_PASSWORD", "SESSION_SECRET", "SUPABASE_SERVICE_ROLE_KEY"}


class KaggleError(RuntimeError):
    pass


@dataclass
class Quota:
    gpu_used: float | None
    gpu_total: float | None
    tpu_used: float | None
    tpu_total: float | None
    refresh_at: datetime | None
    raw: str


def _hours(s: str | None) -> float | None:
    if s is None:
        return None
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None


def _extract_json_list(text: str):
    """Lấy mảng JSON trong đầu ra; CLI có thể in thêm chữ hướng dẫn trước/sau."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            return obj
    return None


def parse_quota(text: str) -> Quota:
    rows = _extract_json_list(text)
    if not rows:
        raise KaggleError("Không đọc được quota từ đầu ra của `kaggle quota`: " + text.strip()[:300])
    by = {str(r.get("resource", "")).upper(): r for r in rows}
    if not by.keys() & {"GPU", "TPU"}:
        raise KaggleError("Đầu ra quota không có dòng GPU/TPU: " + text.strip()[:300])

    def pair(kind: str):
        r = by.get(kind)
        return (_hours(r.get("used")), _hours(r.get("total"))) if r else (None, None)

    refresh = None
    for r in rows:
        if r.get("refreshAt"):
            try:
                refresh = datetime.fromisoformat(str(r["refreshAt"]).replace("Z", "+00:00"))
            except ValueError:
                pass
            break
    g, t = pair("GPU"), pair("TPU")
    return Quota(g[0], g[1], t[0], t[1], refresh, json.dumps(rows))


def _run_sync(args: list[str], token: str, username: str | None, timeout: float) -> str:
    home = tempfile.mkdtemp(prefix="kg_")
    try:
        env = {
            k: v for k, v in os.environ.items()
            if k not in _SENSITIVE_ENV and not k.startswith("KAGGLE_")
        }
        env.update(
            HOME=home,
            KAGGLE_CONFIG_DIR=os.path.join(home, ".kaggle"),
            KAGGLE_API_TOKEN=token,  # access token kiểu mới
        )
        if username:  # tương thích khóa API kiểu cũ (username + key)
            env["KAGGLE_USERNAME"] = username
            env["KAGGLE_KEY"] = token
        try:
            p = subprocess.run(
                [*_cmd(), *args], env=env, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError as e:
            raise KaggleError("Không chạy được Kaggle CLI (đã cài package `kaggle` chưa?)") from e
        except subprocess.TimeoutExpired as e:
            raise KaggleError(f"Kaggle CLI quá thời gian ({int(timeout)}s)") from e
        if p.returncode != 0:
            lines = [l for l in (p.stderr or p.stdout).splitlines() if l.strip()]
            msg = lines[-1].strip() if lines else f"exit {p.returncode}"  # dòng ngoại lệ cuối, bỏ traceback
            raise KaggleError(msg[:400])
        return p.stdout
    finally:
        shutil.rmtree(home, ignore_errors=True)


async def run_kaggle(args, token, username=None, timeout: float | None = None, sem: asyncio.Semaphore | None = None) -> str:
    t = timeout if timeout is not None else DEFAULT_TIMEOUT
    if sem is None:
        return await asyncio.to_thread(_run_sync, args, token, username, t)
    async with sem:
        return await asyncio.to_thread(_run_sync, args, token, username, t)


async def fetch_quota(token, username=None, timeout=None, sem=None) -> Quota:
    out = await run_kaggle(["quota", "--format", "json"], token, username, timeout, sem)
    return parse_quota(out)


async def list_kernels(token, username=None, timeout=None) -> str:
    return await run_kaggle(["kernels", "list", "--mine", "--page-size", "20", "-v"], token, username, timeout)
