const state = {
  token: localStorage.getItem("token") || null,
  role: localStorage.getItem("role") || null, // "admin" | "investor"
  lastPositions: [],
  symbols: [],
  hiddenSymbols: JSON.parse(localStorage.getItem("hiddenSymbols") || "[]"),
  watchEditMode: false,
  watchViewMode: "simple",
  tickTime: {},
  dayHiLo: {},
  prices: {}, // symbol -> {bid, ask, last}
  tickDirection: {}, // symbol -> "up" | "down"
  currentSymbol: "BTC/USDT",
  timeframe: "1h",
  orderMode: "market",
  historyRange: "day",
  historySearch: "",
  historyCustomStart: null,
  historyCustomEnd: null,
  lastHistory: [],
  lastAccountSummary: null,
  ws: null,
  expandedTicket: null,
  orderQty: 0.01,
  orderSl: null,
  orderTp: null,
  orderDeviation: null,
  sparkline: [],
};

const SPREAD_BPS_CLIENT = 5; // mirrors engine.py SPREAD_BPS, used to seed the chart with a visual bid/ask spread from historical closes

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    logout();
  }
  return res.json();
}

function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

function fmt(n, decimals = 2) {
  if (n === undefined || n === null || Number.isNaN(n)) return "-";
  return n.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function pnlClass(n) {
  return n >= 0 ? "pnl-pos" : "pnl-neg";
}

function dispSym(symbol) {
  return symbol.endsWith("=X") ? symbol.slice(0, -2) : symbol;
}

function pricePrecision(symbol) {
  if (symbol.includes("JPY")) return 3;
  if (symbol === "XAUUSD=X") return 2;
  if (symbol.endsWith("=X")) return 5; // forex
  return 2; // crypto, stocks
}

// ---------- auth ----------
// One shared account number, two passwords — whichever matches determines
// the role for this session. No per-person identity.

async function login() {
  const account_number = $("account-input").value.trim();
  const password = $("password-input").value;
  if (!account_number || !password) { $("auth-msg").textContent = "請輸入帳號和密碼"; return; }

  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_number, password }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    $("auth-msg").textContent = `錯誤：${err.detail || "登入失敗"}`;
    return;
  }
  const data = await res.json();
  state.token = data.token;
  state.role = data.role;
  localStorage.setItem("token", state.token);
  localStorage.setItem("role", state.role);
  $("auth-msg").textContent = "";
  $("password-input").value = "";
  showLoggedIn();
  sendWsAuth();
  refreshAll();
}

function logout() {
  state.token = null;
  state.role = null;
  localStorage.removeItem("token");
  localStorage.removeItem("role");
  showLoggedOut();
  sendWsAuth();
}

function showLoggedIn() {
  $("auth-box").classList.add("hidden");
  $("account-bar").classList.remove("hidden");
  const badge = $("role-badge");
  badge.textContent = state.role === "admin" ? "管理員" : "投資人（唯讀）";
  badge.className = state.role === "admin" ? "admin" : "investor";
  $("readonly-notice").textContent = "目前是投資人唯讀模式，僅能查看基金狀況，無法下單。";
  $("readonly-notice").classList.toggle("hidden", state.role === "admin");
  $("trade-controls").classList.toggle("hidden", state.role !== "admin");
  $("order-mobile-ticket").classList.toggle("hidden", state.role !== "admin");
}

function showLoggedOut() {
  $("auth-box").classList.remove("hidden");
  $("account-bar").classList.add("hidden");
  $("trade-controls").classList.add("hidden");
  $("order-mobile-ticket").classList.add("hidden");
  $("readonly-notice").textContent = "請先登入才能查看與操作基金。";
  $("readonly-notice").classList.remove("hidden");
}

// ---------- charts ----------

let mainChart, candleSeries, maSeries, rsiChart, rsiSeries, macdChart, macdLineSeries, macdSignalSeries, macdHistSeries;

function initCharts() {
  const chartOptions = {
    layout: { background: { color: "transparent" }, textColor: "#8b93a7" },
    grid: { vertLines: { color: "#262b38" }, horzLines: { color: "#262b38" } },
    rightPriceScale: { borderColor: "#262b38" },
    timeScale: { borderColor: "#262b38" },
  };

  mainChart = LightweightCharts.createChart($("main-chart"), {
    ...chartOptions,
    width: $("main-chart").clientWidth,
    height: $("main-chart").clientHeight,
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  candleSeries = mainChart.addCandlestickSeries({
    upColor: "#3ddc84", downColor: "#ff5c5c", borderVisible: false,
    wickUpColor: "#3ddc84", wickDownColor: "#ff5c5c",
  });
  maSeries = mainChart.addLineSeries({ color: "#5b8def", lineWidth: 2 });
  mainChart.subscribeClick(onChartClick);

  rsiChart = LightweightCharts.createChart($("rsi-chart"), {
    ...chartOptions,
    width: $("rsi-chart").clientWidth,
    height: $("rsi-chart").clientHeight,
  });
  rsiSeries = rsiChart.addLineSeries({ color: "#e7c34d", lineWidth: 1 });

  macdChart = LightweightCharts.createChart($("macd-chart"), {
    ...chartOptions,
    width: $("macd-chart").clientWidth,
    height: $("macd-chart").clientHeight,
  });
  macdHistSeries = macdChart.addHistogramSeries({ color: "#5b8def" });
  macdLineSeries = macdChart.addLineSeries({ color: "#5b8def", lineWidth: 1 });
  macdSignalSeries = macdChart.addLineSeries({ color: "#ff9f43", lineWidth: 1 });

  window.addEventListener("resize", resizeCharts);
}

// ---------- chart tools: crosshair toggle + horizontal line drawing ----------

let drawnLines = [];
let hlineArmed = false;

function onChartClick(param) {
  if (!hlineArmed || !param.point) return;
  const price = candleSeries.coordinateToPrice(param.point.y);
  if (price === null) return;
  const prec = pricePrecision(state.currentSymbol);
  const line = candleSeries.createPriceLine({
    price,
    color: "#ff9f43",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: fmt(price, prec),
  });
  drawnLines.push(line);
  hlineArmed = false;
  $("hline-btn").classList.remove("active");
}

function clearLines() {
  drawnLines.forEach((line) => candleSeries.removePriceLine(line));
  drawnLines = [];
}

function toggleCrosshair() {
  const btn = $("crosshair-btn");
  const enabled = !btn.classList.contains("active");
  btn.classList.toggle("active", enabled);
  mainChart.applyOptions({
    crosshair: {
      vertLine: { visible: enabled, labelVisible: enabled },
      horzLine: { visible: enabled, labelVisible: enabled },
    },
  });
}

function toggleHlineTool() {
  hlineArmed = !hlineArmed;
  $("hline-btn").classList.toggle("active", hlineArmed);
}

function resizeCharts() {
  if (!mainChart) return;
  mainChart.resize($("main-chart").clientWidth, $("main-chart").clientHeight);
  rsiChart.resize($("rsi-chart").clientWidth, $("rsi-chart").clientHeight);
  macdChart.resize($("macd-chart").clientWidth, $("macd-chart").clientHeight);
}

function computeSMA(closes, period) {
  const out = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) continue;
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += closes[j];
    out.push(sum / period);
  }
  return out;
}

