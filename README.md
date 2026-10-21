# Kaggle Account Management

Trang web quản lý nhiều tài khoản Kaggle: lưu token đã mã hóa, xem hạn mức GPU/TPU còn lại và thời điểm reset của từng tài khoản, xem danh sách notebook và lịch sử quota. Chạy trên **Vercel**, dữ liệu lưu ở **Supabase** qua URL + khóa API (không cần mật khẩu database).

## Cách hoạt động

- Backend FastAPI chạy thành một Vercel Function. Với mỗi tài khoản, nó chạy `python -m kaggle quota --format json` trong một thư mục HOME tạm riêng, token truyền qua biến `KAGGLE_API_TOKEN` của tiến trình con, nên token không lẫn nhau và bí mật của ứng dụng không bị lộ cho tiến trình con.
- Dữ liệu lưu ở Supabase qua Data API (REST) bằng `SUPABASE_URL` + khóa secret. Token Kaggle được mã hóa Fernet (khóa `SECRET_KEY`) trước khi ghi; trình duyệt không bao giờ nhận lại token và API danh sách không hề truy vấn cột token.
- **Không có cron.** Quota được làm mới khi bạn bấm **Làm mới** (từng tài khoản) hoặc **Làm mới tất cả**. Nút "tất cả" xử lý từ tài khoản cũ nhất và dừng khi hết `REFRESH_BUDGET_SECONDS` (mặc định 50s); nếu còn sót, giao diện báo để bạn bấm lại.
- **Trang Sessions** (`#/sessions`): theo dõi các lần chạy notebook của mọi tài khoản: đang chạy / đã xong / lỗi, tài khoản nào, GPU/TPU/CPU, thời gian đã chạy, challenge (competition) và dataset gắn kèm, link mở notebook, internet bật hay không, số vote, thông báo lỗi... Bấm **Làm mới** để thu thập; dữ liệu lưu ở bảng `kernel_sessions`.
- **Trang Submissions** (`#/submissions`): thống kê submission trên các competition tài khoản đã tham gia và **còn hạn** (đang diễn ra hoặc đang chờ chấm): số competition, tổng submission, số đang chấm, lỗi, số lần nộp hôm nay (tính theo ngày UTC, so với giới hạn nộp mỗi ngày), điểm public gần nhất, hạng, hạn chót còn bao lâu; bên dưới là danh sách submission có lọc theo trạng thái, tài khoản, competition. Điểm private thường chỉ có sau khi competition kết thúc. Trang không xếp hạng điểm vì Kaggle không cho biết điểm cao hay thấp là tốt hơn. Competition nào hết hạn sẽ tự ẩn khỏi trang ở lần quét kế tiếp (dữ liệu cũ vẫn lưu trong database).
- **Phân trang**: ba bảng (danh sách Sessions, bảng theo competition và danh sách Submissions) chia trang ở trình duyệt, 10/25/50/100 dòng mỗi trang (nhớ lựa chọn), tự về trang 1 khi đổi bộ lọc. Số liệu tổng ở các ô trên cùng tính trên toàn bộ dữ liệu đã lọc, không chỉ trang hiện tại. Backend trả tối đa 1000 session và 5000 submission mỗi lần.
- Đăng nhập bằng một mật khẩu quản trị. Phiên là cookie có chữ ký (không lưu trên server), đổi `ADMIN_PASSWORD` sẽ đăng xuất mọi phiên.

## Triển khai

**1. Supabase: tạo bảng (một lần)**
Data API không chạy được lệnh tạo bảng, nên bạn chạy tay một lần: Supabase Dashboard → **SQL Editor** → New query → dán toàn bộ nội dung [`supabase/schema.sql`](supabase/schema.sql) → Run.

File này tạo 3 bảng, bật Row Level Security, thu hồi quyền của `anon`/`authenticated`, và cấp quyền riêng cho `service_role` (Supabase đang thôi tự cấp quyền cho bảng mới, nên phần `GRANT` là bắt buộc). Chạy lại nhiều lần vẫn an toàn.

**2. Lấy URL và khóa**
Trong Supabase: *Project Settings → API Keys*.
- `SUPABASE_URL`: dạng `https://<project-ref>.supabase.co`.
- `SUPABASE_SECRET_KEY`: khóa **secret** (`sb_secret_...`) hoặc khóa `service_role` kiểu cũ. **Không dùng khóa anon/publishable**: bảng đã khóa với khóa công khai nên sẽ không đọc/ghi được gì.

Khóa secret có toàn quyền với database. Chỉ đặt trong biến môi trường của Vercel; không đưa vào trình duyệt hay Git.

**3. Tạo khóa mã hóa**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**4. Vercel**
Import repo từ GitHub (Add New → Project). Framework tự nhận FastAPI; điểm vào được khai báo trong `pyproject.toml` (`backend.app.main:app`). Thêm biến môi trường (Settings → Environment Variables):

| Biến | Giá trị |
|---|---|
| `SUPABASE_URL` | URL project ở bước 2 |
| `SUPABASE_SECRET_KEY` | Khóa secret ở bước 2 |
| `SECRET_KEY` | Khóa ở bước 3 |
| `ADMIN_PASSWORD` | Mật khẩu đăng nhập trang |

Deploy xong, mở `/api/health`: nếu trả `{"ok": true}` là đã kết nối được Supabase. Nếu lỗi, thông báo sẽ chỉ rõ cần làm gì (sai khóa, chưa chạy `schema.sql`, thiếu `GRANT`). Sau đó mở trang chủ, đăng nhập và thêm tài khoản.

