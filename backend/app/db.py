"""Lớp dữ liệu qua Supabase Data API (PostgREST) bằng SUPABASE_URL + khóa secret.

Không cần mật khẩu database. Dùng khóa `service_role`/secret, tuyệt đối không dùng khóa
anon/publishable (bảng đã bật RLS nên khóa công khai không đọc được gì, đúng chủ đích).
Khóa này có toàn quyền với database: chỉ đặt ở biến môi trường phía server (Vercel),
không bao giờ đưa vào trình duyệt hay mã nguồn.

Bảng được tạo bằng supabase/schema.sql (chạy một lần trong SQL Editor): Data API không
cho chạy DDL.
"""
import os
from datetime import datetime, timedelta, timezone

import httpx


class DBError(RuntimeError):
    pass


class Conflict(DBError):
    """Vi phạm ràng buộc duy nhất (ví dụ trùng tên tài khoản)."""


_client: httpx.Client | None = None
_transport: httpx.BaseTransport | None = None  # chỉ dùng khi kiểm thử


def _config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("Thiếu SUPABASE_URL hoặc SUPABASE_SECRET_KEY")
    return url, key


def config_ok() -> bool:
    try:
        _config()
        return True
    except RuntimeError:
        return False


def _http() -> httpx.Client:
    global _client
    if _client is None:
        url, key = _config()
        headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # khóa service_role kiểu cũ (JWT) cần thêm Bearer
            headers["Authorization"] = f"Bearer {key}"
        # Khóa mới (sb_secret_...) chỉ đi qua header apikey, không phải JWT.
        _client = httpx.Client(
            base_url=f"{url}/rest/v1", headers=headers, timeout=15, transport=_transport
        )
    return _client


def reset_client() -> None:
    global _client
    if _client is not None:
        _client.close()
    _client = None


def _explain(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        body = {}
    code, msg = body.get("code", ""), body.get("message", r.text[:200])
    if r.status_code in (401, 403) and code != "42501":
        return "Supabase từ chối khóa (kiểm tra SUPABASE_URL và SUPABASE_SECRET_KEY)"
    if code == "42501" or "permission denied" in str(msg).lower():
        return (
            "Supabase báo thiếu quyền trên bảng. Kiểm tra theo thứ tự: "
            "(1) SUPABASE_SECRET_KEY phải là khóa secret/service_role, KHÔNG phải khóa anon/publishable "
            "(khóa công khai bị thu hồi quyền theo thiết kế); "
            "(2) chạy lại supabase/schema.sql, đặc biệt phần GRANT cho service_role"
        )
    if code in ("42703", "PGRST204") or "column" in str(msg).lower() and (
        "does not exist" in str(msg).lower() or "schema cache" in str(msg).lower()
    ):
        return "Thiếu cột trong database: hãy chạy lại toàn bộ supabase/schema.sql trong SQL Editor (file đã được cập nhật)"
    if code in ("PGRST205", "42P01") or "schema cache" in str(msg):
        return "Chưa có bảng: hãy chạy supabase/schema.sql trong SQL Editor của Supabase"
    return f"Supabase lỗi {r.status_code}: {str(msg)[:200]}"


def _req(method: str, table: str, params=None, body=None, prefer: str | None = None) -> httpx.Response:
    headers = {"Prefer": prefer} if prefer else {}
    try:
        r = _http().request(method, f"/{table}", params=params, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise DBError(f"Không kết nối được Supabase: {type(e).__name__}") from e
    if r.status_code >= 400:
        try:
            code = r.json().get("code")
        except ValueError:
            code = None
        if r.status_code == 409 or code == "23505":
            raise Conflict("Vi phạm ràng buộc duy nhất")
        raise DBError(_explain(r))
    return r


# ---------- thao tác ----------
def eq(v) -> str:
    return f"eq.{v}"


def select(table: str, columns: str = "*", filters: dict | None = None, order: str | None = None, limit: int | None = None) -> list[dict]:
    params = [("select", columns)]
    params += [(k, v) for k, v in (filters or {}).items()]
    if order:
        params.append(("order", order))
    if limit is not None:
        params.append(("limit", str(limit)))
    return _req("GET", table, params).json()


def count(table: str, filters: dict | None = None) -> int:
    params = [("select", "id"), ("limit", "1")] + [(k, v) for k, v in (filters or {}).items()]
    r = _req("GET", table, params, prefer="count=exact")
    return int(r.headers.get("content-range", "*/0").rsplit("/", 1)[-1])


def insert(table: str, row: dict) -> dict:
    r = _req("POST", table, body=row, prefer="return=representation")
    rows = r.json()
    return rows[0] if rows else {}


def insert_quiet(table: str, row: dict) -> None:
    _req("POST", table, body=row, prefer="return=minimal")


def upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    """Chèn hoặc cập nhật hàng loạt. Mọi hàng PHẢI có cùng tập khóa (PostgREST từ chối nếu khác nhau,
    và nếu thiếu khóa thì cột đó bị ghi đè bằng NULL/mặc định)."""
    if not rows:
        return
    keys = set(rows[0])
    if any(set(r) != keys for r in rows):
        raise ValueError("upsert: các hàng có tập khóa khác nhau")
    _req(
        "POST", table, params=[("on_conflict", on_conflict)], body=rows,
        prefer="resolution=merge-duplicates,return=minimal",
    )


def update(table: str, filters: dict, values: dict) -> list[dict]:
    return _req("PATCH", table, list(filters.items()), values, prefer="return=representation").json()


def delete(table: str, filters: dict) -> list[dict]:
    return _req("DELETE", table, list(filters.items()), prefer="return=representation").json()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ago(**kw) -> str:
    return iso(utcnow() - timedelta(**kw))
