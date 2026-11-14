# Logic dự án Together We Thrive — bot tín hiệu 2 Đáy/2 Đỉnh v4

Cập nhật 2026-11-15 (UTC+7). Tài liệu mô tả **đúng những gì code đang làm** (thư mục `tbot/`). Nguồn chuẩn của
chiến lược: Pine chính thức `2 Day/2 Dinh v4 -> Break -> EMA Momentum`
(`/workspace/together_bot_spec/script_v4_official.pine`), preset **Active**.

> Chỉ là tín hiệu + lệnh ảo. Bot không đặt lệnh thật, không có API key Binance. Kết quả/winrate chưa được kiểm chứng.

## 1. Luồng dữ liệu

```
Binance REST (ít)         Binance WebSocket (realtime)
  exchangeInfo + 24hr  ─┐     wss://fstream.binance.com/market/stream?streams=<sym>@kline_<tf>/...
  klines (nạp/bù nến)  ─┤                │  chỉ nhận nến ĐÃ ĐÓNG (k.x = true)
                        ▼                ▼
                 Bot (tbot/bot.py) ── mỗi mã+khung: PairStrategy (engine + 1 lệnh ảo)
                        │
             SQLite data/bot.sqlite (nến, lệnh, signals_log)
                        │
         data/bot.json + data/status.json (mỗi 60 s)
                        │  git push nhánh `data` (nội dung đổi → đẩy trong ≤ 2 phút, không đổi → 10 phút heartbeat)
                        ▼
   raw.githubusercontent.com/lechinh911/together-trading-bot/data/bot.json
                        │  dashboard tự tải lại mỗi 60 s
                        ▼
            https://togetherwethrive.vercel.app (web/)
```

### 1.1 Danh sách mã (`universe.py`, `bot.refresh_universe`)
- Hợp đồng `contractType = PERPETUAL`, `status = TRADING`, `quoteAsset = USDT`, khối lượng 24h `quoteVolume ≥ 1.000.000` USDT
  (`TBOT_MIN_QVOL`). Làm mới mỗi giờ (2 lệnh REST: exchangeInfo weight 1 + ticker/24hr weight 40).
- Loại stablecoin/mã neo giá (USDC, FDUSD, TUSD, USDP, DAI, BUSD, USDE, PYUSD, RLUSD, USD1, USDS… — `STABLE_BASES`) và
  mọi mã có biên độ 24h `(high − low)/giá < 0,3 %` (khác Pine — xem mục 5).
- Mã đang có lệnh ảo mở vẫn được giữ (nếu còn TRADING) dù volume tụt dưới ngưỡng.
- Nếu REST lỗi lúc khởi động: dùng danh sách đã lưu trong SQLite.
- Khung: `5m, 15m, 1h, 4h, 1d` → ≈ 470 mã × 5 = ≈ 2.350 luồng.

### 1.2 WebSocket (`bot.conn_loop`, `_sync_streams`)
- Mainnet `wss://fstream.binance.com/market/stream` (đường `/stream` cũ không còn đẩy kline; **không dùng testnet**
  `binancefuture.com`). Tối đa 200 luồng/kết nối (≈ 12 kết nối), SUBSCRIBE/UNSUBSCRIBE theo lô 50.
- Luồng có trong URL (`?streams=...`) nên mỗi lần kết nối lại tự đăng ký đủ luồng. Tự trả lời ping của server.
- Phát hiện kết nối chết (học từ opencode `stream.py`): bot ping 20 s/lần (`ws_ping_s`, mất pong → đóng), và
  **30 s không có dữ liệu** trên một kết nối (`ws_silence_s`, hạn cứng tính từ tin dữ liệu cuối) → đóng và kết nối lại
  kết nối đó. Lỗi thường: backoff 1→60 s; đóng chủ động: kết nối lại ngay (< 1 s).
- Box/VM bị treo hoặc tạm dừng (vòng lặp chậm > 20 s): đóng và mở lại mọi kết nối + bù nến ngay (`resumes`).
  `kill -USR1 <pid>` ép kết nối lại toàn bộ (dùng để kiểm tra).
