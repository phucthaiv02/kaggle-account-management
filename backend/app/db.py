"""Postgres (Supabase) cho tài khoản và lịch sử quota.

Dùng chuỗi kết nối DATABASE_URL. Trên Vercel (serverless) nên dùng chuỗi của
Supabase *transaction pooler* (cổng 6543). Pooler ở chế độ transaction không hỗ trợ
prepared statement nên ta tắt chúng bằng prepare_threshold=None.
"""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

# Idempotent: chạy lại nhiều lần không sao. RLS được bật và thu hồi quyền của các role
# công khai của Supabase để khóa API công khai (anon key) không đọc được bảng chứa token.
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    username    TEXT,
    token_enc   TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked TIMESTAMPTZ,
    last_error  TEXT
);
CREATE TABLE IF NOT EXISTS quota_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    gpu_used    DOUBLE PRECISION,
    gpu_total   DOUBLE PRECISION,
    tpu_used    DOUBLE PRECISION,
    tpu_total   DOUBLE PRECISION,
    refresh_at  TIMESTAMPTZ,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_acc_id ON quota_snapshots(account_id, id DESC);
CREATE TABLE IF NOT EXISTS login_fails (
    id  BIGSERIAL PRIMARY KEY,
    ip  TEXT NOT NULL,
    ts  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_login_fails_ip_ts ON login_fails(ip, ts);

ALTER TABLE accounts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE quota_snapshots  ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_fails      ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON accounts, quota_snapshots, login_fails FROM %I', r);
      EXECUTE format('REVOKE ALL ON SEQUENCE accounts_id_seq, quota_snapshots_id_seq, login_fails_id_seq FROM %I', r);
    END IF;
  END LOOP;
END $$;
"""

_schema_ready = False


def _url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Thiếu DATABASE_URL (chuỗi kết nối Postgres của Supabase)")
    return url


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        _url(), row_factory=dict_row, prepare_threshold=None, connect_timeout=10
    )


def ensure_schema() -> None:
    """Tạo bảng nếu chưa có. Chạy tối đa một lần mỗi tiến trình (khi thành công)."""
    global _schema_ready
    if _schema_ready:
        return
    with _connect() as conn:
        # Khóa advisory để nhiều cold start cùng lúc không đua nhau tạo bảng
        conn.execute("SELECT pg_advisory_xact_lock(727274)")
        conn.execute(SCHEMA)
    _schema_ready = True


@contextmanager
def get_db():
    ensure_schema()
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