function computeEMA(closes, period) {
  const k = 2 / (period + 1);
  const out = [];
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out.push({ idx: period - 1, value: ema });
  for (let i = period; i < closes.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
    out.push({ idx: i, value: ema });
  }
  return out;
}

function computeMACD(closes, fast = 12, slow = 26, signalPeriod = 9) {
  if (closes.length < slow + signalPeriod) return [];
  const emaFast = computeEMA(closes, fast);
  const emaSlow = computeEMA(closes, slow);
  const fastByIdx = new Map(emaFast.map((e) => [e.idx, e.value]));
  const macdSeries = [];
  for (const s of emaSlow) {
    const f = fastByIdx.get(s.idx);
    if (f !== undefined) macdSeries.push({ idx: s.idx, value: f - s.value });
  }
  const macdValues = macdSeries.map((m) => m.value);
  const signalEma = computeEMA(macdValues, signalPeriod);
  return signalEma.map((s) => ({
    idx: macdSeries[s.idx].idx,
    macd: macdSeries[s.idx].value,
    signal: s.value,
    hist: macdSeries[s.idx].value - s.value,
  }));
}

function computeRSI(closes, period = 14) {
  const out = [];
  let gainSum = 0, lossSum = 0;
  for (let i = 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    const gain = Math.max(diff, 0);
    const loss = Math.max(-diff, 0);
    if (i <= period) {
      gainSum += gain;
      lossSum += loss;
      if (i === period) {
        const rs = lossSum === 0 ? 100 : gainSum / period / (lossSum / period);
        out.push({ idx: i, value: 100 - 100 / (1 + rs) });
      }
      continue;
    }
    gainSum = (gainSum * (period - 1) + gain) / period;
    lossSum = (lossSum * (period - 1) + loss) / period;
    const rs = lossSum === 0 ? 100 : gainSum / lossSum;
    out.push({ idx: i, value: 100 - 100 / (1 + rs) });
  }
  return out;
}

async function loadChart(symbol, timeframe) {
  const data = await api(`/api/ohlcv/${symbol}?timeframe=${timeframe}&limit=200`);
  const bars = data.bars || [];
  if (!bars.length) return;

  candleSeries.setData(bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));

  const closes = bars.map((b) => b.close);
  const ma = computeSMA(closes, 20);
  const maOffset = closes.length - ma.length;
  maSeries.setData(ma.map((v, i) => ({ time: bars[i + maOffset].time, value: v })));

  const rsi = computeRSI(closes, 14);
  rsiSeries.setData(rsi.map((r) => ({ time: bars[r.idx].time, value: r.value })));

  const macd = computeMACD(closes);
  macdLineSeries.setData(macd.map((m) => ({ time: bars[m.idx].time, value: m.macd })));
  macdSignalSeries.setData(macd.map((m) => ({ time: bars[m.idx].time, value: m.signal })));
  macdHistSeries.setData(macd.map((m) => ({ time: bars[m.idx].time, value: m.hist, color: m.hist >= 0 ? "#3ddc84" : "#ff5c5c" })));

  mainChart.timeScale().fitContent();
  rsiChart.timeScale().fitContent();
  macdChart.timeScale().fitContent();
}

// ---------- market watch ----------

async function loadSymbols() {
  const data = await api("/api/symbols");
  state.symbols = data.symbols;
  renderMarketWatch();
}

function saveHiddenSymbols() {
  localStorage.setItem("hiddenSymbols", JSON.stringify(state.hiddenSymbols));
}

function toggleWatchEdit() {
  state.watchEditMode = !state.watchEditMode;
  $("watch-edit-btn").classList.toggle("active", state.watchEditMode);
  renderMarketWatch();
}

function removeFromWatch(symbol) {
  if (!state.hiddenSymbols.includes(symbol)) {
    state.hiddenSymbols.push(symbol);
    saveHiddenSymbols();
  }
  renderMarketWatch();
}

function addToWatch(symbol) {
  state.hiddenSymbols = state.hiddenSymbols.filter((s) => s !== symbol);
  saveHiddenSymbols();
  renderMarketWatch();
  openAddSymbolModal();
}

function openAddSymbolModal() {
  const body = state.hiddenSymbols.length === 0
    ? `<p style="color: var(--muted); font-size: 0.8rem; margin: 0;">所有標的都已顯示在行情列表中。</p>`
    : state.hiddenSymbols
        .map((s) => `<div style="display:flex; align-items:center; justify-content:space-between; padding:0.4rem 0;">
          <span>${dispSym(s)}</span>
          <button class="add-symbol-btn" data-symbol="${s}">加入</button>
        </div>`)
        .join("");
  openModal("新增標的", body, async () => {});
  $("modal-confirm").textContent = "關閉";
  $("modal-cancel").classList.add("hidden");
  document.querySelectorAll(".add-symbol-btn").forEach((b) => {
    b.addEventListener("click", () => addToWatch(b.dataset.symbol));
  });
}

function setupSettingsMenu() {
  document.querySelectorAll(".settings-nav-row[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.nav;
      if (target === "journal") { openJournalModal(); return; }
      setMobilePanel(target);
    });
  });
  document.querySelectorAll(".settings-nav-row[data-info]").forEach((btn) => {
    btn.addEventListener("click", () => {
      openModal("提示", `<p style="color: var(--muted); margin: 0;">呢個功能暫時未有開放。</p>`, async () => {});
      $("modal-confirm").textContent = "知道了";
      $("modal-cancel").classList.add("hidden");
    });
  });
}

async function openJournalModal() {
  const data = await api("/api/journal");
  const entries = data.journal || [];
  const body = entries.length
    ? entries.slice().reverse().map((j) => `<div style="padding:0.5rem 0; border-bottom:1px solid var(--border); font-size:0.82rem;">
        <div style="color:var(--muted); font-size:0.72rem;">${new Date(j.time).toLocaleString()}</div>
        <div>${j.message}</div>
      </div>`).join("")
    : `<p style="color: var(--muted); margin: 0;">暫時未有日誌記錄。</p>`;
  openModal("日誌", `<div style="max-height:60vh; overflow-y:auto;">${body}</div>`, async () => {});
  $("modal-confirm").textContent = "關閉";
  $("modal-cancel").classList.add("hidden");
}