- Sau khi kết nối lại, cặp nào thiếu nến sẽ được bù bằng REST.
- Tin kline chưa đóng (`x=false`) chỉ cập nhật giá hiển thị (`last_price`) — **không bao giờ** vào engine.

### 1.3 REST tối thiểu (`rest.py`, `ratelimit.py`)
- Chỉ dùng cho: nạp lịch sử (499 nến/khung, weight 2), bù nến thiếu (`limit` ≤ 99 → weight 1), danh sách mã mỗi giờ.
- Bộ giới hạn trượt 60 s ≤ **600 weight/phút** (`TBOT_WEIGHT_BUDGET`; Binance cho 2400). Tạm dừng tới phút sau nếu
  header `X-MBX-USED-WEIGHT-1M` ≥ 1200 (IP box dùng chung). HTTP 429/418: tôn trọng `Retry-After` (tối thiểu 60 s /
  300 s), lưu thời điểm hết khóa vào `data/rest_ban.json` để khởi động lại vẫn chờ.
- Khởi động lại khi đã có nến trong SQLite chỉ tốn vài chục lệnh REST (ví dụ 17 lệnh, 71 weight, 19 giây).

### 1.4 Nạp lịch sử và thứ tự xử lý (`_warm_symbols`, `_warm_key`, `_backfill`, `_drain`)
1. Kết nối WebSocket trước; nến đóng đến trong lúc nạp được đệm (`busy`).
2. Nạp theo khung **4h trước** (vì 4h là HTF của mọi khung), rồi 1d, 1h, 15m, 5m; 4 luồng song song nhẹ.
3. Mỗi cặp: phát lại tối đa 600 nến đã lưu (nếu nến cuối cũ hơn 400 nến thì tải mới), rồi REST bù tới nến đã đóng gần
   nhất, rồi xả bộ đệm WS.
4. Nến cách quãng (thiếu nến) khi đang chạy → đệm lại + xếp hàng bù REST. **Mỗi 60 s** (`catchup_every_s`) quét: cặp nào
   chưa có nến vừa đóng sau 30 s → bù REST chỉ phần nến thiếu (`startTime = nến cuối + 1`), 6 luồng, đi qua bộ giới
   hạn 600 weight/phút (không bắn dồn).

### 1.5 Lệnh công khai và lệnh "bóng"
- Mọi nến (kể cả lịch sử) đều đi qua đúng logic Pine để trạng thái setup / vị thế / cooldown khớp Pine.
- Tín hiệu chỉ mở **lệnh công khai** khi nến đến từ WS hoặc REST bù (`live=True`), đóng cách đây không quá min(1 nến, 15 phút) (tránh báo muộn với giá cũ, ví dụ nến 4h đã đóng hàng giờ),
  chưa có tín hiệu trùng (cùng mã/khung/nến) trong `signals_log`, và không có lệnh mở đã lưu đang chờ gắn lại.
  Ngược lại là **lệnh bóng** (giữ cho đúng trạng thái Pine, không công bố).
- Lệnh mở đã lưu trong SQLite được gắn lại khi phát lại tới nến sau nến vào lệnh; các nến sau đó được áp dụng tiếp
  (thoát lệnh / BE / trailing), nến đã áp dụng không bị tính lại.

## 2. Chiến lược — từng bước Pine ↔ code

Mỗi nến ĐÃ ĐÓNG của một mã+khung chạy `PairStrategy.on_bar` (`strategy.py`) theo đúng thứ tự Pine
(`process_orders_on_close=true`, `pyramiding=0`):

