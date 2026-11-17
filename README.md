# Studio AI Messenger — Hướng dẫn vận hành

Hệ thống trợ lý AI tư vấn khách hàng trên Facebook Messenger (sử dụng FastAPI + SQLite + Gemini API).

---

## 1. Khởi động & Tắt Server (Python / FastAPI)

### Cách 1: Chạy bằng PowerShell (khuyên dùng khi tự theo dõi log)
Mở PowerShell tại thư mục `e:\Hoc AI` và chạy:

```powershell
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

* Mở trình duyệt vào: `http://localhost:8000`
* **Tắt server:** Bấm vào cửa sổ PowerShell đó và nhấn tổ hợp phím **`Ctrl + C`**.

### Cách 2: Chạy ngầm & Tắt nhanh từ PowerShell
* **Chạy ngầm:**
  ```powershell
  Start-Process python -ArgumentList "-m uvicorn app:app --host 0.0.0.0 --port 8000" -WindowStyle Hidden
  ```
* **Tắt tất cả server đang chạy cổng 8000:**
  ```powershell
  Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
  ```

---

## 2. Mở link HTTPS công khai bằng Cloudflared

Facebook Messenger bắt buộc Webhook phải dùng link HTTPS công khai.

### Cài đặt (chỉ cần làm 1 lần nếu máy chưa có)
```powershell
winget install Cloudflare.cloudflared
```
*(Sau khi cài xong, tắt PowerShell mở lại để nhận lệnh `cloudflared`).*

### Chạy lấy link HTTPS
Mở một cửa sổ PowerShell **khác** (giữ nguyên cửa sổ server đang chạy) và gõ:

```powershell
cloudflared tunnel --url http://localhost:8000
```

Trong kết quả hiện ra, tìm dòng có dạng:
`https://xxxx-xxxx-xxxx.trycloudflare.com`

Dán vào **Meta for Developers -> Cài đặt API Messenger -> Định cấu hình webhook**:
* **Callback URL:** `https://xxxx-xxxx-xxxx.trycloudflare.com/webhook`
* **Verify Token:** giá trị `FB_VERIFY_TOKEN` trong file `.env` (mặc định: `studio_cuoi_verify_123`)

---

## 3. Tắt/mở máy hoặc tắt/mở server có phải làm lại link Cloudflared không?

* **Nếu chỉ tắt/bật lại server Python (`uvicorn`)** nhưng **vẫn giữ cửa sổ `cloudflared` đang chạy**: **KHÔNG cần** làm lại link, link cũ vẫn dùng bình thường.
* **Nếu tắt cửa sổ `cloudflared` hoặc tắt/khởi động lại máy tính**:
  * Với lệnh chạy nhanh (`cloudflared tunnel --url ...`), mỗi lần chạy lại Cloudflare sẽ cấp **một link ngẫu nhiên mới** -> bạn **PHẢI** copy link mới dán lại vào Callback URL trên Meta for Developers.

### Cách khắc phục để dùng cố định lâu dài (không bao giờ phải đổi link):
1. **Dùng Named Tunnel của Cloudflare (miễn phí tunnel, chỉ cần có 1 tên miền trỏ qua Cloudflare):** Gắn cố định 1 subdomain như `https://bot.tenstudio.com/webhook` trỏ về máy bạn. Dù tắt/mở lại máy, link vẫn giữ nguyên.
2. **Đưa lên Server / VPS chạy 24/7 (Render, Railway, hoặc VPS):** Nên làm khi đưa vào vận hành chính thức, giúp hệ thống nhận tin nhắn từ khách **ngay cả khi bạn tắt máy tính ở nhà**.
