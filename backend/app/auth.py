"""Phiên đăng nhập không lưu trạng thái (stateless) bằng cookie có chữ ký HMAC.

Serverless không giữ được bộ nhớ giữa các lần gọi nên không thể lưu phiên trong RAM.
Khóa ký được dẫn xuất từ SECRET_KEY *và* ADMIN_PASSWORD, nên đổi mật khẩu quản trị
sẽ vô hiệu hóa mọi phiên đang có.
"""
import hashlib
import hmac
import os
import time


def _key() -> bytes:
    secret = os.environ.get("SECRET_KEY", "")
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if not secret or not pw:
        raise RuntimeError("Thiếu SECRET_KEY hoặc ADMIN_PASSWORD")
    return hashlib.sha256(f"kam-session|{secret}|{pw}".encode()).digest()


def _sig(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(ttl_seconds: int) -> str:
    exp = str(int(time.time()) + ttl_seconds)
    return f"{exp}.{_sig(exp)}"


def verify_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp, _, sig = cookie.partition(".")
    if not exp.isdigit() or not hmac.compare_digest(sig, _sig(exp)):
        return False
    return int(exp) > time.time()


def check_password(given: str) -> bool:
    expected = os.environ.get("ADMIN_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())