| # | Pine | Code |
|---|------|------|
| 1 | Broker: lệnh stop/limit đặt ở nến trước được kiểm tra trên nến này | `trades.check_exit` |
| 2 | `emaF/emaS/atr/volSma/rsi/dmi` | `indicators.py`, `Engine._append` |
| 3 | `ta.pivothigh/pivotlow(5,5)` + khối cụm/spike (`validH/validL`) | `Engine._is_pivot`, `_cluster` |
| 4 | Khối `2 DAY` rồi `2 DINH` | `Engine._on_pivot`, `_arm` |
| 5 | `UPDATE BREAK / CANCEL` | `Engine._update_setup`, `_breaks` |
| 6 | `SIGNALS` + `strategy.entry/exit` | `Engine._signal`, `trades.open_trade` |
| 7 | Khối BE / trail của vị thế đang mở | `trades.manage` |
| 8 | `lastExitBar := bar_index` khi vị thế vừa đóng | `PairStrategy.last_exit_idx` |

### 2.1 Chỉ báo (`indicators.py`) — đúng công thức Pine
- `ta.ema` (α = 2/(n+1), giá trị đầu = SMA n nến đầu), `ta.rma` (α = 1/n, mồi SMA), `ta.atr(14)` = RMA của True Range
  (nến đầu TR = high−low), `ta.sma(volume, 20)`, `ta.dmi(14,14)` → ADX, `ta.rsi(14)` (chỉ dùng khi bật RSI; Active tắt).
- Chỉ số được tính từ đầu chuỗi nến bot có (≈ 500–600 nến), không phải toàn bộ lịch sử như TradingView; EMA50/RMA14
  hội tụ nên sai khác chỉ ở chữ số rất nhỏ.

### 2.2 Pivot (`_is_pivot`)
- Nến `i−5` là đỉnh khi 5 nến bên trái **không cao hơn** (được bằng) và 5 nến bên phải **thấp hơn hẳn** (bằng là loại)
  — luật hòa của TradingView `ta.pivothigh` (một vùng đỉnh phẳng cho đúng 1 pivot, ở nến cuối vùng). Đáy đối xứng.

### 2.3 Cụm / spike (`_cluster`) — Pine dòng 169–233
- `tol = 0,5 × ATR[pivot]`. Đếm liên tiếp sang trái rồi sang phải (dừng ở nến đầu tiên không đạt) các nến có
  `high ≥ đỉnh − tol` (đáy: `low ≤ đáy + tol`). `hBot` = high thấp nhất trong cụm (đáy: `lTop` = low cao nhất).
- Hợp lệ nếu cụm ≥ 3 nến **hoặc** spike: đỉnh nhô ≥ 1,0 × ATR[pivot] so với high cao nhất của 10 nến lân cận.
- Chỉ pivot hợp lệ mới được lưu (tối đa 20 pivot mỗi loại) — như Pine `array.push` nằm trong `if validH/validL`.

### 2.4 2 Đáy (2 Đỉnh đối xứng) (`_on_pivot`) — Pine dòng 236–337
Duyệt các đáy hợp lệ cũ từ mới → cũ:
- `gap = idx2 − idx1`; nếu `gap > 100` → dừng. Bỏ qua nếu `gap < 5` hoặc đáy 2 < đáy 1 − 0,75 × ATR[pivot].
- `hh/ll` = cao nhất / thấp nhất từ đáy 1 tới đáy 2. Cần đủ ba điều kiện:
  hồi `đáy2 ≤ hh − 0,55 × (hh − đáy1)`; chiều cao `hh − max(lTop, t1) ≥ 1,6 × ATR[pivot]`;
  không có nến thủng sâu `ll ≥ min(đáy1, đáy2) − 0,75 × ATR`.
- Ứng viên đầu tiên đạt đủ ba điều kiện **kết thúc vòng lặp** (dù chất lượng có đạt hay không).
- Chất lượng `q = 0,4×đối xứng + 0,3×khoảng cách + 0,3×chiều cao` (`f_quality`, `atrSafe = ATR + 1e-10`); chỉ vũ trang
  nếu `q ≥ 0,40` và setup cùng chiều chưa ở trạng thái "đã vũ trang + đã gãy".