function splitPriceDigits(price, prec) {
  if (price === null || price === undefined) return `<span class="digit-normal">-</span>`;
  const str = price.toFixed(prec);
  const [intPart, decPart = ""] = str.split(".");
  const normalCount = Math.max(prec - 3, 0);
  const bigCount = Math.min(prec - normalCount, 2);
  const normal = decPart.slice(0, normalCount);
  const big = decPart.slice(normalCount, normalCount + bigCount);
  const tiny = decPart.slice(normalCount + bigCount);
  const intWithComma = Number(intPart).toLocaleString();
  return `<span class="digit-normal">${intWithComma}${normal ? "." + normal : (big || tiny ? "." : "")}</span><span class="digit-big">${big}</span><span class="digit-tiny">${tiny}</span>`;
}

function renderMarketWatchAdvanced() {
  const wrap = $("market-watch-advanced");
  const list = state.symbols.filter((s) => !state.hiddenSymbols.includes(s));
  wrap.innerHTML = list.map((s) => {
    const p = state.prices[s];
    const prec = pricePrecision(s);
    const tickClass = state.tickDirection[s] === "up" ? "tick-up" : state.tickDirection[s] === "down" ? "tick-down" : "";
    const t = state.tickTime[s];
    const timeStr = t ? `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}:${String(t.getSeconds()).padStart(2, "0")}` : "-";
    const spreadPts = p ? Math.round((p.ask - p.bid) * Math.pow(10, prec)) : "-";
    const hl = state.dayHiLo[s];
    return `<div class="watch-card" data-symbol="${s}">
      <div class="wc-time">${timeStr}</div>
      <div class="wc-symbol">${dispSym(s)}</div>
      <div class="wc-price ${tickClass}">${splitPriceDigits(p ? p.bid : null, prec)}</div>
      <div class="wc-price ${tickClass}">${splitPriceDigits(p ? p.ask : null, prec)}</div>
      <div class="wc-meta-label">點差: ${spreadPts}</div>
      <div class="wc-meta-hilo">最低: <span>${hl ? fmt(hl.low, prec) : "-"}</span>　最高: <span>${hl ? fmt(hl.high, prec) : "-"}</span></div>
    </div>`;
  }).join("");
  wrap.querySelectorAll(".watch-card").forEach((card) => {
    card.addEventListener("click", () => {
      selectSymbol(card.dataset.symbol);
      if (isMobileView()) setMobilePanel("chart-area");
    });
  });
}

async function loadAllDayHiLo() {
  await Promise.all(state.symbols.map(async (s) => {
    const data = await api(`/api/ohlcv/${s}?timeframe=1d&limit=1`);
    const bar = (data.bars || [])[0];
    if (bar) state.dayHiLo[s] = { high: bar.high, low: bar.low };
  }));
  if (state.watchViewMode === "advanced") renderMarketWatchAdvanced();
}

function setupWatchViewToggle() {
  document.querySelectorAll(".watch-view-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".watch-view-btn").forEach((b) => b.classList.toggle("active", b === btn));
      state.watchViewMode = btn.dataset.mode;
      $("market-watch-table").classList.toggle("hidden", state.watchViewMode !== "simple");
      $("market-watch-advanced").classList.toggle("hidden", state.watchViewMode !== "advanced");
      if (state.watchViewMode === "advanced") {
        renderMarketWatchAdvanced();
        loadAllDayHiLo();
      }
    });
  });
}

function renderMarketWatch() {
  const tbody = $("market-watch-body");
  const list = state.watchEditMode ? state.symbols : state.symbols.filter((s) => !state.hiddenSymbols.includes(s));
  tbody.innerHTML = list
    .map((s) => {
      const p = state.prices[s];
      const active = s === state.currentSymbol ? ' style="color: var(--accent)"' : "";
      const prec = pricePrecision(s);
      const spread = p ? fmt(p.ask - p.bid, prec) : "-";
      const tickClass = state.tickDirection[s] === "up" ? "tick-up" : state.tickDirection[s] === "down" ? "tick-down" : "";
      if (state.watchEditMode) {
        return `<tr data-symbol="${s}"><td colspan="3">${dispSym(s)}</td><td><button class="watch-remove-btn" data-symbol="${s}">&#8722;</button></td></tr>`;
      }
      return `<tr data-symbol="${s}"><td${active}>${dispSym(s)}</td><td class="${tickClass}">${p ? fmt(p.bid, prec) : "-"}</td><td class="${tickClass}">${p ? fmt(p.ask, prec) : "-"}</td><td>${spread}</td></tr>`;
    })
    .join("");
  if (state.watchViewMode === "advanced") renderMarketWatchAdvanced();
  if (state.watchEditMode) {
    tbody.querySelectorAll(".watch-remove-btn").forEach((b) => {
      b.addEventListener("click", (e) => { e.stopPropagation(); removeFromWatch(b.dataset.symbol); });
    });
  } else {
    tbody.querySelectorAll("tr").forEach((tr) => {
      tr.addEventListener("click", () => {
        selectSymbol(tr.dataset.symbol);
        if (isMobileView()) setMobilePanel("chart-area");
      });
    });
  }
}

const TF_LABELS = { "1m": "M1", "5m": "M5", "15m": "M15", "1h": "H1", "4h": "H4", "1d": "D1" };

function updateChartLabels() {
  const tfLabel = TF_LABELS[state.timeframe] || state.timeframe;
  $("tf-select-btn").textContent = tfLabel;
  $("chart-symbol-label").textContent = `${dispSym(state.currentSymbol)}, ${tfLabel}`;
}

function openTimeframePicker() {
  const actions = Object.entries(TF_LABELS).map(([tf, label]) => ({
    label,
    onClick: () => {
      state.timeframe = tf;
      updateChartLabels();
      loadChart(state.currentSymbol, state.timeframe);
    },
  }));
  openActionSheet("選擇時間週期", actions);
}

function selectSymbol(symbol) {
  state.currentSymbol = symbol;
  updateChartLabels();
  $("order-symbol-name").textContent = dispSym(symbol);
  state.sparkline = [];
  state.orderSl = null;
  state.orderTp = null;
  renderTicketField("sl");
  renderTicketField("tp");
  renderMarketWatch();
  updateOneClickPrices();
  loadChart(symbol, state.timeframe);
  loadDayHiLo(symbol);
  seedSparkline(symbol);
  clearLines();
}

async function seedSparkline(symbol) {
  const data = await api(`/api/ohlcv/${symbol}?timeframe=1m&limit=90`);
  if (state.currentSymbol !== symbol) return; // symbol changed again while awaiting
  const bars = data.bars || [];
  if (!bars.length) return;
  const halfSpread = SPREAD_BPS_CLIENT / 10000;
  const seeded = bars.map((b) => ({
    bid: b.close * (1 - halfSpread),
    ask: b.close * (1 + halfSpread),
  }));
  state.sparkline = seeded.concat(state.sparkline).slice(-90);
  drawSparkline();
}

