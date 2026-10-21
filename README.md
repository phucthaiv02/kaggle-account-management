# Kaggle Account Management

Trang web quản lý nhiều tài khoản Kaggle: lưu token đã mã hóa, xem hạn mức GPU/TPU còn lại và thời điểm reset của từng tài khoản, xem danh sách notebook và lịch sử quota. Chạy trên **Vercel** với database **Supabase (Postgres)**.

## Cách hoạt động

- Backend FastAPI chạy thành một Vercel Function. Với mỗi tài khoản, nó chạy `python -m kaggle quota --format json` trong một thư mục HOME tạm riêng, token truyền qua biến `KAGGLE_API_TOKEN` của tiến trình con, nên token không lẫn nhau và bí mật của ứng dụng không bị lộ cho tiến trình con.
- Token được mã hóa Fernet (khóa `SECRET_KEY`) trước khi ghi vào Supabase. Trình duyệt không bao giờ nhận lại token.
- **Không có cron.** Quota được làm mới khi bạn bấm **Làm mới** (từng tài khoản) hoặc **Làm mới tất cả**. Nút "tất cả" xử lý từ tài khoản cũ nhất và dừng khi hết `REFRESH_BUDGET_SECONDS` (mặc định 50s); nếu còn sót, giao diện báo để bạn bấm lại.
- Đăng nhập bằng một mật khẩu quản trị. Phiên là cookie có chữ ký (không lưu trên server), đổi `ADMIN_PASSWORD` sẽ đăng xuất mọi phiên.

## Triển khai

**1. Supabase**
Tạo project, vào *Connect* và copy chuỗi **Transaction pooler** (cổng 6543):
`postgresql://postgres.<ref>:<mật-khẩu>@aws-0-<region>.pooler.supabase.com:6543/postgres`

Không cần chạy SQL thủ công: bảng được tự tạo ở request đầu tiên, và **Row Level Security được bật** để khóa API công khai của Supabase (anon key) không đọc được bảng chứa token. Ứng dụng kết nối trực tiếp bằng `DATABASE_URL`, không dùng Supabase REST/anon/service-role key.

**2. Tạo khóa mã hóa**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**3. Vercel**
Import repo từ GitHub (Add New → Project). Framework tự nhận FastAPI; điểm vào được khai báo trong `pyproject.toml` (`backend.app.main:app`). Thêm biến môi trường (Settings → Environment Variables):

| Biến | Giá trị |
|---|---|
| `DATABASE_URL` | Chuỗi Transaction pooler ở bước 1 |
| `SECRET_KEY` | Khóa ở bước 2 |
| `ADMIN_PASSWORD` | Mật khẩu đăng nhập trang |

Deploy xong, mở `/api/health` để kiểm tra kết nối database (trả `{"ok": true}`), rồi mở trang chủ, đăng nhập và thêm tài khoản.

## Thêm tài khoản mà không cần deploy lại

Tài khoản nằm trong database, thêm/sửa/xóa qua giao diện, không đụng đến code hay biến môi trường. Chỉ cần ba biến ở trên được đặt một lần.

- **Không bao giờ đổi `SECRET_KEY`** sau khi đã thêm tài khoản, nếu không sẽ không giải mã được token đã lưu. Hãy sao lưu khóa này.
- Lấy token cho từng tài khoản: Kaggle → Settings → API → tạo token mới, dán vào ô "Access token". Khóa API kiểu cũ (username + key) vẫn dùng được nếu điền thêm username.

## Chạy cục bộ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # điền DATABASE_URL, SECRET_KEY, ADMIN_PASSWORD
set -a; source .env; set +a
uvicorn backend.app.main:app --reload
```

Mở http://localhost:8000. Chạy kiểm thử (dùng Postgres cục bộ tự khởi động, không cần Supabase): `python -m pytest backend/tests`.

## Giới hạn và lưu ý

- **`maxDuration`**: `vercel.json` đặt 60 giây cho function; `REFRESH_BUDGET_SECONDS` phải nhỏ hơn giá trị này. Nếu gói Vercel của bạn giới hạn thấp hơn, hạ cả hai và bấm "Làm mới tất cả" nhiều lần khi có nhiều tài khoản.
- Mỗi lần làm mới ghi một snapshot; snapshot cũ hơn 90 ngày tự bị xóa.
- Đăng nhập sai quá 5 lần/IP hoặc 30 lần tổng trong 15 phút sẽ bị khóa tạm thời.
- "Đăng xuất" chỉ xóa cookie trên trình duyệt hiện tại; muốn hủy mọi phiên thì đổi `ADMIN_PASSWORD`.
- Kaggle quy định mỗi người chỉ nên dùng một tài khoản, nhất là trong các cuộc thi. Chỉ quản lý các tài khoản thuộc về bạn hoặc tổ chức của bạn, không dùng công cụ này để lách giới hạn quota.

## API

| Method | Đường dẫn | Mô tả |
|---|---|---|
| GET | `/api/health` | Kiểm tra kết nối database |
| POST | `/api/login`, `/api/logout` | Đăng nhập / đăng xuất |
| GET | `/api/accounts` | Danh sách kèm quota mới nhất |
| POST | `/api/accounts` | Thêm tài khoản (kiểm tra token ngay) |
| PATCH | `/api/accounts/{id}` | Sửa tên, ghi chú, token |
| DELETE | `/api/accounts/{id}` | Xóa |
| POST | `/api/accounts/{id}/refresh` | Làm mới quota một tài khoản |
| POST | `/api/refresh-all` | Làm mới tất cả (có giới hạn thời gian) |
| GET | `/api/accounts/{id}/history` | Lịch sử quota |
| GET | `/api/accounts/{id}/kernels` | Notebook của tài khoản |