- Vũ trang: neckline = `hh` (2 đỉnh: `ll`), SL mô hình = `min(đáy1, đáy2)` (2 đỉnh: `max`), hết hạn = nến hiện tại + 50.
  Mô hình ngược chiều hủy setup đang chờ (`oppCancel`).
- Quét lại k2: 5 nến từ nến pivot+1 tới nến hiện tại: đóng cửa dưới SL → hủy; đóng cửa > neckline + 0,15 × ATR(hiện tại)
  và volume nến đó ≥ 1,15 × SMA20(volume) của nến đó → đánh dấu gãy tại nến đó.

### 2.5 Gãy / hủy (`_update_setup`) — Pine dòng 340–374
- Đóng cửa vượt ngược SL mô hình → hủy. Đã gãy: (Active: không hủy khi retest, không giới hạn thời gian chờ).
- Chưa gãy: quá 50 nến → hủy; đóng cửa > neckline + 0,15 × ATR và volume ≥ 1,15 × SMA20 → gãy (2 đỉnh đối xứng).

### 2.6 Tín hiệu (`_signal`) — Pine dòng 377–388
SHORT (LONG đối xứng) khi **tất cả** đúng:
1. `coolOk`: `bar_index − lastExitBar ≥ 5` (tính từ nến đóng lệnh trước; lần đầu luôn đạt) và đang không có vị thế;
2. setup 2 đỉnh đã vũ trang + đã gãy, nến hiện tại ≥ nến gãy (được vào ngay trên nến gãy);
3. EMA20 < EMA50 và `EMA50 − EMA20 ≥ 0,30 × ATR` (bắt buộc);
4. điểm momentum ≥ 2/3: slope `(EMA20 − EMA20[3]) / ATR ≤ −0,25`; khoảng cách EMA đang mở rộng; giá đóng < cả 2 EMA;
5. ADX14 ≥ 20;
6. HTF: `htfClose < htfEma` (mục 2.7).
Vào lệnh tại giá đóng nến tín hiệu nếu risk > 0; khi đó setup bị xóa (nếu risk ≤ 0 setup vẫn giữ).

### 2.7 HTF 4h cho mọi khung (`htf.py`)
Pine: `request.security(tickerid, "240", close / ta.ema(close,50), lookahead_off)` với `htfTf` mặc định `"240"`.
Bot dùng **4h cho cả 5 khung** và ngữ nghĩa **nến lịch sử của TradingView**: lấy nến 4h cuối cùng có giờ đóng ≤ giờ
đóng nến chart (nến 4h đã xác nhận, không dùng nến 4h đang chạy):
- 5m/15m/1h: nến 4h trước đó; riêng nến chart đóng cùng lúc với nến 4h thì thấy luôn nến 4h đó.
- 4h: chính nến 4h đó (close so với EMA50 của chính nó). 1d: nến 4h cuối của ngày (đóng cùng giờ đóng nến ngày).
- Nếu nến 4h đóng cùng thời điểm chưa kịp tới, bot hoàn tất nó bằng giá đóng nến chart (cùng giá cuối) và tiến EMA
  một bước, nên kết quả không phụ thuộc thứ tự tin nhắn.
- Chuỗi 4h (close, EMA50) được giữ tới ≈ 700 nến để nến 1d khi nạp lịch sử vẫn tra được.

Lý do chọn ngữ nghĩa "đã xác nhận": đây là thứ TradingView hiển thị khi chiến lược được tính lại (mở/tải lại chart),
không repaint, và khớp ca SIRENUSDT 5m 01:25 mà anh đối chiếu (xem mục 6). Ở thời gian thực, TradingView dùng giá trị
nến 4h **đang chạy** cho đến khi chart được tải lại, nên một lệnh hiện trên chart đang mở "live" có thể biến mất/xuất
hiện sau khi tải lại; bot luôn theo bản đã tải lại.

### 2.8 Lệnh ảo (`trades.py`) — Pine dòng 391–457
- Vào: giá đóng nến tín hiệu. SL = SL mô hình, siết tối đa 3 × ATR (`max(SL mô hình, close − 3×ATR)` cho LONG).
  TP = entry ± 1,5R. Phí 0,04 %/chiều trong R và %.