function updateOneClickPrices() {
  const p = state.prices[state.currentSymbol];
  const prec = pricePrecision(state.currentSymbol);
  $("oneclick-bid").textContent = p ? fmt(p.bid, prec) : "-";
  $("oneclick-ask").textContent = p ? fmt(p.ask, prec) : "-";
  renderMobileQuote();
}

function renderMobileQuote() {
  const p = state.prices[state.currentSymbol];
  const prec = pricePrecision(state.currentSymbol);
  const dir = state.tickDirection[state.currentSymbol];
  const cls = "mq-num" + (dir === "down" ? " tick-down" : "");
  $("mq-bid").textContent = p ? fmt(p.bid, prec) : "-";
  $("mq-bid").className = cls;
  $("mq-ask").textContent = p ? fmt(p.ask, prec) : "-";
  $("mq-ask").className = cls;
  if (p) pushSparklinePoint(p.bid, p.ask);
}

function pushSparklinePoint(bid, ask) {
  state.sparkline.push({ bid, ask });
  if (state.sparkline.length > 90) state.sparkline.shift();
  drawSparkline();
}

function drawSparkline() {
  const svg = $("order-sparkline");
  const data = state.sparkline;
  if (!svg || data.length < 2) return;
  const prec = pricePrecision(state.currentSymbol);
  const all = data.flatMap((d) => [d.bid, d.ask]);
  const min = Math.min(...all);
  const max = Math.max(...all);
  const range = (max - min) || 1;
  const last = data[data.length - 1];
  const w = 300, h = 120, padY = 8;
  const maxLabelLen = Math.max(fmt(min, prec).length, fmt(max, prec).length, fmt(last.bid, prec).length, fmt(last.ask, prec).length);
  const padRight = Math.max(46, maxLabelLen * 5.6 + 14);
  const plotW = w - padRight;
  const xAt = (i) => (i / (data.length - 1)) * plotW;
  const yAt = (v) => padY + (1 - (v - min) / range) * (h - padY * 2);

  const line = (key) => data.map((d, i) => `${xAt(i).toFixed(1)},${yAt(d[key]).toFixed(1)}`).join(" ");

  const gridCount = 8;
  let gridLines = "";
  for (let i = 0; i <= gridCount; i++) {
    const v = min + (range * i) / gridCount;
    const y = yAt(v);
    gridLines += `<line x1="0" y1="${y.toFixed(1)}" x2="${plotW}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 3"/>`;
    gridLines += `<text x="${plotW + 6}" y="${(y + 3).toFixed(1)}" font-size="9" fill="var(--muted)">${fmt(v, prec)}</text>`;
  }

  const bidY = yAt(last.bid), askY = yAt(last.ask);

  svg.innerHTML = `
    ${gridLines}
    <polyline points="${line("ask")}" fill="none" stroke="var(--red)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    <polyline points="${line("bid")}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    <line x1="0" y1="${askY.toFixed(1)}" x2="${plotW}" y2="${askY.toFixed(1)}" stroke="var(--red)" stroke-width="1" stroke-dasharray="3 2" opacity="0.6"/>
    <line x1="0" y1="${bidY.toFixed(1)}" x2="${plotW}" y2="${bidY.toFixed(1)}" stroke="var(--accent)" stroke-width="1" stroke-dasharray="3 2" opacity="0.6"/>
    <rect x="${plotW + 2}" y="${(askY - 7).toFixed(1)}" width="${padRight - 4}" height="14" fill="var(--red)"/>
    <text x="${plotW + 6}" y="${(askY + 3).toFixed(1)}" font-size="9" font-weight="700" fill="white">${fmt(last.ask, prec)}</text>
    <rect x="${plotW + 2}" y="${(bidY - 7).toFixed(1)}" width="${padRight - 4}" height="14" fill="var(--accent)"/>
    <text x="${plotW + 6}" y="${(bidY + 3).toFixed(1)}" font-size="9" font-weight="700" fill="white">${fmt(last.bid, prec)}</text>
  `;
}

async function loadDayHiLo(symbol) {
  const data = await api(`/api/ohlcv/${symbol}?timeframe=1d&limit=1`);
  const bar = (data.bars || [])[0];
  const prec = pricePrecision(symbol);
  $("day-high").textContent = bar ? fmt(bar.high, prec) : "-";
  $("day-low").textContent = bar ? fmt(bar.low, prec) : "-";
}

// ---------- orders (admin only — server also enforces this) ----------

function setOrderMode(mode) {
  state.orderMode = mode;
  document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("pending-fields").classList.toggle("hidden", mode !== "pending");
  $("submit-order-btn").classList.toggle("hidden", mode !== "pending");
  $("order-msg").textContent = mode === "market" ? "使用上方買進／賣出按鈕送出市價單" : "";
}

async function submitMarketOrder(side) {
  const qty = parseFloat($("qty-input").value);
  if (!qty || qty <= 0) { $("order-msg").textContent = "請輸入有效數量"; return; }
  const sl = parseFloat($("sl-input").value) || null;
  const tp = parseFloat($("tp-input").value) || null;
  const result = await post("/api/order/market", { symbol: state.currentSymbol, side, qty, sl, tp });
  $("order-msg").textContent = result.ok ? `#${result.ticket} 市價單成交` : `錯誤：${result.error}`;
  refreshAll();
}

// ---------- mobile order ticket ----------

function setupOrderTicket() {
  $("order-back-btn").addEventListener("click", () => setMobilePanel("market-watch"));
  $("order-symbol-btn").addEventListener("click", openSymbolSwitcher);

  document.querySelectorAll(".qty-step-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const step = parseFloat(btn.dataset.step);
      state.orderQty = Math.max(0.01, Math.round((state.orderQty + step) * 100) / 100);
      $("qty-display").value = state.orderQty.toFixed(2);
    });
  });

  $("qty-display").addEventListener("change", () => {
    const v = parseFloat($("qty-display").value);
    state.orderQty = v > 0 ? Math.round(v * 100) / 100 : 0.01;
    $("qty-display").value = state.orderQty.toFixed(2);
  });

  document.querySelectorAll(".ticket-field-row").forEach((row) => {
    const field = row.dataset.field;
    row.querySelector(".minus").addEventListener("click", () => adjustTicketField(field, -1));
    row.querySelector(".plus").addEventListener("click", () => adjustTicketField(field, 1));
    row.querySelector(".ticket-field-value").addEventListener("click", () => clearTicketField(field));
  });

  $("mq-sell-btn").addEventListener("click", () => submitMobileMarketOrder("sell"));
  $("mq-buy-btn").addEventListener("click", () => submitMobileMarketOrder("buy"));
}

