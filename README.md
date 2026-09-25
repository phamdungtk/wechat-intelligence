# WeChat Intelligence

Ứng dụng FastAPI tải một bài viết WeChat từ link `https://mp.weixin.qq.com/s/...`, hiển thị kết luận có sẵn trong bài và số container xuất khẩu qua hải quan đường bộ Trung Quốc theo ngày. Ứng dụng không dùng AI và không dịch bài viết.

## Chạy trên Windows

Yêu cầu: Python 3.10 trở lên, PowerShell và kết nối Internet.

Mở PowerShell tại thư mục dự án và chạy:

```powershell
git clone --recurse-submodules https://github.com/phamdungtk/wechat-intelligence.git
cd wechat-intelligence
```

Nếu đã clone repo mà thiếu thư mục `vendor/wechat-article-downloader-skill`, chạy `git submodule update --init --recursive`.

Sau đó cài phụ thuộc:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Tệp `.env` chỉ cần khi dùng chức năng lịch sử tài khoản; luồng nhập một link bài viết không cần cấu hình thêm.

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mở [http://127.0.0.1:8000/](http://127.0.0.1:8000/), dán link bài viết WeChat, rồi chọn **Lấy bài viết**. Mỗi lần tải được lưu trong thư mục riêng dưới `storage/raw/`; các bài cũ không bị xóa. Mở [Bài viết đã lưu](http://127.0.0.1:8000/articles) để tìm và xem chi tiết từng bài. Trang chi tiết hiển thị kết luận có trong bài, toàn văn và dashboard số container Việt Nam, Thái Lan theo ngày. Dashboard chỉ có ngày 20/09/2026 trong dữ liệu mẫu hiện tại; thêm bài có cùng bảng hải quan để bổ sung ngày. Dừng máy chủ bằng `Ctrl+C`.

Nếu PowerShell chặn lệnh kích hoạt môi trường ảo, chạy trực tiếp bằng `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` và `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.

## Tùy chọn

- Thử tải một bài mà không cần giao diện: `python poc.py "https://mp.weixin.qq.com/s/ARTICLE_ID"`. Kết quả nằm trong `storage/raw/`.
- PostgreSQL: `docker compose up -d postgres` khởi động cơ sở dữ liệu theo `docker-compose.yml`. Luồng tải bài và báo cáo hiện lưu vào `storage/raw/`, nên không cần PostgreSQL để chạy giao diện. `requirements-db.txt` dành cho phần tích hợp cơ sở dữ liệu tùy chọn.

## Khi gặp lỗi

- Không tải được bài viết: kiểm tra link vẫn mở được trên trình duyệt. Một số bài có thể bị WeChat chặn hoặc yêu cầu xác minh.