- Mỗi nến sau (broker emulator TradingView, độ chi tiết mặc định):
  1. Giá mở đã vượt SL hoặc TP → khớp tại giá mở (gap).
  2. Ngược lại theo đường đi giả định của TradingView: giá mở gần high hơn → O→H→L→C; ngược lại O→L→H→C; mức nào bị
     chạm trước trên đường đó thì khớp tại đúng mức đó.
  3. Nếu còn mở: nến chạm +1R (`high − entry ≥ risk`) → SL về hòa vốn và bật trailing; từ đó mỗi nến
     `SL = max(SL, close − 1,3 × ATR)` (SHORT đối xứng). TP giữ 1,5R. SL mới áp dụng từ nến sau. Nến vào lệnh không
     được quản lý (như Pine, vị thế chỉ tồn tại sau khi khớp ở giá đóng).
- Lý do thoát: `tp`, `sl`, `be` (SL đúng bằng giá vào), `trail` (SL trailing đã vượt giá vào).
- Đóng lệnh trên nến N → `lastExitBar = N`; Pine cho phép vào lệnh mới ngay trên nến N nếu lần thoát trước đó cách ≥ 5 nến.

## 3. Lưu trữ và xuất bản
- `data/bot.sqlite`: bảng `candles` (600 nến/cặp, ghi theo lô mỗi 10 s, dọn mỗi giờ), `trades` (trạng thái
  `open/closed/void`; `void` = lệnh bị loại vì tạo bởi logic sai trước khi sửa, không hiển thị), `kv` (`universe`,
  `signals_log` 300 tín hiệu gần nhất).
- `publisher.py`: mỗi 60 s ghi `data/bot.json` (schema `tbot-1`: status, signals, open, closed ≤ 300, stats theo khung
  và chiều, waiting = setup đã gãy đang chờ) và `data/status.json`. Đẩy lên nhánh `data` bằng git thường
  (`gitpush.py`), token đọc qua `scripts/git_askpass.sh` từ biến môi trường `GITHUB_TOKEN_TOGETHER`, không ghi ra
  file/log. Chỉ đẩy sau khi nạp xong.

## 4. Dashboard (`web/`)
Trang tĩnh tiếng Việt, giờ UTC+7, đọc `bot.json` từ raw.githubusercontent mỗi 60 s (có cache-buster). Tab: Tín hiệu
mới, Lệnh đang mở, Lệnh đã đóng, Thống kê (winrate theo khung/chiều), Trạng thái bot. Bấm mã → TradingView
`BINANCE:<SYM>.P` đúng khung. Chỉ deploy lại Vercel khi `web/` đổi (`scripts/install_site.sh` rồi
`/workspace/ultimate/scripts/deploy_vercel.sh`).

## 5. Khác biệt còn lại so với TradingView / Pine (đã biết)
- Chỉ có preset Active (tham số khác: `TBOT_PARAMS` JSON ghi đè). Không vẽ hình (box/label/bảng).
- **Khác Pine có chủ đích (2026-11-15, sau lệnh USDCUSDT 1h: rủi ro ≈ 1 tick, phí 0,04 %/chiều → −7,97R):**
  (1) danh sách mã bỏ stablecoin/mã neo giá và mã biên độ 24h < 0,3 % (mục 1.1);
  (2) tín hiệu có rủi ro (entry→SL) < `min_risk_fees` (2) × phí khứ hồi (2 × 0,04 %) = **0,16 % giá vào** không được
  công khai: vẫn chạy như lệnh bóng để trạng thái vị thế/cooldown giống Pine, chỉ không lên dashboard
  (`counters.fee_dominated`, log `skip fee-dominated`).
- Lịch sử chỉ báo ngắn hơn TradingView (mục 2.1); khởi động lại → phát lại 600 nến, trạng thái setup/vị thế có thể khác
  một chuỗi chạy liên tục nhiều ngày.