function ticketFieldStep(field) {
  if (field === "dev") return 1;
  return Math.pow(10, -pricePrecision(state.currentSymbol)) * 10;
}

function adjustTicketField(field, dir) {
  const step = ticketFieldStep(field);
  const key = field === "sl" ? "orderSl" : field === "tp" ? "orderTp" : "orderDeviation";
  if (state[key] === null) {
    const p = state.prices[state.currentSymbol];
    state[key] = field === "dev" ? 0 : (p ? p.bid : 0);
  }
  state[key] = Math.max(0, state[key] + step * dir);
  renderTicketField(field);
}

function clearTicketField(field) {
  const key = field === "sl" ? "orderSl" : field === "tp" ? "orderTp" : "orderDeviation";
  state[key] = null;
  renderTicketField(field);
}

function renderTicketField(field) {
  const row = document.querySelector(`.ticket-field-row[data-field="${field}"]`);
  if (!row) return;
  const key = field === "sl" ? "orderSl" : field === "tp" ? "orderTp" : "orderDeviation";
  const val = state[key];
  const valueEl = row.querySelector(".ticket-field-value");
  if (val === null) {
    valueEl.textContent = "沒有設置";
    valueEl.classList.remove("set");
  } else {
    valueEl.textContent = field === "dev" ? fmt(val, 0) : fmt(val, pricePrecision(state.currentSymbol));
    valueEl.classList.add("set");
  }
}

function openSymbolSwitcher() {
  const actions = state.symbols
    .filter((s) => !state.hiddenSymbols.includes(s))
    .map((s) => ({
      label: dispSym(s),
      onClick: () => { selectSymbol(s); },
    }));
  openActionSheet("選擇標的", actions);
}

async function submitMobileMarketOrder(side) {
  const qty = parseFloat($("qty-display").value) || state.orderQty;
  if (!qty || qty <= 0) { $("order-mobile-msg").textContent = "請輸入有效數量"; return; }
  const result = await post("/api/order/market", {
    symbol: state.currentSymbol, side, qty, sl: state.orderSl, tp: state.orderTp,
  });
  $("order-mobile-msg").textContent = result.ok ? `#${result.ticket} 市價單成交` : `錯誤：${result.error}`;
  refreshAll();
}

async function submitPendingOrder() {
  const qty = parseFloat($("qty-input").value);
  const price = parseFloat($("pending-price").value);
  if (!qty || qty <= 0) { $("order-msg").textContent = "請輸入有效數量"; return; }
  if (!price || price <= 0) { $("order-msg").textContent = "請輸入觸發價格"; return; }
  const sl = parseFloat($("sl-input").value) || null;
  const tp = parseFloat($("tp-input").value) || null;
  const result = await post("/api/order/pending", {
    symbol: state.currentSymbol, order_type: $("pending-type").value, qty, trigger_price: price, sl, tp,
  });
  $("order-msg").textContent = result.ok ? `#${result.ticket} 掛單已送出` : `錯誤：${result.error}`;
  refreshAll();
}

async function cancelPending(ticket) {
  await post("/api/pending/cancel", { ticket: parseInt(ticket, 10) });
  refreshAll();
}

function openModal(title, fieldsHTML, onConfirm) {
  $("modal-title").textContent = title;
  $("modal-fields").innerHTML = fieldsHTML;
  $("modal-overlay").classList.remove("hidden");
  const confirmBtn = $("modal-confirm");
  const cancelBtn = $("modal-cancel");
  confirmBtn.textContent = "確認";
  confirmBtn.className = "";
  cancelBtn.classList.remove("hidden");
  const cleanup = () => {
    $("modal-overlay").classList.add("hidden");
    confirmBtn.removeEventListener("click", onConfirmWrapped);
    cancelBtn.removeEventListener("click", onCancel);
  };
  const onConfirmWrapped = async () => { await onConfirm(); cleanup(); };
  const onCancel = () => cleanup();
  confirmBtn.addEventListener("click", onConfirmWrapped);
  cancelBtn.addEventListener("click", onCancel);
}

function openCloseModal(pos) {
  const prec = pricePrecision(pos.symbol);
  $("modal-title").textContent = `平倉 #${pos.ticket} ${dispSym(pos.symbol)}`;
  $("modal-fields").innerHTML = `
    <div id="close-quote">
      <span id="close-bid" class="quote-num"></span>
      <span id="close-ask" class="quote-num"></span>
    </div>
    <label>平倉數量</label>
    <input id="field-qty" type="number" step="any" value="${pos.qty}">
  `;
  $("modal-overlay").classList.remove("hidden");

  const confirmBtn = $("modal-confirm");
  const cancelBtn = $("modal-cancel");
  const qtyInput = $("field-qty");

  const updatePreview = () => {
    const p = state.prices[pos.symbol];
    const qty = parseFloat(qtyInput.value) || 0;
    let pnl = 0;
    if (p) {
      const exitPrice = pos.side === "buy" ? p.bid : p.ask;
      const sign = pos.side === "buy" ? 1 : -1;
      pnl = (exitPrice - pos.entry_price) * qty * sign;
    }
    $("close-bid").textContent = p ? fmt(p.bid, prec) : "-";
    $("close-ask").textContent = p ? fmt(p.ask, prec) : "-";
    confirmBtn.textContent = `平倉 ${pnl >= 0 ? "獲利" : "虧損"} ${fmt(pnl)}`;
    confirmBtn.className = pnl >= 0 ? "profit" : "loss";
  };

  const cleanup = () => {
    $("modal-overlay").classList.add("hidden");
    confirmBtn.className = "";
    confirmBtn.disabled = false;
    qtyInput.removeEventListener("input", updatePreview);
    confirmBtn.removeEventListener("click", onConfirm);
    cancelBtn.removeEventListener("click", onCancel);
  };

  const onConfirm = async () => {
    confirmBtn.disabled = true;
    const qty = parseFloat(qtyInput.value) || null;
    const result = await post("/api/position/close", { ticket: pos.ticket, qty });
    confirmBtn.textContent = result.ok ? `已平倉 ✓ ${fmt(result.pnl)}` : `錯誤：${result.error}`;
    refreshAll();
    setTimeout(cleanup, 2000);
  };
  const onCancel = () => cleanup();

  qtyInput.addEventListener("input", updatePreview);
  confirmBtn.addEventListener("click", onConfirm);
  cancelBtn.addEventListener("click", onCancel);
  updatePreview();
}

