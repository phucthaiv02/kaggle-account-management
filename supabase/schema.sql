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

-- ============ Theo dõi session (notebook run) ============
-- Mỗi hàng = một phiên chạy (một phiên bản của một notebook) của một tài khoản Kaggle.
alter table public.accounts add column if not exists sessions_checked_at timestamptz;
alter table public.accounts add column if not exists sessions_error text;

create table if not exists public.kernel_sessions (
    id              bigserial primary key,
    account_id      bigint not null references public.accounts(id) on delete cascade,
    ref             text not null,                      -- owner/slug của notebook
    version         integer not null default 0,         -- số phiên bản (mỗi lần chạy là một phiên bản)
    title           text,
    author          text,
    status          text not null default 'unknown',    -- queued|running|complete|error|cancel_requested|cancel_acknowledged|new_script|unknown
    failure_message text,
    last_run_time   timestamptz,                        -- thời điểm chạy gần nhất do Kaggle báo (dùng làm mốc bắt đầu)
    duration_seconds double precision,                  -- ước tính từ log; -1 = đã thử nhưng không xác định được
    accelerator     text not null default 'cpu',        -- cpu | gpu | tpu | unknown (chưa xác minh)
    machine_shape   text,
    docker_image    text,
    enable_internet boolean,
    is_private      boolean,
    kernel_type     text,
    language        text,
    total_votes     integer,
    competitions    jsonb not null default '[]'::jsonb, -- competition gắn qua data source
    datasets        jsonb not null default '[]'::jsonb,
    models          jsonb not null default '[]'::jsonb,
    kernel_sources  jsonb not null default '[]'::jsonb,
    first_seen_at   timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    status_changed_at timestamptz,
    unique (account_id, ref, version)
);
-- true = loại máy (cpu/gpu/tpu) đã được xác minh bằng metadata của đúng phiên bản
alter table public.kernel_sessions add column if not exists accelerator_checked boolean not null default false;
create index if not exists idx_ks_acc_run on public.kernel_sessions(account_id, last_run_time desc);
create index if not exists idx_ks_status on public.kernel_sessions(status);

alter table public.kernel_sessions enable row level security;
revoke all on public.kernel_sessions from anon, authenticated;
revoke all on sequence public.kernel_sessions_id_seq from anon, authenticated;
grant select, insert, update, delete on public.kernel_sessions to service_role;
grant usage, select on sequence public.kernel_sessions_id_seq to service_role;

-- ---------- Submissions trên các competition ----------
alter table public.accounts add column if not exists subs_checked_at timestamptz;
alter table public.accounts add column if not exists subs_error text;

create table if not exists public.competition_entries (
    id              bigserial primary key,
    account_id      bigint not null references public.accounts(id) on delete cascade,
    slug            text not null,                      -- tên ngắn của competition
    title           text,
    deadline        timestamptz,
    category        text,
    reward          text,
    team_count      integer,
    user_rank       integer,
    max_daily_submissions integer,
    evaluation_metric text,
    url             text,
    active          boolean not null default true,      -- còn hạn nộp / đang chấm
    submissions_disabled boolean not null default false,
    submissions_fetched boolean not null default false, -- đã lấy được danh sách submission ở lần quét gần nhất
    updated_at      timestamptz not null default now(),
    unique (account_id, slug)
);
create index if not exists idx_ce_active on public.competition_entries(active, deadline);

create table if not exists public.submissions (
    id              bigserial primary key,
    account_id      bigint not null references public.accounts(id) on delete cascade,
    competition     text not null,
    ref             bigint not null,                    -- mã submission do Kaggle cấp
    submitted_at    timestamptz,
    description     text,
    file_name       text,
    status          text not null default 'unknown',    -- pending | complete | error | unknown
    public_score    text,                               -- Kaggle trả dạng chuỗi
    private_score   text,
    error_description text,
    team_name       text,
    submitted_by    text,
    url             text,
    total_bytes     bigint,
    first_seen_at   timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (account_id, competition, ref)
);
create index if not exists idx_sub_date on public.submissions(account_id, submitted_at desc);
create index if not exists idx_sub_status on public.submissions(status);

alter table public.competition_entries enable row level security;
alter table public.submissions enable row level security;
revoke all on public.competition_entries, public.submissions from anon, authenticated;
revoke all on sequence public.competition_entries_id_seq, public.submissions_id_seq from anon, authenticated;
grant select, insert, update, delete on public.competition_entries, public.submissions to service_role;
grant usage, select on sequence public.competition_entries_id_seq, public.submissions_id_seq to service_role;

-- Báo cho Data API nạp lại danh sách bảng
notify pgrst, 'reload schema';
