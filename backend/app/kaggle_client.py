"""Gọi Kaggle CLI 2.x cho từng tài khoản, tách biệt hoàn toàn thông tin xác thực.

Mỗi lần gọi dùng một thư mục HOME tạm riêng, nên không đọc nhầm token của máy chủ
và không lẫn token giữa các tài khoản.
"""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

KAGGLE_BIN = os.environ.get("KAGGLE_BIN", "kaggle")
TIMEOUT = int(os.environ.get("KAGGLE_TIMEOUT", "60"))
# Giới hạn số tiến trình kaggle chạy song song
_sem = asyncio.Semaphore(int(os.environ.get("KAGGLE_CONCURRENCY", "3")))


class KaggleError(RuntimeError):
    pass


@dataclass
class Quota:
    gpu_used: float | None
    gpu_total: float | None
    tpu_used: float | None
    tpu_total: float | None
    raw: str


# Khớp các dòng dạng: "TPU 0.00h used, 20.00h remaining" (định dạng quan sát được
# trong báo cáo thử nghiệm của Kaggle CLI 2.x). Nếu Kaggle đổi định dạng, chỉnh ở đây.
_LINE = re.compile(
    r"(GPU|TPU)\D{0,40}?([\d.]+)\s*h(?:ours?)?\s*used\D{0,40}?([\d.]+)\s*h(?:ours?)?\s*(?:remaining|left)",
    re.IGNORECASE,
)


def parse_quota(text: str) -> Quota:
    found: dict[str, tuple[float, float]] = {}
    for m in _LINE.finditer(text):
        kind = m.group(1).upper()
        used, remaining = float(m.group(2)), float(m.group(3))
        found[kind] = (used, used + remaining)
    if not found:
        raise KaggleError("Không đọc được quota từ đầu ra của `kaggle quota`: " + text.strip()[:300])
    g = found.get("GPU", (None, None))
    t = found.get("TPU", (None, None))
    return Quota(g[0], g[1], t[0], t[1], text.strip())


def _run_sync(args: list[str], token: str, username: str | None) -> str:
    home = tempfile.mkdtemp(prefix="kg_")
    try:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "KAGGLE_CONFIG_DIR": os.path.join(home, ".kaggle"),
            "KAGGLE_API_TOKEN": token,  # access token kiểu mới
        }
        if username:  # tương thích khóa API kiểu cũ (username + key)
            env["KAGGLE_USERNAME"] = username
            env["KAGGLE_KEY"] = token
        try:
            p = subprocess.run(
                [KAGGLE_BIN, *args], env=env, capture_output=True, text=True, timeout=TIMEOUT
            )
        except FileNotFoundError as e:
            raise KaggleError(f"Không tìm thấy lệnh '{KAGGLE_BIN}'. Cài bằng: pip install kaggle") from e
        except subprocess.TimeoutExpired as e:
            raise KaggleError(f"Kaggle CLI quá thời gian ({TIMEOUT}s)") from e
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).strip() or f"exit {p.returncode}"
            raise KaggleError(msg[:500])
        return p.stdout
    finally:
        shutil.rmtree(home, ignore_errors=True)


async def run_kaggle(args: list[str], token: str, username: str | None = None) -> str:
    async with _sem:
        return await asyncio.to_thread(_run_sync, args, token, username)


async def fetch_quota(token: str, username: str | None = None) -> Quota:
    return parse_quota(await run_kaggle(["quota"], token, username))


async def list_kernels(token: str, username: str | None = None) -> str:
    return await run_kaggle(["kernels", "list", "--mine", "--csv", "-p", "1"], token, username)