function openModifyModal(pos) {
  openModal(`修改 #${pos.ticket} ${dispSym(pos.symbol)} SL/TP`, `
    <label>停損 SL</label>
    <input id="field-sl" type="number" step="any" value="${pos.sl ?? ""}">
    <label>停利 TP</label>
    <input id="field-tp" type="number" step="any" value="${pos.tp ?? ""}">
  `, async () => {
    const sl = parseFloat($("field-sl").value) || null;
    const tp = parseFloat($("field-tp").value) || null;
    await post("/api/position/modify", { ticket: pos.ticket, sl, tp });
    refreshAll();
  });
}

// ---------- terminal panels ----------

async function refreshAccount() {
  if (!state.token) return;
  const a = await api("/api/account");
  if (a.error) return;
  renderAccount(a);
}

function renderAccount(a) {
  state.lastAccountSummary = a;
  $("acc-balance").textContent = fmt(a.balance);
  $("acc-equity").textContent = fmt(a.equity);
  $("acc-floating").textContent = fmt(a.floating_pnl);
  $("acc-floating").className = pnlClass(a.floating_pnl);
  $("acc-margin-used").textContent = fmt(a.margin_used);
  $("acc-free-margin").textContent = fmt(a.free_margin);
  $("acc-margin-level").textContent = a.margin_level === null ? "-" : fmt(a.margin_level) + "%";
  $("acc-leverage").textContent = "1:" + a.leverage;
}

async function refreshPositions() {
  if (!state.token) return;
  const data = await api("/api/positions");
  renderPositions(data.positions);
}

function renderPositions(positions) {
  state.lastPositions = positions;
  const isAdmin = state.role === "admin";
  const tbody = document.querySelector("#trade-table tbody");
  tbody.innerHTML = positions
    .map((p) => `<tr>
      <td>#${p.ticket}</td><td>${dispSym(p.symbol)}</td>
      <td>${p.side === "buy" ? "買" : "賣"}</td><td>${fmt(p.qty, 4)}</td>
      <td>${fmt(p.entry_price, pricePrecision(p.symbol))}</td><td>${p.sl ? fmt(p.sl, pricePrecision(p.symbol)) : "-"}</td><td>${p.tp ? fmt(p.tp, pricePrecision(p.symbol)) : "-"}</td>
      <td class="${pnlClass(p.floating_pnl || 0)}">${fmt(p.floating_pnl)}</td>
      <td>${isAdmin ? `<button class="modify-btn" data-ticket="${p.ticket}">改</button> <button class="close-btn" data-ticket="${p.ticket}">平倉</button>` : ""}</td>
    </tr>`)
    .join("");
  if (isAdmin) {
    tbody.querySelectorAll(".close-btn").forEach((b) => {
      const pos = positions.find((p) => p.ticket === parseInt(b.dataset.ticket, 10));
      b.addEventListener("click", () => openCloseModal(pos));
    });
    tbody.querySelectorAll(".modify-btn").forEach((b) => {
      const pos = positions.find((p) => p.ticket === parseInt(b.dataset.ticket, 10));
      b.addEventListener("click", () => openModifyModal(pos));
    });
  }
}

async function renderPositionsPanel() {
  // Runs on every price tick, so it reads the account the websocket already
  // pushed rather than issuing its own request each time.
  const zero = { balance: 0, equity: 0, margin_used: 0, free_margin: 0, margin_level: 0, floating_pnl: 0 };
  const account = (state.token && state.lastAccountSummary) ? state.lastAccountSummary : zero;
  $("positions-pnl-text").textContent = `${fmt(account.floating_pnl)} USD`;
  $("positions-header").className = account.floating_pnl >= 0 ? "pnl-pos-bg" : "pnl-neg-bg";
  $("pp-balance").textContent = fmt(account.balance);
  $("pp-equity").textContent = fmt(account.equity);
  $("pp-margin").textContent = fmt(account.margin_used);
  $("pp-free-margin").textContent = fmt(account.free_margin);
  $("pp-margin-level").textContent = account.margin_level === null ? "-" : fmt(account.margin_level);

  const positions = state.token ? (state.lastPositions || []) : [];
  $("positions-list-label").classList.toggle("hidden", positions.length === 0);
  const list = $("positions-list");
  list.innerHTML = positions.length
    ? positions.map((p) => {
        const prec = pricePrecision(p.symbol);
        const sideClass = p.side === "buy" ? "side-buy" : "side-sell";
        const cur = state.prices[p.symbol];
        const curPrice = cur ? (p.side === "buy" ? cur.bid : cur.ask) : null;
        const sign = p.side === "buy" ? 1 : -1;
        const livePnl = curPrice !== null ? (curPrice - p.entry_price) * p.qty * sign : (p.floating_pnl || 0);
        const ot = new Date(p.open_time);
        const otStr = `${ot.getFullYear()}.${String(ot.getMonth() + 1).padStart(2, "0")}.${String(ot.getDate()).padStart(2, "0")} ${String(ot.getHours()).padStart(2, "0")}:${String(ot.getMinutes()).padStart(2, "0")}:${String(ot.getSeconds()).padStart(2, "0")}`;
        const expanded = state.expandedTicket === p.ticket ? "" : "hidden";
        return `<div class="position-card" data-ticket="${p.ticket}">
          <div class="position-row-top">
            <span>${dispSym(p.symbol)}, <span class="${sideClass}">${p.side} ${fmt(p.qty, 4)}</span></span>
            <span class="${pnlClass(livePnl)}">${fmt(livePnl)}</span>
          </div>
          <div class="position-row-bottom">${fmt(p.entry_price, prec)} &rarr; ${curPrice !== null ? fmt(curPrice, prec) : "-"}</div>
          <div class="position-detail ${expanded}">
            <div class="position-open-time">${otStr}</div>
            <div class="position-detail-grid">
              <div><span>止損：</span><span>${p.sl ? fmt(p.sl, prec) : "-"}</span></div>
              <div><span>庫存費：</span><span>0.00</span></div>
              <div><span>獲利：</span><span>${p.tp ? fmt(p.tp, prec) : "-"}</span></div>
              <div><span>稅費：</span><span>0.00</span></div>
              <div><span>ID：</span><span>${p.ticket}</span></div>
              <div><span>手續費：</span><span>0.00</span></div>
            </div>
          </div>
        </div>`;
      }).join("")
    : `<div class="empty-state"><svg viewBox="0 0 24 24"><polyline points="7 3 3 7 7 11"/><line x1="3" y1="7" x2="21" y2="7"/><polyline points="17 13 21 17 17 21"/><line x1="21" y1="17" x2="3" y2="17"/></svg></div>`;

  list.querySelectorAll(".position-card").forEach((card) => {
    card.addEventListener("click", () => {
      const ticket = parseInt(card.dataset.ticket, 10);
      state.expandedTicket = ticket;
      list.querySelectorAll(".position-detail").forEach((d) => d.classList.add("hidden"));
      card.querySelector(".position-detail").classList.remove("hidden");
      openPositionActionSheet(ticket);
    });
  });
}

