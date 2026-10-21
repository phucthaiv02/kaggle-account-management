"""Hằng số và hàm dùng chung cho các bài kiểm thử."""
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = (ROOT / "supabase" / "schema.sql").read_text()
API_KEY = "sb_secret_test_key_123"

# `kaggle` giả mô phỏng đúng định dạng `kaggle quota --format json` (đối chiếu mã nguồn kaggle 2.2.x),
# có in thêm chữ hướng dẫn xác thực trước JSON như CLI thật hay làm.
FAKE_KAGGLE = r'''
import json, os, sys
# Tiến trình con không được thấy bí mật của ứng dụng
for k in ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SECRET_KEY", "ADMIN_PASSWORD", "DATABASE_URL"):
    if k in os.environ:
        sys.stderr.write("LEAK " + k); sys.exit(3)
if "kg_" not in os.environ.get("HOME", ""):
    sys.stderr.write("HOME không được cô lập"); sys.exit(3)
tok = os.environ.get("KAGGLE_API_TOKEN", "")
if tok.startswith("bad"):
    sys.stderr.write("401 Unauthorized"); sys.exit(1)
print("Authentication required to call the Kaggle API.\n  Option A: Environment variable")
args = sys.argv[1:]
if args[:1] == ["quota"]:
    print(json.dumps([
        {"resource": "GPU", "used": "16.33h", "remaining": "13.67h", "total": "30.00h", "refreshAt": "2026-10-24T00:00:00+00:00"},
        {"resource": "TPU", "used": "0.00h", "remaining": "20.00h", "total": "20.00h", "refreshAt": "2026-10-24T00:00:00+00:00"},
    ], indent=2))
elif args[:2] == ["kernels", "list"]:
    print("ref,title\nme/nb1,Notebook 1")
'''

# Script thu thập session giả: đọc kịch bản từ FAKE_PROBE_SCENARIO ({token: kết quả}) và ghi tùy chọn nhận được
# vào FAKE_PROBE_LOG (mỗi dòng một JSON) để test kiểm tra.
FAKE_PROBE = r'''
import json, os, sys
for k in ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SECRET_KEY", "ADMIN_PASSWORD", "DATABASE_URL"):
    if k in os.environ:
        sys.stderr.write("LEAK " + k); sys.exit(3)
if "kg_" not in os.environ.get("HOME", ""):
    sys.stderr.write("HOME không được cô lập"); sys.exit(3)
opts = json.loads(sys.argv[1])
with open(os.environ["FAKE_PROBE_LOG"], "a") as f:
    f.write(json.dumps({"token": os.environ.get("KAGGLE_API_TOKEN"), "opts": opts}) + "\n")
scenario = json.load(open(os.environ["FAKE_PROBE_SCENARIO"]))
key = os.environ.get("KAGGLE_API_TOKEN", "") + (":submissions" if opts.get("mode") == "submissions" else "")
res = scenario.get(key)
print("Authentication required to call the Kaggle API.")  # nhiễu như CLI thật
if res is None:
    sys.stderr.write("không có kịch bản"); sys.exit(1)
print("@@KAM@@" + json.dumps(res))
'''


def reset_database(pg_url, apply_schema=True):
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for role, attr in (("anon", ""), ("authenticated", ""), ("service_role", "BYPASSRLS")):
            c.execute(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') "
                f"THEN CREATE ROLE {role} NOLOGIN {attr}; END IF; END $$;"
            )
        c.execute("GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role")
        if apply_schema:
            c.execute(SCHEMA_SQL)


def login(c, pw="pw123"):
    return c.post("/api/login", json={"password": pw})


def add(c, name, token="token-aaaaaaaa"):
    return c.post("/api/accounts", json={"name": name, "token": token})
