# WeChat Intelligence

Ứng dụng FastAPI tải bài viết WeChat từ link `https://mp.weixin.qq.com/s/...`, hiển thị kết luận và số container xuất khẩu qua hải quan đường bộ Trung Quốc theo ngày. Với bài tiếng Trung, ứng dụng có thể dùng OpenAI API để tạo kết luận và báo cáo tiếng Việt.

## Chạy trên Ubuntu bằng systemd (cổng 3435)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip python3-tk
mkdir -p ~/apps && cd ~/apps
git clone --recurse-submodules https://github.com/phamdungtk/wechat-intelligence.git
cd wechat-intelligence
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sudo bash deploy/install-service.sh
```

Script cài service `wechat-intelligence` cho người dùng đang chạy `sudo`, dùng thư mục dự án hiện tại và khởi động Uvicorn trên `0.0.0.0:3435`. Service tự khởi động cùng Ubuntu và tự chạy lại khi lỗi. Kiểm tra bằng `sudo systemctl status wechat-intelligence`; xem log bằng `sudo journalctl -u wechat-intelligence -f`. Nếu UFW đang bật và cần truy cập từ máy khác, chạy `sudo ufw allow 3435/tcp` rồi mở `http://IP_MAY_CHU:3435/`.

Sau khi cập nhật mã nguồn, chạy lại `sudo bash deploy/install-service.sh` để áp dụng cấu hình và khởi động lại service. Dữ liệu bài viết trong `storage/` được giữ nguyên.

### WeChat Desktop trên Ubuntu

Phiên WeChat Linux chạy riêng trong Docker, không thay đổi service API cổng 3435. Khởi động bằng:

```bash
cd ~/apps/wechat-intelligence
docker compose -f deploy/wechat-desktop.compose.yml up -d
docker compose -f deploy/wechat-desktop.compose.yml ps
```

Giao diện chỉ nghe trên `127.0.0.1:3436` của server. Từ máy cá nhân, mở một terminal và giữ lệnh SSH tunnel này chạy:

```bash
ssh -N -L 3436:127.0.0.1:3436 root@IP_MAY_CHU
```

Sau đó mở `https://127.0.0.1:3436/` trên máy cá nhân, chấp nhận chứng chỉ tự ký của giao diện nội bộ và quét QR bằng WeChat trên điện thoại. Dữ liệu phiên được giữ tại `storage/wechat_desktop/config/`; không đưa thư mục này lên Git. Cổng 3436 không được mở trực tiếp ra Internet.

Khi đã đăng nhập, có thể bật dịch vụ đọc link được sao chép trong WeChat Desktop:

```bash
sudo bash deploy/install-desktop-collector.sh
sudo journalctl -u wechat-intelligence-collector -f
```

Mỗi link bài WeChat mới trong clipboard của phiên desktop sẽ được tải qua luồng hiện có, lưu thành một bài riêng và xuất hiện trong dashboard. Dịch vụ bỏ qua URL đã có trong kho bài viết.

### Bật báo cáo tiếng Việt cho bài tiếng Trung

Tạo OpenAI API key theo [hướng dẫn chính thức](https://developers.openai.com/api/docs/quickstart). Trên Ubuntu, mở `~/apps/wechat-intelligence/.env`, thêm `OPENAI_API_KEY=...` và có thể đặt `OPENAI_REPORT_MODEL=gpt-4.1-mini`, rồi chạy `sudo systemctl restart wechat-intelligence`. Giữ `.env` trên máy chủ; tệp này đã được Git bỏ qua. Không gửi API key qua chat hoặc đưa vào Git.

Sau khi có key, bài tiếng Trung mới tải hoặc cập nhật sẽ tự tạo báo cáo tiếng Việt. Với bài đã lưu, mở trang chi tiết và chọn **Tạo báo cáo tiếng Việt**; không cần tải lại bài từ WeChat. Báo cáo được lưu trong thư mục của từng bài, còn dashboard tiếp tục lấy số liệu trực tiếp từ `article.md` gốc. Khi chưa cấu hình key hoặc OpenAI API lỗi, bài gốc vẫn được lưu và trang hiển thị lý do chưa có báo cáo.

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

Tệp `.env` chỉ cần khi dùng lịch sử tài khoản hoặc tạo báo cáo tiếng Việt qua OpenAI API; luồng nhập một link bài viết vẫn tải được khi chưa có key.

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mở [http://127.0.0.1:8000/](http://127.0.0.1:8000/), dán link bài viết WeChat, rồi chọn **Lấy bài viết**. Mỗi lần tải được lưu trong thư mục riêng dưới `storage/raw/`; các bài cũ không bị xóa. Mở [Bài viết đã lưu](http://127.0.0.1:8000/articles) để tìm và xem chi tiết từng bài. Trang chi tiết hiển thị kết luận có trong bài, toàn văn và dashboard số container Việt Nam, Thái Lan theo ngày. Thêm bài có bảng hải quan để bổ sung ngày vào dashboard. Dừng máy chủ bằng `Ctrl+C`.

Nút **Cập nhật bài viết** ở danh sách và trang chi tiết tải lại URL gốc của đúng bài đã lưu, giữ nguyên mã bài viết và lưu bản trước trong `storage/versions/`. Dashboard không dùng AI hay nguồn dữ liệu riêng: mỗi lần mở trang, ứng dụng đọc các tệp `storage/raw/.../article.md`, tìm bảng hải quan đường bộ có ngày và số container Việt Nam/Thái Lan. Mỗi dòng dashboard có link tới bài đã lưu và trích đoạn số liệu gốc.

Nút **Xóa bài viết** ở danh sách và trang chi tiết yêu cầu xác nhận, sau đó bỏ đúng bài được chọn khỏi danh sách và dashboard. Máy chủ chuyển bản gốc vào `storage/trash/` để có thể khôi phục thủ công nếu xóa nhầm.

Khi tải hoặc cập nhật bài, trang hiển thị nhật ký từng bước và lỗi nếu có. Trên Ubuntu có thể xem chi tiết lỗi máy chủ bằng `sudo journalctl -u wechat-intelligence -f`.

Nếu PowerShell chặn lệnh kích hoạt môi trường ảo, chạy trực tiếp bằng `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` và `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.

## Tùy chọn

- Thử tải một bài mà không cần giao diện: `python poc.py "https://mp.weixin.qq.com/s/ARTICLE_ID"`. Kết quả nằm trong `storage/raw/`.
- PostgreSQL: `docker compose up -d postgres` khởi động cơ sở dữ liệu theo `docker-compose.yml`. Luồng tải bài và báo cáo hiện lưu vào `storage/raw/`, nên không cần PostgreSQL để chạy giao diện. `requirements-db.txt` dành cho phần tích hợp cơ sở dữ liệu tùy chọn.

## Khi gặp lỗi

- Không tải được bài viết: kiểm tra link vẫn mở được trên trình duyệt. Một số bài có thể bị WeChat chặn hoặc yêu cầu xác minh.