function openActionSheet(title, actions) {
  const overlay = document.createElement("div");
  overlay.id = "action-sheet-overlay";
  overlay.innerHTML = `
    <div id="action-sheet-box">
      <div id="action-sheet-title">${title}</div>
      ${actions.map((a, i) => `<button class="action-sheet-btn ${a.className || ""}" data-idx="${i}">${a.label}</button>`).join("")}
      <button id="action-sheet-cancel">取消</button>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => document.body.removeChild(overlay);
  actions.forEach((a, i) => {
    overlay.querySelector(`[data-idx="${i}"]`).addEventListener("click", () => { close(); a.onClick(); });
  });
  overlay.querySelector("#action-sheet-cancel").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
}

function openPositionActionSheet(ticket) {
  const pos = (state.lastPositions || []).find((p) => p.ticket === ticket);
  if (!pos) return;
  const isAdmin = state.role === "admin";
  const actions = [];
  if (isAdmin) {
    actions.push({ label: "平倉", className: "danger", onClick: () => openCloseModal(pos) });
    actions.push({ label: "修改", onClick: () => openModifyModal(pos) });
  }
  actions.push({ label: "交易", onClick: () => { selectSymbol(pos.symbol); setMobilePanel("order-panel"); } });
  actions.push({ label: "圖表", onClick: () => { selectSymbol(pos.symbol); setMobilePanel("chart-area"); } });
  openActionSheet(`#${pos.ticket} ${dispSym(pos.symbol)}`, actions);
}

async function refreshPending() {
  if (!state.token) return;
  const data = await api("/api/pending");
  renderPending(data.pending);
}

function renderPending(pending) {
  const isAdmin = state.role === "admin";
  const tbody = document.querySelector("#pending-table tbody");
  tbody.innerHTML = pending
    .map((p) => `<tr>
      <td>#${p.ticket}</td><td>${dispSym(p.symbol)}</td><td>${p.order_type}</td>
      <td>${fmt(p.qty, 4)}</td><td>${fmt(p.trigger_price, pricePrecision(p.symbol))}</td>
      <td>${isAdmin ? `<button class="cancel-btn" data-ticket="${p.ticket}">取消</button>` : ""}</td>
    </tr>`)
    .join("");
  if (isAdmin) {
    tbody.querySelectorAll(".cancel-btn").forEach((b) => b.addEventListener("click", () => cancelPending(b.dataset.ticket)));
  }
}

const STARTING_DEPOSIT = 100000;

async function refreshHistory() {
  if (!state.token) return;
  const data = await api("/api/history");
  state.lastHistory = data.history || [];
  const tbody = document.querySelector("#history-table tbody");
  const reasonLabel = { manual: "手動", sl: "停損", tp: "停利", stop_out: "強制平倉" };
  tbody.innerHTML = state.lastHistory
    .map((h) => `<tr>
      <td>#${h.ticket}</td><td>${dispSym(h.symbol)}</td><td>${h.side === "buy" ? "買" : "賣"}</td>
      <td>${fmt(h.qty, 4)}</td><td>${fmt(h.entry_price, pricePrecision(h.symbol))}</td><td>${fmt(h.close_price, pricePrecision(h.symbol))}</td>
      <td class="${pnlClass(h.pnl)}">${fmt(h.pnl)}</td><td>${reasonLabel[h.reason] || h.reason}</td>
    </tr>`)
    .join("");

  // The websocket keeps this fresh; only fetch on the very first load.
  let account = state.lastAccountSummary;
  if (!account) {
    const res = await api("/api/account");
    if (!res.error) account = state.lastAccountSummary = res;
  }
  if (account) fillHistorySummary("ha", account);

  if ($("history-mobile-panel").classList.contains("mobile-active")) renderHistoryMobilePanel();
}

function fillHistorySummary(prefix, account, loggedIn = true) {
  const deposit = loggedIn ? STARTING_DEPOSIT : 0;
  const profit = account.balance - deposit;
  $(`${prefix}-profit`).textContent = fmt(profit);
  $(`${prefix}-profit`).className = pnlClass(profit);
  $(`${prefix}-credit`).textContent = fmt(0);
  $(`${prefix}-deposit`).textContent = fmt(deposit);
  $(`${prefix}-withdrawal`).textContent = fmt(0);
  $(`${prefix}-balance`).textContent = fmt(account.balance);
}

function historyRangeStart(range) {
  const now = new Date();
  if (range === "day") return new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (range === "week") {
    const d = new Date(now);
    const day = (d.getDay() + 6) % 7; // Monday-start week
    d.setDate(d.getDate() - day);
    d.setHours(0, 0, 0, 0);
    return d;
  }
  if (range === "month") return new Date(now.getFullYear(), now.getMonth(), 1);
  if (range === "custom") return state.historyCustomStart ? new Date(state.historyCustomStart + "T00:00:00") : null;
  return null;
}

function historyRangeEnd(range) {
  if (range === "custom" && state.historyCustomEnd) {
    return new Date(state.historyCustomEnd + "T23:59:59");
  }
  return null;
}

function renderHistoryMobilePanel() {
  const rangeStart = historyRangeStart(state.historyRange);
  const rangeEnd = historyRangeEnd(state.historyRange);
  const query = state.historySearch.trim().toUpperCase();
  const rows = state.lastHistory.filter((h) => {
    const closeTime = new Date(h.close_time);
    if (rangeStart && closeTime < rangeStart) return false;
    if (rangeEnd && closeTime > rangeEnd) return false;
    if (query && !h.symbol.toUpperCase().includes(query)) return false;
    return true;
  });

  const list = $("history-mobile-list");
  list.innerHTML = rows.length
    ? rows.map((h) => {
        const prec = pricePrecision(h.symbol);
        const sideClass = h.side === "buy" ? "side-buy" : "side-sell";
        const dt = new Date(h.close_time);
        const dtStr = `${dt.getFullYear()}.${String(dt.getMonth() + 1).padStart(2, "0")}.${String(dt.getDate()).padStart(2, "0")} ${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}:${String(dt.getSeconds()).padStart(2, "0")}`;
        return `<div class="history-card">
          <div class="history-row-top">
            <span>${dispSym(h.symbol)}, <span class="${sideClass}">${h.side} ${fmt(h.qty, 4)}</span></span>
            <span class="history-date">${dtStr}</span>
          </div>
          <div class="history-row-bottom">
            <span>${fmt(h.entry_price, prec)} &rarr; ${fmt(h.close_price, prec)}</span>
            <span class="history-pnl ${pnlClass(h.pnl)}">${fmt(h.pnl)}</span>
          </div>
        </div>`;
      }).join("")
    : `<div class="empty-state"><svg viewBox="0 0 24 24"><rect x="6" y="4" width="12" height="17" rx="1.5"/><rect x="9" y="2" width="6" height="4" rx="1"/><line x1="9" y1="11" x2="15" y2="11"/><line x1="9" y1="15" x2="15" y2="15"/></svg><span>無歷史數據</span></div>`;

  const account = state.token && state.lastAccountSummary ? state.lastAccountSummary : { balance: 0 };
  fillHistorySummary("hm", account, state.token);
}