- HTF theo ngữ nghĩa nến đã xác nhận (mục 2.7), không theo giá trị 4h đang chạy của chart live.
- Bar magnifier (độ chi tiết cao, gói Premium) không mô phỏng; khi giá mở cách đều high và low, bot coi là O→L→H→C
  (TradingView không ghi rõ trường hợp hòa).
- Chưa kiểm chứng: `strategy.position_size[1]` của Pine khi lệnh vào ở giá đóng rồi thoát ngay nến kế tiếp (bot luôn
  ghi `lastExitBar`); giá trị tuyệt đối ATR/EMA so với TradingView (bot không truy cập được TradingView).

## 6. Ca đối chiếu 2026-11-15 (test `tests/test_live_cases.py`)
- **SIRENUSDT 5m, nến 01:20–01:25**: setup 2 đỉnh đã gãy ở nến này, EMA20<EMA50, spread 0,55 ATR, điểm 3/3, ADX 20,6,
  cooldown đạt. Bot cũ dùng HTF 1h (close 0,02606 < EMA 0,02671 → cho SHORT). Pine dùng 4h: nến 4h 19:00–23:00 đóng
  0,02651 > EMA50 ≈ 0,02645 → `htfBear = false` → **không vào lệnh** (TradingView chỉ hiện nhãn gãy). Lệnh cũ đã đánh dấu `void`.
- **WOOUSDT 5m, nến 01:25–01:30**: Pine/TradingView có SHORT tại 0,00933. Bot cũ bỏ lỡ vì pivot loại cả đỉnh có nến bằng giá
  bên trái, mất đỉnh 22:00 (0,00966); đỉnh 00:35 chỉ còn ghép được với đỉnh 12:15 (cách > 100 nến) nên không có mô hình. Sau khi sửa: 2 đỉnh 0,00966 (22:00) và 0,00953 (00:35),
  neckline 0,00934, q = 0,464, gãy ở nến 01:25, HTF 4h giảm → SHORT 0,00933, SL 0,0094912 (siết 3 ATR), TP 0,0090883.
- Sau khi sửa, bot chạy lại logic mới cho các nến từ 00:50 hôm nay: hủy 1 lệnh (SIREN), bổ sung 5 lệnh logic Pine có
  nhưng bot cũ bỏ lỡ (AIA, BILL, FARTCOIN, ICNT, WOO; đánh dấu `recovered` trong dữ liệu).

## 7. Kiểm thử
`.venv/bin/python -m pytest` — 36 test: chỉ báo/phát hiện khớp bản port v2 độc lập, các bộ lọc Active, tiền tố không
repaint, luật lệnh ảo (gap, đường đi O-H-L-C, BE/trail), bộ giới hạn REST, lọc mã, HTF 4h, nến đang hình thành bị bỏ qua,
bù nến, và **parity**: so khớp từng tín hiệu/lệnh/giá thoát với mô phỏng tham chiếu viết theo từng dòng Pine
(`tests/pine_reference.py`) trên nến Binance thật, cùng hai ca SIREN/WOO. Quét thêm 600 cặp mã/khung từ dữ liệu live:
0 khác biệt giữa engine và tham chiếu.

## 8. Vận hành
- Chạy: `scripts/run.sh` (= `.venv/bin/python -m tbot`). Quản lý: `bash /workspace/ops/status.sh`, `ops/start_all.sh`,
  `ops/stop_all.sh`; chỉ khởi động lại bot: `. /workspace/ops/common.sh; stop_one tradingbot; start_one tradingbot`.
  Watchdog (`ops/watchdog.sh --loop`) bật lại bot trong ≤ 10 phút và xoay log.
- Sau khi box khởi động lại: không có gì tự chạy. Từ shell có `GITHUB_TOKEN_TOGETHER` (và `VERCEL_TOKEN` nếu deploy web):
  nếu thiếu `.venv` thì `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, rồi `bash /workspace/ops/start_all.sh`.
- Log `data/logs/bot.log` (xoay 5 MB × 3), trạng thái `data/status.json`.
