# Together Trading Bot — Bot tín hiệu Binance USDT-M Futures

Bot quét **tín hiệu** theo chiến lược **2 Đáy/2 Đỉnh (cụm nến) → Gãy neckline → EMA20/50 + Momentum (v4, preset Active)**
trên các khung **5m, 15m, 1h, 4h, 1d**, theo dõi **lệnh ảo** và xuất dữ liệu cho website Market Lens
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

## Logic tín hiệu (bám sát Pine script)
- Pivot 5/5, ATR14 (RMA). Cụm đỉnh/đáy: ≥ 3 nến trong 0,5×ATR, hoặc spike nhô ≥ 1×ATR.
- 2 đáy: đáy 2 không thủng đáy 1 quá 0,75×ATR, cách 5–100 nến, hồi ≥ 55%, chiều cao ≥ 1,6×ATR; neckline = đỉnh cao
  nhất giữa 2 đáy; SL mô hình = đáy thấp hơn. 2 đỉnh đối xứng. Quét lại (k2) các nến chờ xác nhận pivot.
- **Chất lượng** = 0,4×đối xứng + 0,3×khoảng cách + 0,3×chiều cao ≥ 0,40. Mô hình ngược chiều hủy setup đang chờ.
- **Gãy**: nến đóng vượt neckline ≥ 0,15×ATR với khối lượng ≥ 1,15×SMA20.
- **Tín hiệu**: EMA20/EMA50 đúng chiều, khoảng cách ≥ 0,30×ATR (bắt buộc), điểm momentum ≥ 2/3, ADX14 ≥ 20,
  xu hướng khung lớn (giá đóng nến đã đóng gần nhất so với EMA50: 5m→1h, 15m→4h, 1h→4h, 4h→1D, 1D→1W),
  nghỉ 5 nến sau khi đóng lệnh, mỗi mã + khung tối đa 1 lệnh.
- **Lệnh ảo**: vào tại giá đóng nến tín hiệu; SL = đáy/đỉnh mô hình, tối đa 3×ATR; TP = 1,5R; chạm +1R → dời SL về
  hòa vốn và trailing `close − 1,3×ATR`. Nếu SL và TP cùng chạm trong 1 nến → giả định SL trước. Phí 0,04%/chiều.

## Chạy
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest          # kiểm thử
./scripts/install_site.sh /workspace/ultimate/frontend   # cài giao diện web
./scripts/run.sh                    # chạy bot (log: data/logs/bot.log, trạng thái: data/status.json)
```
Biến môi trường tùy chọn: `TBOT_MIN_QVOL`, `TBOT_WEIGHT_BUDGET`, `TBOT_SITE`, `TBOT_DATA`, `TBOT_DEPLOY`,
`TBOT_PARAMS` (JSON ghi đè tham số, ví dụ `{"min_quality":0.5}`).

## Website
`web/` là dashboard tĩnh (tiếng Việt, giờ UTC+7): Tín hiệu mới, Lệnh đang mở, Lệnh đã đóng, Thống kê (tỷ lệ thắng
theo khung/chiều), Trạng thái bot; lọc theo khung/chiều/mã, bảng sắp xếp được, tự chuyển thành thẻ trên điện thoại.

**Dữ liệu live qua GitHub, không tốn AI, không cần deploy lại web:** bot (Python thuần) tự commit `bot.json` lên nhánh
`data` của repo này mỗi 2 phút khi tín hiệu/lệnh/thống kê thay đổi, và mỗi 10 phút để cập nhật trạng thái. Dashboard
đọc trực tiếp `https://raw.githubusercontent.com/lechinh911/together-trading-bot/data/bot.json` và tự tải lại mỗi 60 giây.
Token GitHub chỉ nằm trong biến môi trường `GITHUB_TOKEN_TOGETHER`, git đọc qua `scripts/git_askpass.sh`
(không ghi vào file/log).

## Cấu trúc
`tbot/engine.py` (phát hiện mô hình + tín hiệu), `tbot/trades.py` (lệnh ảo), `tbot/indicators.py` (EMA/RMA/ATR/DMI),
`tbot/ratelimit.py`, `tbot/rest.py`, `tbot/universe.py`, `tbot/htf.py`, `tbot/store.py`, `tbot/publisher.py`, `tbot/bot.py`.
Kiểm thử trong `tests/` (phát hiện trên dữ liệu Binance thật khớp bản port độc lập v2, lệnh ảo, quy tắc SL trước,
bộ giới hạn, lọc mã, khung lớn, bù nến thiếu).