function setupHistoryPanel() {
  document.querySelectorAll(".hist-filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".hist-filter-btn").forEach((b) => b.classList.toggle("active", b === btn));
      state.historyRange = btn.dataset.range;
      $("history-custom-range").classList.toggle("hidden", btn.dataset.range !== "custom");
      if (btn.dataset.range === "custom" && !state.historyCustomStart) {
        const today = new Date();
        const monthAgo = new Date(today);
        monthAgo.setDate(monthAgo.getDate() - 30);
        state.historyCustomStart = monthAgo.toISOString().slice(0, 10);
        state.historyCustomEnd = today.toISOString().slice(0, 10);
        $("history-custom-start").value = state.historyCustomStart;
        $("history-custom-end").value = state.historyCustomEnd;
      }
      renderHistoryMobilePanel();
    });
  });
  $("history-search-input").addEventListener("input", (e) => {
    state.historySearch = e.target.value;
    renderHistoryMobilePanel();
  });
  $("history-custom-ok").addEventListener("click", () => {
    state.historyCustomStart = $("history-custom-start").value || null;
    state.historyCustomEnd = $("history-custom-end").value || null;
    renderHistoryMobilePanel();
  });
}

async function refreshJournal() {
  if (!state.token) return;
  const data = await api("/api/journal");
  const panel = $("journal-panel");
  panel.innerHTML = data.journal
    .map((j) => `<div class="entry"><time>${new Date(j.time).toLocaleTimeString()}</time>${j.message}</div>`)
    .join("");
}

function refreshAll() {
  if (!state.token) return;
  refreshAccount();
  refreshPositions().then(() => {
    if ($("positions-panel").classList.contains("mobile-active")) renderPositionsPanel();
  });
  refreshPending();
  refreshHistory();
  refreshJournal();
}

// ---------- websocket ----------

function sendWsAuth() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ token: state.token || null }));
  }
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = sendWsAuth;
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    const newPrices = data.prices;
    for (const sym in newPrices) {
      const old = state.prices[sym];
      if (old) {
        if (newPrices[sym].bid > old.bid) state.tickDirection[sym] = "up";
        else if (newPrices[sym].bid < old.bid) state.tickDirection[sym] = "down";
        if (newPrices[sym].bid !== old.bid || newPrices[sym].ask !== old.ask) state.tickTime[sym] = new Date();
      } else {
        state.tickTime[sym] = new Date();
      }
    }
    state.prices = newPrices;
    renderMarketWatch();
    updateOneClickPrices();

    // Fund state arrives on the same tick for logged-in sockets, so the client
    // no longer polls /api/account, /api/positions or /api/pending at all.
    if (data.account) {
      renderAccount(data.account);
      renderPositions(data.positions);
      renderPending(data.pending);
      // History and the journal only move when a trade actually opens/closes.
      if (data.changed) {
        refreshHistory();
        refreshJournal();
      }
    }
    if ($("positions-panel").classList.contains("mobile-active")) renderPositionsPanel();
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

// ---------- tabs ----------

function setupTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
      $(`${btn.dataset.tab}-table`)?.classList.remove("hidden");
      $(`${btn.dataset.tab}-panel`)?.classList.remove("hidden");
    });
  });
}

// ---------- mobile bottom nav ----------

function isMobileView() {
  return window.matchMedia("(max-width: 768px)").matches;
}

function setMobilePanel(panelId) {
  document.querySelectorAll("#header, #market-watch, #chart-area, #order-panel, #positions-panel, #history-mobile-panel")
    .forEach((el) => el.classList.toggle("mobile-active", el.id === panelId));
  document.querySelectorAll(".nav-btn")
    .forEach((b) => b.classList.toggle("active", b.dataset.panel === panelId));
  if (panelId === "chart-area") {
    requestAnimationFrame(resizeCharts);
  }
  if (panelId === "positions-panel") {
    renderPositionsPanel();
  }
  if (panelId === "history-mobile-panel") {
    renderHistoryMobilePanel();
  }
  if (panelId === "order-panel") {
    $("order-symbol-name").textContent = dispSym(state.currentSymbol);
    renderTicketField("sl");
    renderTicketField("tp");
    renderTicketField("dev");
    $("qty-display").value = state.orderQty.toFixed(2);
    renderMobileQuote();
    if (state.sparkline.length < 2) seedSparkline(state.currentSymbol);
  }
}

function setupMobileNav() {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => setMobilePanel(btn.dataset.panel));
  });
  setMobilePanel("market-watch");
}

// ---------- init ----------

$("login-btn").addEventListener("click", login);
$("logout-btn").addEventListener("click", logout);
$("password-input").addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
$("oneclick-buy").addEventListener("click", () => submitMarketOrder("buy"));
$("oneclick-sell").addEventListener("click", () => submitMarketOrder("sell"));
$("submit-order-btn").addEventListener("click", submitPendingOrder);
document.querySelectorAll(".mode-btn").forEach((b) => b.addEventListener("click", () => setOrderMode(b.dataset.mode)));
$("tf-select-btn").addEventListener("click", openTimeframePicker);
$("crosshair-btn").addEventListener("click", toggleCrosshair);
$("hline-btn").addEventListener("click", toggleHlineTool);
$("clear-lines-btn").addEventListener("click", clearLines);
$("watch-edit-btn").addEventListener("click", toggleWatchEdit);
$("watch-add-btn").addEventListener("click", openAddSymbolModal);
$("pp-add-btn").addEventListener("click", () => setMobilePanel("order-panel"));

(async function init() {
  initCharts();
  setupTabs();
  setupHistoryPanel();
  setupOrderTicket();
  setupWatchViewToggle();
  setupSettingsMenu();
  setupMobileNav();
  setOrderMode("market");
  await loadSymbols();
  selectSymbol(state.currentSymbol);
  if (state.token) {
    showLoggedIn();
    refreshAll();
  } else {
    showLoggedOut();
  }
  connectWs();
  setInterval(() => loadChart(state.currentSymbol, state.timeframe), 10000);
  // Live state is pushed over the websocket; this is only a slow safety net
  // in case a push is missed while the socket is reconnecting.
  setInterval(refreshAll, 60000);
})();