## Thêm tài khoản mà không cần deploy lại

Tài khoản nằm trong database, thêm/sửa/xóa qua giao diện, không đụng đến code hay biến môi trường. Chỉ cần bốn biến ở trên được đặt một lần.

- **Không bao giờ đổi `SECRET_KEY`** sau khi đã thêm tài khoản, nếu không sẽ không giải mã được token đã lưu. Hãy sao lưu khóa này.
- Lấy token cho từng tài khoản: Kaggle → Settings → API → tạo token mới, dán vào ô "Access token". Khóa API kiểu cũ (username + key) vẫn dùng được nếu điền thêm username.

## Chạy cục bộ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # điền SUPABASE_URL, SUPABASE_SECRET_KEY, SECRET_KEY, ADMIN_PASSWORD
set -a; source .env; set +a
uvicorn backend.app.main:app --reload
```

Mở http://localhost:8000. Chạy kiểm thử: `python -m pytest backend/tests`. Bộ test tự khởi động một Postgres cục bộ, chạy đúng `supabase/schema.sql` và dùng một bản giả lập Data API tối thiểu (không phải PostgREST thật), nên không cần tài khoản Supabase.

## Giới hạn và lưu ý

- **`maxDuration`**: `vercel.json` đặt 60 giây cho function; `REFRESH_BUDGET_SECONDS` phải nhỏ hơn giá trị này. Nếu gói Vercel của bạn giới hạn thấp hơn, hạ cả hai và bấm "Làm mới tất cả" nhiều lần khi có nhiều tài khoản.
- **Sessions: giới hạn của API Kaggle.** Kaggle không có API công khai liệt kê phiên tương tác (đang mở notebook để sửa). Trang này theo dõi các lần *chạy* (commit/run) của notebook thuộc tài khoản. Thời điểm bắt đầu lấy từ `lastRunTime`; thời gian đã chạy được ước tính từ mốc log cuối nên chỉ gần đúng, và có thể trống nếu log không đọc được. Lần chạy đã kết thúc và không đổi phiên bản sẽ không bị hỏi lại. Giới hạn mỗi lần quét chỉnh bằng `SESSIONS_*` (xem `.env.example`); quét dở vì hết giờ sẽ báo "một phần", bấm lại để tiếp tục.
- **Loại máy (GPU/TPU/CPU)**: dòng trong danh sách của Kaggle có thể thiếu thông tin máy, nên mỗi quét sẽ hỏi lại metadata của từng phiên bản (tối đa `SESSIONS_ACCEL_BUDGET` cái) và chỉ ghi "CPU" khi đã xác minh; chưa xác minh được thì hiện "Chưa rõ" và được kiểm tra tiếp ở lần quét sau.
- **Cập nhật schema**: sau khi nâng cấp, chạy lại toàn bộ `supabase/schema.sql` trong SQL Editor (idempotent) để có bảng `kernel_sessions` và cột mới của `accounts`. Trang Tài khoản vẫn dùng được nếu chưa chạy, riêng trang Sessions sẽ báo 503 kèm hướng dẫn. Bản có tab Submissions thêm 2 bảng (`competition_entries`, `submissions`) và 2 cột của `accounts`; cần chạy lại schema thì tab đó mới dùng được.
- Giao diện chỉ còn nút **Làm mới tất cả** và **Xóa** trên mỗi tài khoản; các endpoint sửa tài khoản, làm mới từng tài khoản, notebook và lịch sử quota vẫn còn ở backend.
- Mỗi lần làm mới ghi một snapshot; snapshot cũ hơn 90 ngày tự bị xóa.
- Đăng nhập sai quá 5 lần/IP hoặc 30 lần tổng trong 15 phút sẽ bị khóa tạm thời.
- "Đăng xuất" chỉ xóa cookie trên trình duyệt hiện tại; muốn hủy mọi phiên thì đổi `ADMIN_PASSWORD`.
- Kaggle quy định mỗi người chỉ nên dùng một tài khoản, nhất là trong các cuộc thi. Chỉ quản lý các tài khoản thuộc về bạn hoặc tổ chức của bạn, không dùng công cụ này để lách giới hạn quota.

## API

| Method | Đường dẫn | Mô tả |
|---|---|---|
| GET | `/api/health` | Kiểm tra kết nối Supabase |
| POST | `/api/login`, `/api/logout` | Đăng nhập / đăng xuất |
| GET | `/api/accounts` | Danh sách kèm quota mới nhất |
| POST | `/api/accounts` | Thêm tài khoản (kiểm tra token ngay) |
| PATCH | `/api/accounts/{id}` | Sửa tên, ghi chú, token |
| DELETE | `/api/accounts/{id}` | Xóa |
| POST | `/api/accounts/{id}/refresh` | Làm mới quota một tài khoản |
| POST | `/api/refresh-all` | Làm mới tất cả (có giới hạn thời gian) |
| GET | `/api/accounts/{id}/history` | Lịch sử quota |
| GET | `/api/accounts/{id}/kernels` | Notebook của tài khoản |
| GET | `/api/sessions` | Các lần chạy notebook + trạng thái quét từng tài khoản |
| POST | `/api/sessions/refresh` | Quét lại sessions (có giới hạn thời gian) |
| GET | `/api/submissions` | Competition còn hạn + submission + trạng thái quét từng tài khoản |
| POST | `/api/submissions/refresh` | Quét lại submission (có giới hạn thời gian) |
