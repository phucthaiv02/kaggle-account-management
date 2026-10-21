-- Chạy MỘT LẦN trong Supabase: Dashboard → SQL Editor → New query → dán toàn bộ file này → Run.
-- Chạy lại nhiều lần vẫn an toàn (idempotent).

create table if not exists public.accounts (
    id           bigserial primary key,
    name         text not null unique,
    username     text,
    token_enc    text not null,          -- token Kaggle đã mã hóa Fernet, không bao giờ là bản rõ
    note         text not null default '',
    created_at   timestamptz not null default now(),
    last_checked timestamptz,
    last_error   text,
    -- quota mới nhất (cập nhật mỗi lần làm mới thành công)
    gpu_used     double precision,
    gpu_total    double precision,
    tpu_used     double precision,
    tpu_total    double precision,
    refresh_at   timestamptz
);

create table if not exists public.quota_snapshots (
    id          bigserial primary key,
    account_id  bigint not null references public.accounts(id) on delete cascade,
    ts          timestamptz not null default now(),
    gpu_used    double precision,
    gpu_total   double precision,
    tpu_used    double precision,
    tpu_total   double precision,
    refresh_at  timestamptz,
    raw         text
);
create index if not exists idx_snap_acc_id on public.quota_snapshots(account_id, id desc);

create table if not exists public.login_fails (
    id  bigserial primary key,
    ip  text not null,
    ts  timestamptz not null default now()
);
create index if not exists idx_login_fails_ip_ts on public.login_fails(ip, ts);

-- Bảo mật: bật RLS (không có policy nào) và thu hồi quyền của các role công khai,
-- để khóa anon/publishable không đọc được bảng chứa token.
alter table public.accounts        enable row level security;
alter table public.quota_snapshots enable row level security;
alter table public.login_fails     enable row level security;

revoke all on public.accounts, public.quota_snapshots, public.login_fails from anon, authenticated;
revoke all on sequence public.accounts_id_seq, public.quota_snapshots_id_seq, public.login_fails_id_seq
    from anon, authenticated;

-- Chỉ service_role (khóa secret của backend) được truy cập qua Data API.
-- Supabase đang thôi tự cấp quyền cho bảng mới, nên phải cấp tường minh ở đây.
grant select, insert, update, delete
    on public.accounts, public.quota_snapshots, public.login_fails to service_role;
grant usage, select
    on sequence public.accounts_id_seq, public.quota_snapshots_id_seq, public.login_fails_id_seq to service_role;

-- Báo cho Data API nạp lại danh sách bảng
notify pgrst, 'reload schema';
