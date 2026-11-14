# Together Trading Bot — Bot tín hiệu Binance USDT-M Futures

Bot quét **tín hiệu** theo chiến lược **2 Đáy/2 Đỉnh (cụm nến) → Gãy neckline → EMA20/50 + Momentum (v4, preset Active)**
trên các khung **5m, 15m, 1h, 4h, 1d**, theo dõi **lệnh ảo** và xuất dữ liệu cho website Together We Thrive
(https://togetherwethrive.vercel.app).

> ⚠️ **Chỉ là tín hiệu, lệnh ảo, chưa kiểm chứng hiệu quả.** Bot **không đặt lệnh thật**, không dùng API key Binance.
> Không phải khuyến nghị đầu tư.

## Cách hoạt động
- **Danh sách mã**: hợp đồng USDT vĩnh cửu (PERPETUAL) đang `TRADING`, khối lượng 24h (quoteVolume) **≥ 1.000.000 USDT**.
  Làm mới mỗi giờ (`/fapi/v1/exchangeInfo` + `/fapi/v1/ticker/24hr`), tự thêm/bớt mã.
- **Realtime bằng WebSocket**: luồng `<symbol>@kline_<tf>` qua `wss://fstream.binance.com/market/stream`
  (Binance đã chuyển luồng nến USDT-M sang đường dẫn `/market`), tối đa 200 luồng/kết nối, nhiều kết nối song song,
  tự kết nối lại (backoff), tự trả lời ping.
- **REST tối thiểu**: chỉ dùng để nạp lịch sử lúc khởi động (499 nến/khung, weight 2), bù nến thiếu khi mất kết nối
  và làm mới danh sách mã. Bộ giới hạn weight **≤ 600/phút** (giới hạn của Binance là 2400), tự dừng khi máy chủ báo
  weight cao, và **tôn trọng `Retry-After`** khi gặp HTTP 429/418 (thời gian khóa được lưu lại, khởi động lại vẫn chờ).
- **Chỉ xử lý nến đã đóng** (`k.x = true`) → không repaint. Nến và lệnh ảo lưu trong SQLite (`data/bot.sqlite`),
  khởi động lại chỉ cần bù phần nến còn thiếu.

## Logic tín hiệu (đồng bộ với Pine v4 chính thức)
Chi tiết từng bước Pine ↔ code, vòng đời lệnh ảo, xuất bản dữ liệu và các khác biệt còn lại: **[docs/LOGIC.md](docs/LOGIC.md)**.

Tóm tắt (preset Active, chỉ nến đã đóng): pivot 5/5 (luật hòa TradingView) + cụm ≥ 3 nến trong 0,5×ATR hoặc spike
≥ 1×ATR → 2 đáy/2 đỉnh (cách 5–100 nến, hồi ≥ 55 %, cao ≥ 1,6×ATR, dung sai 0,75×ATR, chất lượng ≥ 0,40) → gãy
neckline ≥ 0,15×ATR với volume ≥ 1,15×SMA20 (≤ 50 nến) → EMA20/50 đúng chiều, khoảng cách ≥ 0,30×ATR, momentum ≥ 2/3,
ADX14 ≥ 20, **HTF 4h cho mọi khung** (close nến 4h đã xác nhận so với EMA50 4h), cooldown 5 nến từ nến đóng lệnh,
1 lệnh/mã/khung. Lệnh ảo: vào giá đóng, SL mô hình siết ≤ 3×ATR, TP 1,5R, +1R → hòa vốn + trailing 1,3×ATR, khớp
SL/TP theo broker emulator TradingView (gap ở giá mở, đường đi O-H-L-C/O-L-H-C), phí 0,04 %/chiều.

## Chạy
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest          # kiểm thử
./scripts/install_site.sh /workspace/ultimate/frontend   # cài giao diện web
./scripts/run.sh                    # chạy bot (log: data/logs/bot.log, trạng thái: data/status.json)
```
Biến môi trường tùy chọn: `TBOT_MIN_QVOL`, `TBOT_WEIGHT_BUDGET`, `TBOT_DATA`, `TBOT_PUSH` (0 = không đẩy GitHub),
`TBOT_PARAMS` (JSON ghi đè tham số, ví dụ `{"min_quality":0.5}`); `TBOT_SITE` chỉ dùng cho `scripts/install_site.sh`.

## Website
`web/` là dashboard tĩnh (tiếng Việt, giờ UTC+7): Tín hiệu mới, Lệnh đang mở, Lệnh đã đóng, Thống kê (tỷ lệ thắng
theo khung/chiều), Trạng thái bot; lọc theo khung/chiều/mã, bảng sắp xếp được, tự chuyển thành thẻ trên điện thoại.

**Dữ liệu live qua GitHub, không tốn AI, không cần deploy lại web:** bot (Python thuần) tự commit `bot.json` lên nhánh
`data` của repo này mỗi 2 phút khi tín hiệu/lệnh/thống kê thay đổi, và mỗi 10 phút để cập nhật trạng thái. Dashboard
đọc trực tiếp `https://raw.githubusercontent.com/lechinh911/together-trading-bot/data/bot.json` và tự tải lại mỗi 60 giây.
Token GitHub chỉ nằm trong biến môi trường `GITHUB_TOKEN_TOGETHER`, git đọc qua `scripts/git_askpass.sh`
(không ghi vào file/log).

## Cấu trúc
| file | vai trò |
|---|---|
| `tbot/engine.py` | pivot, cụm/spike, 2 đáy/2 đỉnh, gãy/hủy, tín hiệu (Pine bước 2–6) |
| `tbot/strategy.py` | một "chiến lược Pine" cho mỗi mã+khung: thứ tự broker → script, 1 vị thế, cooldown |
| `tbot/trades.py` | lệnh ảo: SL/TP, khớp lệnh kiểu TradingView, hòa vốn + trailing, PnL |
| `tbot/htf.py` | HTF 4h (`request.security("240", lookahead_off)`) |
| `tbot/indicators.py` | EMA/RMA/ATR/SMA/DMI/RSI kiểu Pine |
| `tbot/bot.py` | asyncio: danh sách mã, nạp/bù nến, WebSocket, lệnh công khai/bóng |
| `tbot/rest.py`, `tbot/ratelimit.py` | REST công khai + giới hạn weight/back-off |
| `tbot/universe.py`, `tbot/store.py` | lọc mã, SQLite |
| `tbot/publisher.py`, `tbot/gitpush.py` | bot.json/status.json, đẩy nhánh `data` |
| `web/` | dashboard |
| `tests/` | 36 test, gồm parity với mô phỏng Pine tham chiếu và ca SIREN/WOO |
