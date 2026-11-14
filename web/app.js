'use strict';
(() => {
const TFS = ['5m', '15m', '1h', '4h', '1d'];
// Live data: the bot pushes bot.json to the `data` branch on GitHub; the site never needs a redeploy for new data.
const DATA_URLS = ['https://raw.githubusercontent.com/lechinh911/together-trading-bot/data/bot.json', 'data/bot.json'];
const S = {data: null, tab: 'signals', tf: '', side: '', q: '', sort: {}, limit: {}};
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const dash = '—';

// ---------- formatting ----------
function fp(x) {
  if (x == null || !isFinite(x)) return dash;
  const a = Math.abs(x);
  if (a >= 1000) return x.toLocaleString('en-US', {maximumFractionDigits: 2});
  if (a >= 1) return String(Number(x.toPrecision(6)));
  if (a >= 1e-4) return String(Number(x.toPrecision(5)));
  return x.toFixed(10).replace(/0+$/, '');
}
const fr = (x, d = 2) => (x == null || !isFinite(x)) ? dash : (x > 0 ? '+' : '') + x.toFixed(d) + 'R';
const fpc = (x, d = 2) => (x == null || !isFinite(x)) ? dash : (x > 0 ? '+' : '') + x.toFixed(d) + '%';
const sgn = x => x == null ? 'mut' : x > 0 ? 'pos' : x < 0 ? 'neg' : 'mut';
function t7(ms) { // epoch ms -> 'dd/mm HH:MM' in UTC+7
  if (ms == null) return dash;
  const d = new Date(ms + 7 * 3600e3), p = n => String(n).padStart(2, '0');
  return `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}
function s7(str) { // 'YYYY-MM-DD HH:MM' -> 'dd/mm HH:MM'
  if (!str) return dash;
  const m = /^(\d{4})-(\d\d)-(\d\d) (\d\d:\d\d)/.exec(str);
  return m ? `${m[3]}/${m[2]} ${m[4]}` : str;
}
function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 90) return 'vừa xong';
  if (s < 3600) return Math.round(s / 60) + ' phút trước';
  if (s < 86400) return Math.round(s / 3600) + ' giờ trước';
  return Math.round(s / 86400) + ' ngày trước';
}
function dur(s) {
  if (s == null) return dash;
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + 'n ' : '') + (h ? h + 'g ' : '') + m + 'p';
}
const TFMS = {'5m': 3e5, '15m': 9e5, '1h': 3.6e6, '4h': 1.44e7, '1d': 8.64e7};
const TVI = {'5m': '5', '15m': '15', '1h': '60', '4h': '240', '1d': 'D'};
const REASON = {tp: 'Chốt lời 1.5R', sl: 'Cắt lỗ', be: 'Hòa vốn', trail: 'Trailing'};

// ---------- small builders ----------
const badge = side => el('span', 'badge ' + (side === 'LONG' ? 'long' : 'short'), side);
const tfTag = tf => el('span', 'tf', tf);
function symCell(r) {
  const f = document.createDocumentFragment();
  const a = el('a', 'sym', r.symbol.replace(/USDT$/, ''));
  a.href = 'https://www.tradingview.com/chart/?symbol=' + encodeURIComponent('BINANCE:' + r.symbol + '.P') + '&interval=' + (TVI[r.tf] || '60');
  a.title = 'Mở TradingView ' + r.symbol + ' ' + r.tf;
  a.target = '_blank'; a.rel = 'noopener';
  a.appendChild(el('small', null, 'USDT'));
  f.appendChild(a);
  const b = badge(r.side); b.classList.add('mo'); f.appendChild(b);
  const t = tfTag(r.tf); t.classList.add('mo'); f.appendChild(t);
  return f;
}
function qual(q) {
  const w = el('span', 'qbar');
  if (q == null) { w.textContent = dash; return w; }
  const i = el('i'), b = el('b'); b.style.width = Math.round(Math.min(1, q) * 100) + '%'; i.appendChild(b);
  w.appendChild(i); w.appendChild(el('span', 'num', q.toFixed(2)));
  return w;
}
function numSpan(txt, cls) { return el('span', 'num ' + (cls || ''), txt); }

// ---------- generic sortable table (cards on mobile) ----------
function table(id, cols, rows, defSort) {
  const st = S.sort[id] || (S.sort[id] = defSort);
  const col = cols.find(c => c.k === st.k);
  if (col && col.v) rows = rows.slice().sort((a, b) => {
    const x = col.v(a), y = col.v(b);
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * (st.d === 'asc' ? 1 : -1);
  });
  const lim = S.limit[id] || 50;
  const wrap = el('div', 'tbl-wrap'), t = el('table'), th = el('thead'), hr = el('tr');
  cols.forEach(c => {
    const h = el('th', (c.r ? 'r ' : '') + (c.v ? 'sort ' : '') + (st.k === c.k ? st.d : ''), c.l);
    if (c.v) {
      h.tabIndex = 0; h.setAttribute('aria-sort', st.k === c.k ? (st.d === 'asc' ? 'ascending' : 'descending') : 'none');
      const go = () => { S.sort[id] = {k: c.k, d: st.k === c.k && st.d === 'desc' ? 'asc' : 'desc'}; render(); };
      h.onclick = go; h.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } };
    }
    hr.appendChild(h);
  });
  th.appendChild(hr); t.appendChild(th);
  const tb = el('tbody');
  rows.slice(0, lim).forEach(r => {
    const tr = el('tr');
    cols.forEach(c => {
      const td = el('td', (c.r ? 'r ' : '') + (c.head ? 'head ' : '') + (c.hm ? 'hide-m' : ''));
      td.dataset.l = c.l;
      const v = c.f(r);
      if (v instanceof Node) td.appendChild(v); else td.textContent = v == null ? dash : v;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
  t.appendChild(tb); wrap.appendChild(t);
  const f = document.createDocumentFragment(); f.appendChild(wrap);
  if (rows.length > lim) {
    const m = el('button', 'more', `Xem thêm (${rows.length - lim} dòng)`);
    m.onclick = () => { S.limit[id] = lim + 50; render(); };
    f.appendChild(m);
  }
  return f;
}
function empty(title, sub) { const e = el('div', 'empty'); e.appendChild(el('b', null, title)); if (sub) e.appendChild(el('span', null, sub)); return e; }
function head(title, sub) { const h = el('div', 'ph'); h.appendChild(el('h2', null, title)); if (sub) h.appendChild(el('p', null, sub)); return h; }

function filt(rows) {
  const q = S.q.trim().toUpperCase();
  return rows.filter(r => (!S.tf || r.tf === S.tf) && (!S.side || r.side === S.side) && (!q || r.symbol.includes(q)));
}

// ---------- panels ----------
function pSignals(d) {
  const p = $('#panel-signals'); p.replaceChildren();
  const rows = filt(d.signals);
  p.appendChild(head('Tín hiệu mới', 'Nến đóng có đủ điều kiện → mở lệnh ảo tại giá đóng cửa. Giờ là lúc nến tín hiệu đóng (UTC+7).'));
  if (!rows.length) p.appendChild(empty(d.signals.length ? 'Không có tín hiệu khớp bộ lọc' : 'Chưa có tín hiệu nào',
    d.signals.length ? 'Thử bỏ bớt bộ lọc khung thời gian, chiều hoặc mã.' : 'Bot đang quét realtime; tín hiệu xuất hiện khi mô hình gãy neckline và EMA/momentum xác nhận.'));
  else p.appendChild(table('sig', [
    {k: 't', l: 'Thời gian', f: r => { const s = el('span', 'num', t7(r.t + TFMS[r.tf])); s.title = ago(r.t + TFMS[r.tf]); return s; }, v: r => r.t + TFMS[r.tf]},
    {k: 'symbol', l: 'Mã', head: true, f: symCell, v: r => r.symbol},
    {k: 'side', l: 'Chiều', hm: true, f: r => badge(r.side), v: r => r.side},
    {k: 'tf', l: 'Khung', hm: true, f: r => tfTag(r.tf), v: r => TFMS[r.tf]},
    {k: 'entry', l: 'Giá vào', r: true, f: r => numSpan(fp(r.entry))},
    {k: 'stop', l: 'Dừng lỗ', r: true, f: r => numSpan(fp(r.stop), 'neg')},
    {k: 'tp', l: 'Chốt lời', r: true, f: r => numSpan(fp(r.tp), 'pos')},
    {k: 'neck', l: 'Neckline', r: true, hm: true, f: r => numSpan(fp(r.neck))},
    {k: 'quality', l: 'Chất lượng', f: r => qual(r.quality), v: r => r.quality},
    {k: 'adx', l: 'ADX', r: true, f: r => numSpan(r.adx == null ? dash : r.adx.toFixed(1)), v: r => r.adx},
    {k: 'state', l: 'Trạng thái', f: r => el('span', 'tag', r.state === 'open' ? 'Đang mở' : r.state === 'skipped' ? 'Bỏ qua' : 'Đã đóng'), v: r => r.state},
  ], rows, {k: 't', d: 'desc'}));
  const w = filt(d.waiting || []);
  p.appendChild(head('Mô hình đã gãy, chờ xác nhận', 'Neckline đã bị phá; chờ EMA20/50, momentum, ADX và xu hướng khung lớn. Chưa phải tín hiệu.'));
  if (!w.length) p.appendChild(empty('Không có mô hình nào đang chờ', null));
  else p.appendChild(table('wait', [
    {k: 'break_t', l: 'Gãy lúc', f: r => numSpan(s7(r.break_t7)), v: r => r.break_t},
    {k: 'symbol', l: 'Mã', head: true, f: symCell, v: r => r.symbol},
    {k: 'side', l: 'Chiều', hm: true, f: r => badge(r.side), v: r => r.side},
    {k: 'tf', l: 'Khung', hm: true, f: r => tfTag(r.tf), v: r => TFMS[r.tf]},
    {k: 'neck', l: 'Neckline', r: true, f: r => numSpan(fp(r.neck))},
    {k: 'sl', l: 'Đáy/đỉnh mô hình', r: true, f: r => numSpan(fp(r.sl))},
    {k: 'quality', l: 'Chất lượng', f: r => qual(r.quality), v: r => r.quality},
  ], w, {k: 'break_t', d: 'desc'}));
}

function pOpen(d) {
  const p = $('#panel-open'); p.replaceChildren();
  const rows = filt(d.open);
  p.appendChild(head('Lệnh đang mở (ảo)', 'Mỗi mã + khung tối đa 1 lệnh. R tạm tính theo giá gần nhất lúc xuất dữ liệu, đã trừ phí 0,04%/chiều.'));
  if (!rows.length) { p.appendChild(empty('Không có lệnh ảo đang mở', d.open.length ? 'Không có lệnh khớp bộ lọc.' : null)); return; }
  p.appendChild(table('open', [
    {k: 'entry_t', l: 'Mở lúc', f: r => numSpan(s7(r.entry_t7)), v: r => r.entry_t},
    {k: 'symbol', l: 'Mã', head: true, f: symCell, v: r => r.symbol},
    {k: 'side', l: 'Chiều', hm: true, f: r => badge(r.side), v: r => r.side},
    {k: 'tf', l: 'Khung', hm: true, f: r => tfTag(r.tf), v: r => TFMS[r.tf]},
    {k: 'entry', l: 'Giá vào', r: true, f: r => numSpan(fp(r.entry))},
    {k: 'last', l: 'Giá gần nhất', r: true, f: r => numSpan(fp(r.last))},
    {k: 'stop', l: 'Dừng lỗ hiện tại', r: true, f: r => { const s = numSpan(fp(r.stop), 'neg'); if (r.be) s.title = 'Đã dời về hòa vốn / trailing'; return s; }},
    {k: 'tp', l: 'Chốt lời', r: true, f: r => numSpan(fp(r.tp), 'pos')},
    {k: 'mode', l: 'Quản lý', f: r => el('span', 'tag' + (r.be ? ' be' : ''), r.trail ? 'Trailing' : r.be ? 'Hòa vốn' : 'Ban đầu'), v: r => (r.be ? 1 : 0) + (r.trail ? 1 : 0)},
    {k: 'open_R', l: 'R tạm tính', r: true, f: r => numSpan(fr(r.open_R), sgn(r.open_R)), v: r => r.open_R},
  ], rows, {k: 'entry_t', d: 'desc'}));
}

function pClosed(d) {
  const p = $('#panel-closed'); p.replaceChildren();
  const rows = filt(d.closed);
  p.appendChild(head('Lệnh đã đóng (ảo)', 'Nếu dừng lỗ và chốt lời cùng chạm trong một nến, bot giả định dừng lỗ trước. Kết quả đã trừ phí.'));
  if (!rows.length) { p.appendChild(empty('Chưa có lệnh ảo nào đóng', d.closed.length ? 'Không có lệnh khớp bộ lọc.' : 'Lệnh sẽ hiện ở đây khi chạm dừng lỗ, hòa vốn, trailing hoặc chốt lời 1.5R.')); return; }
  p.appendChild(table('closed', [
    {k: 'exit_t', l: 'Đóng lúc', f: r => numSpan(s7(r.exit_t7)), v: r => r.exit_t},
    {k: 'symbol', l: 'Mã', head: true, f: symCell, v: r => r.symbol},
    {k: 'side', l: 'Chiều', hm: true, f: r => badge(r.side), v: r => r.side},
    {k: 'tf', l: 'Khung', hm: true, f: r => tfTag(r.tf), v: r => TFMS[r.tf]},
    {k: 'entry', l: 'Giá vào', r: true, f: r => numSpan(fp(r.entry))},
    {k: 'exit', l: 'Giá ra', r: true, f: r => numSpan(fp(r.exit))},
    {k: 'reason', l: 'Lý do', f: r => el('span', 'tag ' + (r.reason || ''), REASON[r.reason] || r.reason || dash), v: r => r.reason},
    {k: 'bars', l: 'Số nến', r: true, hm: true, f: r => numSpan(r.bars == null ? dash : String(r.bars)), v: r => r.bars},
    {k: 'r', l: 'R', r: true, f: r => numSpan(fr(r.r), sgn(r.r)), v: r => r.r},
    {k: 'pct', l: '% giá', r: true, f: r => numSpan(fpc(r.pct), sgn(r.pct)), v: r => r.pct},
  ], rows, {k: 'exit_t', d: 'desc'}));
}

function pStats(d) {
  const p = $('#panel-stats'); p.replaceChildren();
  const st = d.stats;
  p.appendChild(head('Thống kê lệnh ảo', 'Tính từ khi bot chạy trực tiếp. Mẫu nhỏ chưa có ý nghĩa thống kê — chưa kiểm chứng hiệu quả.'));
  const rows = ['ALL'].concat(TFS).map(k => Object.assign({tf: k}, st[k] || {}));
  p.appendChild(table('stats', [
    {k: 'tf', l: 'Khung', head: true, f: r => r.tf === 'ALL' ? el('b', null, 'Tất cả') : tfTag(r.tf)},
    {k: 'signals', l: 'Tín hiệu', r: true, f: r => numSpan(String(r.signals ?? 0))},
    {k: 'open', l: 'Đang mở', r: true, f: r => numSpan(String(r.open ?? 0))},
    {k: 'n', l: 'Đã đóng', r: true, f: r => numSpan(String(r.n ?? 0))},
    {k: 'wr', l: 'Tỷ lệ thắng', r: true, f: r => numSpan(r.win_rate == null ? dash : r.win_rate.toFixed(1) + '%')},
    {k: 'avg', l: 'R TB/lệnh', r: true, f: r => numSpan(fr(r.avg_r), sgn(r.avg_r))},
    {k: 'sum', l: 'Tổng R', r: true, f: r => numSpan(fr(r.sum_r), sgn(r.sum_r))},
    {k: 'pct', l: '% giá TB', r: true, f: r => numSpan(fpc(r.avg_pct), sgn(r.avg_pct))},
    {k: 'best', l: 'Tốt nhất', r: true, hm: true, f: r => numSpan(fr(r.best_r), 'pos')},
    {k: 'worst', l: 'Tệ nhất', r: true, hm: true, f: r => numSpan(fr(r.worst_r), 'neg')},
  ], rows, {k: '', d: 'desc'}));
  const g = el('div', 'sgrid'); g.style.marginTop = '12px';
  const b1 = el('div', 'box'); b1.appendChild(el('h3', null, 'Tổng R theo khung'));
  const bars = el('div', 'bars');
  const mx = Math.max(1, ...TFS.map(k => Math.abs((st[k] || {}).sum_r || 0)));
  TFS.forEach(k => {
    const v = (st[k] || {}).sum_r, row = el('div', 'brow');
    row.appendChild(tfTag(k));
    const tr = el('div', 'track'), b = el('b');
    const w = v ? Math.abs(v) / mx * 50 : 0;
    b.style.width = w + '%'; b.style.left = v >= 0 ? '50%' : (50 - w) + '%';
    b.style.background = v >= 0 ? 'var(--up)' : 'var(--dn)';
    tr.appendChild(b); row.appendChild(tr); row.appendChild(numSpan(v == null ? dash : fr(v), sgn(v)));
    bars.appendChild(row);
  });
  b1.appendChild(bars); g.appendChild(b1);
  const b2 = el('div', 'box'); b2.appendChild(el('h3', null, 'LONG / SHORT & cách đóng lệnh'));
  const dl = el('dl', 'kv');
  const add = (k, v, cls) => { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', cls, v)); };
  ['LONG', 'SHORT'].forEach(s => { const x = st['side_' + s] || {}; add(s + ' — đã đóng / thắng', `${x.n || 0} / ${x.win_rate == null ? dash : x.win_rate.toFixed(1) + '%'}`); add(s + ' — tổng R', fr(x.sum_r), sgn(x.sum_r)); });
  const br = (st.ALL || {}).by_reason || {};
  Object.keys(REASON).forEach(k => add(REASON[k], String(br[k] || 0)));
  b2.appendChild(dl); g.appendChild(b2);
  p.appendChild(g);
}

function pStatus(d) {
  const p = $('#panel-status'); p.replaceChildren();
  const s = d.status, rest = s.rest || {}, su = s.startup || {}, c = s.counters || {};
  p.appendChild(head('Trạng thái bot', 'Dữ liệu realtime qua WebSocket nến Binance; REST chỉ dùng để nạp lịch sử, bù nến thiếu và làm mới danh sách mã mỗi giờ.'));
  const g = el('div', 'sgrid');
  const box = (title, pairs) => {
    const b = el('div', 'box'); b.appendChild(el('h3', null, title)); const dl = el('dl', 'kv');
    pairs.forEach(([k, v]) => { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v == null ? dash : String(v))); });
    b.appendChild(dl); return b;
  };
  g.appendChild(box('Vận hành', [
    ['Dữ liệu xuất lúc', s7(d.generated_t7) + ' (UTC+7)'], ['Khởi động lúc', s7(s.started_t7)], ['Thời gian chạy', dur(s.uptime_s)],
    ['Preset', s.preset], ['Khung thời gian', (s.timeframes || []).join(' · ')],
    ['Số mã theo dõi', `${s.universe} (KL 24h ≥ ${(s.universe_min_qvol / 1e6).toFixed(0)} triệu USDT)`], ['Cập nhật danh sách mã', s7(s.universe_t7)],
    ['Cặp mã × khung đã nạp', `${s.keys_warm}/${s.keys}`],
  ]));
  g.appendChild(box('Kết nối & tải', [
    ['WebSocket', `${s.connected}/${s.connections} kết nối · ${s.streams} luồng nến`], ['Tin nhắn WS đã nhận', (c.ws_msgs || 0).toLocaleString('vi-VN')],
    ['Nến đóng đã xử lý', (c.closed_bars || 0).toLocaleString('vi-VN')], ['Lần bù nến thiếu (gap)', c.gaps || 0],
    ['REST: số lệnh gọi / weight', `${rest.calls || 0} / ${rest.weight_total || 0}`], ['REST: weight cao nhất 60s (bot / máy chủ)', `${rest.weight_peak_60s || 0} / ${rest.server_weight_peak || 0} (giới hạn tự đặt ${rest.budget_per_min})`],
    ['Khởi động: lệnh gọi / weight', su.rest_calls == null ? 'đang nạp…' : `${su.rest_calls} / ${su.rest_weight} trong ${su.warm_seconds}s`],
    ['HTTP 429 / 418', `${rest.http_429 || 0} / ${rest.http_418 || 0}` + (rest.blocked_until_t ? ` · tạm dừng REST đến ${t7(rest.blocked_until_t * 1000)}` : '')],
  ]));
  const w = s.setups_waiting || {}, bk = s.setups_broken || {};
  g.appendChild(box('Mô hình đang theo dõi', TFS.map(k => [k, `${w[k] || 0} mô hình chờ · ${bk[k] || 0} đã gãy`])));
  const P = s.params || {};
  g.appendChild(box('Tham số Active', [
    ['Pivot / ATR / cụm', `${P.piv_left}/${P.piv_right} · ATR${P.atr_len} · cụm ${P.cluster_atr}×ATR, ≥${P.min_cluster} nến, spike ${P.spike_atr}`],
    ['Mô hình', `hồi ≥${P.min_retrace} · cao ≥${P.min_height_atr}×ATR · ${P.min_bars}–${P.max_gap} nến · chất lượng ≥${P.min_quality}`],
    ['Gãy', `đóng vượt neckline ≥${P.break_buf}×ATR · KL ≥${P.vol_mult}×TB${P.vol_len}`],
    ['Xác nhận', `EMA${P.ema_fast}/${P.ema_slow} cách ≥${P.spread_atr}×ATR · điểm ≥${P.min_score}/3 · ADX ≥${P.adx_min} · khung lớn EMA${P.htf_ema}`],
    ['Lệnh ảo', `SL tối đa ${P.stop_cap_atr}×ATR · TP ${P.tp_r}R · hòa vốn tại ${P.be_at_r}R · trailing ${P.trail_atr}×ATR · nghỉ ${P.cooldown} nến`],
  ]));
  p.appendChild(g);
  const cb = el('div', 'box'); cb.style.marginTop = '12px'; cb.appendChild(el('h3', null, 'Kết nối WebSocket'));
  const cg = el('div', 'conns');
  (s.conns || []).forEach(x => {
    const k = el('div', 'conn'); const dt = el('span', 'dot' + (x.connected ? ' on' : '')); k.appendChild(dt);
    k.appendChild(el('b', null, '#' + x.id)); k.appendChild(document.createTextNode(` · ${x.streams} luồng · nối lại ${x.reconnects}`));
    cg.appendChild(k);
  });
  cb.appendChild(cg); p.appendChild(cb);
  const gh = s.github || {};
  const dd = el('p', 'mut', `Dữ liệu được bot tự đẩy lên GitHub (nhánh data): ${gh.pushes || 0} lần từ khi khởi động` + (gh.last_push_t7 ? `, gần nhất ${s7(gh.last_push_t7)}` : '') + (gh.last_error ? ` · lỗi gần nhất: ${gh.last_error}` : '') + '. Trang tự tải lại mỗi 60 giây.');
  dd.style.fontSize = '12px'; p.appendChild(dd);
}

function kpis(d) {
  const k = $('#kpis'); k.replaceChildren();
  const s = d.status, a = d.stats.ALL || {};
  const add = (lbl, val, sub, cls) => { const c = el('div', 'kpi'); c.appendChild(el('div', 'lbl', lbl)); c.appendChild(el('div', 'val ' + (cls || ''), val)); c.appendChild(el('div', 'sub', sub)); k.appendChild(c); };
  add('Mã theo dõi', String(s.universe), `KL 24h ≥ ${(s.universe_min_qvol / 1e6).toFixed(0)}tr USDT`);
  add('Tín hiệu', String(d.signals.length), d.signals[0] ? 'Gần nhất ' + ago(d.signals[0].t + TFMS[d.signals[0].tf]) : 'Chưa có tín hiệu');
  add('Lệnh đang mở', String(d.open.length), `${d.open.filter(x => x.side === 'LONG').length} LONG · ${d.open.filter(x => x.side === 'SHORT').length} SHORT`);
  add('Lệnh đã đóng', String(a.n || 0), a.n ? `${a.wins} thắng · ${a.losses} thua` : 'Chưa có');
  add('Tỷ lệ thắng', a.win_rate == null ? dash : a.win_rate.toFixed(1) + '%', 'Lệnh ảo, chưa kiểm chứng');
  add('Tổng R', a.sum_r == null ? dash : fr(a.sum_r), a.avg_r == null ? 'R TB: —' : 'R TB: ' + fr(a.avg_r, 3), sgn(a.sum_r));
}

function tfcards(d) {
  const w = $('#tfcards'); w.replaceChildren();
  TFS.forEach(tf => {
    const x = d.stats[tf] || {}, c = el('button', 'tfc' + (S.tf === tf ? ' on' : ''));
    c.type = 'button'; c.setAttribute('aria-pressed', S.tf === tf);
    const h = el('div', 'tfc-h'); h.appendChild(el('b', null, tf)); h.appendChild(el('span', null, `${(d.status.setups_broken || {})[tf] || 0} chờ xác nhận`)); c.appendChild(h);
    const g = el('div', 'tfc-g');
    [['Tín hiệu', String(x.signals || 0)], ['Đang mở', String(x.open || 0)], ['Thắng', x.win_rate == null ? dash : x.win_rate.toFixed(0) + '%'], ['Tổng R', x.sum_r == null ? dash : fr(x.sum_r, 1), sgn(x.sum_r)]]
      .forEach(([l, v, cls]) => { const dv = el('div'); dv.appendChild(el('small', null, l)); dv.appendChild(el('span', cls, v)); g.appendChild(dv); });
    c.appendChild(g);
    c.onclick = () => { S.tf = S.tf === tf ? '' : tf; render(); };
    w.appendChild(c);
  });
}

function chips() {
  const t = $('#tf-chips'); t.replaceChildren();
  [['', 'Tất cả'], ...TFS.map(x => [x, x])].forEach(([v, l]) => {
    const b = el('button', S.tf === v ? 'on' : '', l); b.type = 'button'; b.setAttribute('aria-pressed', S.tf === v);
    b.onclick = () => { S.tf = v; render(); }; t.appendChild(b);
  });
  const s = $('#side-chips'); s.replaceChildren();
  [['', 'Cả hai'], ['LONG', 'LONG'], ['SHORT', 'SHORT']].forEach(([v, l]) => {
    const b = el('button', (S.side === v ? 'on ' : '') + v.toLowerCase(), l); b.type = 'button'; b.setAttribute('aria-pressed', S.side === v);
    b.onclick = () => { S.side = v; render(); }; s.appendChild(b);
  });
}

function live(d) {
  const p = $('#live-pill'), t = $('#live-text');
  const age = Date.now() / 1000 - d.generated, s = d.status;
  p.className = 'pill ' + (s.connected < s.connections ? 'warn' : age < 1500 ? 'ok' : 'warn');
  t.textContent = (age < 1500 ? 'Đang chạy · ' : 'Dữ liệu cũ · ') + 'cập nhật ' + s7(d.generated_t7).slice(6);
  t.title = 'Dữ liệu xuất lúc ' + d.generated_t7 + ' (UTC+7)';
}

function render() {
  const d = S.data; if (!d) return;
  live(d); kpis(d); tfcards(d); chips();
  $('#n-signals').textContent = d.signals.length; $('#n-open').textContent = d.open.length; $('#n-closed').textContent = d.closed.length;
  document.querySelectorAll('.tabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === S.tab));
  document.querySelectorAll('.panel').forEach(p => { p.hidden = p.id !== 'panel-' + S.tab; });
  $('#filters').hidden = S.tab === 'stats' || S.tab === 'status';
  ({signals: pSignals, open: pOpen, closed: pClosed, stats: pStats, status: pStatus})[S.tab](d);
}

function clock() {
  const d = new Date(Date.now() + 7 * 3600e3), p = n => String(n).padStart(2, '0');
  $('#clock').textContent = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC+7`;
}

async function load() {
  try {
    let r = null;
    for (const u of DATA_URLS) {
      try { r = await fetch(u + '?t=' + Date.now(), {cache: 'no-store'}); if (r.ok) break; } catch (e) { r = null; }
    }
    if (!r || !r.ok) throw new Error(r ? 'HTTP ' + r.status : 'mạng');
    S.data = await r.json();
    const e = $('#err'); if (e) e.remove();
    render();
  } catch (e) {
    if (!$('#err')) { const x = el('div', 'err', 'Chưa có dữ liệu bot (' + e.message + '). Bot có thể đang khởi động và nạp lịch sử nến; trang tự thử lại mỗi 60 giây.'); x.id = 'err'; $('#main').prepend(x); }
    $('#live-pill').className = 'pill bad'; $('#live-text').textContent = 'Không tải được dữ liệu';
  }
}

document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => { S.tab = b.dataset.tab; render(); });
$('#q').addEventListener('input', e => { S.q = e.target.value; render(); });
clock(); setInterval(clock, 15000);
load(); setInterval(load, 60000);
})();
