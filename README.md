# Kaggle Account Management

Trang web quản lý nhiều tài khoản Kaggle: lưu token đã mã hóa, theo dõi hạn mức GPU/TPU còn lại của từng tài khoản, xem danh sách notebook và lịch sử quota.

## Cách hoạt động

- Backend FastAPI gọi **Kaggle CLI 2.x** (`kaggle quota`, `kaggle kernels list --mine`) cho từng tài khoản, mỗi lần trong một thư mục HOME tạm riêng nên token không lẫn nhau.
- Token được mã hóa bằng Fernet (khóa `SECRET_KEY`) trước khi lưu vào SQLite. Frontend không bao giờ nhận lại token.
- Job nền làm mới quota mỗi `REFRESH_MINUTES` phút; mỗi lần đo được lưu thành một snapshot để xem lịch sử.
- Đăng nhập bằng một mật khẩu quản trị (`ADMIN_PASSWORD`), phiên lưu bằng cookie HttpOnly.

## Chạy thử

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd ..
cp .env.example .env   # điền SECRET_KEY và ADMIN_PASSWORD
set -a; source .env; set +a
uvicorn backend.app.main:app --reload
```

Mở http://localhost:8000.

### Docker

```bash
docker build -t kaggle-account-management .
docker run -p 8000:8000 -v kam-data:/data --env-file .env kaggle-account-management
```

## Lấy token cho từng tài khoản

Kaggle → Settings → API → tạo token mới (kiểu access token của CLI 2.x). Dán vào ô "Access token" khi thêm tài khoản. Với khóa API kiểu cũ, điền thêm username.

## Lưu ý

- **Định dạng đầu ra của `kaggle quota`** chưa có tài liệu chính thức; bộ đọc ở `backend/app/kaggle_client.py` (`parse_quota`) dựa trên định dạng quan sát được (`GPU 16.33h used, 13.67h remaining`). Nếu Kaggle đổi định dạng, chỉ cần sửa biểu thức chính quy ở đó. Lỗi đọc sẽ hiển thị trên thẻ tài khoản kèm đầu ra gốc.
- Chạy sau HTTPS và đặt `COOKIE_SECURE=1` khi triển khai thật. Ứng dụng dành cho một quản trị viên, phiên lưu trong bộ nhớ nên chạy một tiến trình.
- Kaggle quy định mỗi người chỉ nên dùng một tài khoản, nhất là trong các cuộc thi. Chỉ quản lý các tài khoản thuộc về bạn hoặc tổ chức của bạn, và không dùng công cụ này để lách giới hạn quota.

## API

| Method | Đường dẫn | Mô tả |
|---|---|---|
| POST | `/api/login`, `/api/logout` | Đăng nhập / đăng xuất |
| GET | `/api/accounts` | Danh sách kèm quota mới nhất |
| POST | `/api/accounts` | Thêm tài khoản (kiểm tra token ngay) |
| PATCH | `/api/accounts/{id}` | Sửa tên, ghi chú, token |
| DELETE | `/api/accounts/{id}` | Xóa |
| POST | `/api/accounts/{id}/refresh`, `/api/refresh-all` | Làm mới quota |
| GET | `/api/accounts/{id}/history` | Lịch sử quota |
| GET | `/api/accounts/{id}/kernels` | Notebook của tài khoản |
