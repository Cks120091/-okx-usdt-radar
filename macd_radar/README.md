# New MACD Radar (independent preview, 2026-10-29)

This is an entirely separate **read-only** OKX USDT-margined perpetual MACD scanner. It has its own backend, core, and responsive UI under `macd_radar/`. It does **not** import, replace, or execute the previous `radar/` code, the existing Solana paper-trading app, or any trading SDK.

## Inputs (from screenshots)
- Source: Close
- MACD: Fast EMA 12, Slow EMA 26, Signal EMA 9
- TradingView "wait for timeframe closes": enabled (OKX candles only when `confirm == "1"`)
- Available scanner timeframes: 15m, 1H, 4H, 1D (one chosen timeframe at a time).
- **4MA 5/10/20/90 is *not* a gate in this version**. Its average type was not shown in the screenshots. Do not silently change signal rules by introducing an unverified MA filter.

## Signal distinctions
- **金叉／死叉** = crossing of MACD line and signal line on *the most recent closed bar* (histogram zero crossing).
- **金叉預備／死叉預備** = two consecutively improving closed histogram bars approaching zero, but no confirmed crossing; WATCH ONLY.
- **方向維持** = state of histogram, not a fresh signal.
- **動能增強／減弱** = growth of |histogram| vs previous closed bar. Not predictive win rate.
- Latest ticker price is different from the last **confirmed candle close** and is deliberately displayed separately.

## Launch locally (Python 3.10+, only standard library)

From repository root:

```sh
python -m macd_radar.app
# Open http://localhost:8000
python -m unittest discover -s macd_radar/tests -v
```

Environment variable `PORT` changes the port. API: `GET /api/scan?tf=15m&limit=20`, `GET /health`.

Data source: OKX public, unauthenticated `/api/v5/market/tickers?instType=SWAP` and `/api/v5/market/candles`. Each candle includes a confirmation field; only complete candles are counted. Up to 300 candles are requested per symbol. The default universe is the top 20 USDT-SWAP tickers by **approximate** 24h nominal quote volume (`volCcy24h * last`), which is a ranking proxy only and must not be presented as exact execution liquidity. This prototype scans up to 40 tickers on request, using three concurrent workers and a 50-second server-side cache.

## Current scope / exclusions
- UI auto-polls while open every 60 seconds; **not** 24/7 headless monitoring yet.
- Not deployed on Render; **does not modify the existing main branch or production service**.
- No exchange account API keys, order placement, signal forwarding, backtest validation, TP/SL, liquidation analysis, or auto execution.
- Does not claim MACD cross accuracy or win-rate; no implicit old V3.x logic, ranking, filters, or OI rules are carried over.
- Deployment should use a **separate** service and explicit entry command `python -m macd_radar.app`, not replace the existing `run.py` in production.
- Exchange session boundaries and EMA history initialization can make minor differences vs an individual TradingView chart; compare same contract, market, timeframe and candle boundaries before asserting exact equivalence.
- Existing backup branch: `backup/pre-macd-rebuild-20261029` (pinned to commit `c6ce05bb3ae28a516a45773a3e044cabb5a5bb14`). This preserves repository code, **not** ephemeral Render databases, environment variables or any uncommitted state.

## Recommended next validation
1. Run pure calculation tests offline.
2. Confirm active OKX REST connectivity and data freshness from intended hosting region.
3. Compare several candles and crossover timings against TradingView.
4. Test browser UI on iPhone; only then consider separate staging deployment.

## New MA × MACD research rules (2026-10-29 user chart feedback)

**This changes only the isolated feature branch. No production deployment.**

### MA (5/10/20/90)
- The supplied image only reveals the four lengths, not whether the custom `4MA` script uses SMA or EMA. **SMA is a clearly provisional research default until chart parity is verified**. The implementation supports `ma_type="EMA"` as an alternative.
- MA5 crossing MA10 (fresh up/down) is recorded separately.
- Actual MA5 and MA10 crossings of MA20 are tracked up to 12 previously closed candles. Neither crossing needs to occur on the same bar.
- Full bearish structure = MA5 < MA10 < MA20 and MA20 strictly descending across three closed readings. Full bullish = inverse. Full structure first appearance is distinct from a persistent structure.
- MA90 is context-only: current last close above/below MA90; NOT an entry condition.

### Two different "12/26 crosses"
- `EMA12 - EMA26` crossing zero is the **EMA12 vs EMA26 crossover**.
- `MACD(12,26)` crossing `signal EMA9` is the **MACD signal crossover**. These two are not interchangeable.
- Crosses are based on confirmed closed candles. Intrabar changes cannot create formal alerts.

### Divergence / high-low pivot comparison
- Compare PRICE high/low swing pivots with the MACD-line values at the **same** confirmed bars.
- Regular bearish divergence: price higher high, MACD lower high; regular bullish: price lower low, MACD higher low.
- A pivot is accepted only after two closed candles on its right; no access to future unknown data.
- Candidate is shown only when the second pivot is no older than 12 bars, and pivot separation 3–40 bars. Price change >= 0.3% and MACD difference >= 8% of max absolute MACD pivot magnitude are provisional noise filters.
- Provisional **major** candidate: pivot separation >=12 bars AND price move >=1.2%. Otherwise **minor** candidate; these are mechanical scopes, **not** subjective or validated strength/win probabilities.
- Does not yet identify hidden divergences, nested fractal divergence across higher timeframes, or "failed divergence" outcomes; these require separate research.
- Divergence may be a caution against an ongoing move, not a sufficient entry by itself.

### Test and release status
- Offline unit test file `macd_radar/tests/test_technical.py` added for MA order, MA slope, zero-cross distinction, regular bearish divergence and delayed pivot confirmation. **The tests have been committed but not executed in a connected test environment yet.**
- Compare DOGE and ETH charts with matching OKX contract/timeframe and the TradingView custom 4MA source before calling visual parity.


## 嚴格雙確認模式（2026-10-29）

- **正式交易訊號必須兩項都成立**：① MA5／MA10、MA5／MA20、MA10／MA20 均已同向交叉，完成完整排列且 MA20 斜率同向；② MACD(12,26,9) 主線穿越 Signal(9) 訊號線，同方向新金叉／新死叉。這裡的 MACD 交叉不是 EMA12／EMA26 交叉（零軸），零軸狀態仍單獨呈現。
- 兩事件可以**任意先後發生**，不要求同一根，也不設定 5 根 K 線硬截止。MA5／MA10、MA5／MA20、MA10／MA20 三組必須都有已觀察到的同向交叉，MA20 斜率同向；MACD 與 Signal 線也必須有同方向交叉。第二條件完成當根兩者仍有效才可發正式訊號。
- 最新一根已收盤 K 線剛完成第二條件才是 `正式雙確認做多／做空`。若雙條件過去已成立，本輪只標示延續狀態，不當作新訊號重複推播。
- 頁面預設顯示「正式雙確認」；單一 MACD 交叉、背離、均線交叉只放進「觀察池」，不標示正式交易訊號。
- 現有前端按鈕不是全天候後台推播，Render Free 服務可能閒置，仍須另行實作背景監控及通知。
- MA 平均線暫用 SMA，尚未核對 TradingView 的自訂 4MA 指標來源；此規則是研究起始版本，未完成市場真實報價及勝率驗證。


## 24 小時成交額過濾（2026-10-29）

- 只接受 `*-USDT-SWAP` 的 OKX USDT 本位永續合約，先檢查估算 **24h USDT 成交額 >= 1,500,000**，達標才載入 K 線、MA、MACD、背離及雙重確認。
- 目前 OKX `SWAP` ticker 的 `volCcy24h` 是標的幣數量，並非可直接與 USDT 門檻比較的成交額。以 `volCcy24h × last` 計算 **近似 USDT 成交額**，不是逐筆成交價加總的精確值；例如邊界附近行情可因價差被錯分。網站與 API 均標明「估算」。
- `>=` 含剛好 150 萬；資料缺漏／無效／非正數／非有限值一律不通過。按估算成交額排序，選擇前 20／40 個合約；**不是整個符合門檻的合約宇宙全部掃描**。所選合約即使高於門檻，也仍可能因 K 線不足／OKX API 錯誤而不顯示。
- 這是成交活躍度篩選，不等於訂單簿深度，也不能代替滑價評估；避免用它當作勝率指標。
- 離線測試涵蓋臨界值、低於門檻、非 USDT 合約、缺失／NaN 資料，以及未達標時**不觸發 K 線掃描**。

## 掃描標的：加密貨幣＋黃金（2026-10-29）

- 在任何成交額、K 線、MA、MACD 或背離分析**之前**，先查 OKX 唯讀公開 API `/api/v5/public/instruments?instType=SWAP`。
- **一般加密貨幣**：要求 `instType=SWAP`、`instId` 結尾 `-USDT-SWAP`、`instCategory="1"`（Crypto）且 `state=live`。
- **黃金唯一特例**：`XAU-USDT-SWAP` 且 `instCategory="4"`（Commodities），同時 `state=live`；不是整個類別 4 都開放。OKX 已於 2026-04-22 將 `XAU-USDT-SWAP` 重新歸類為大宗商品；2026-01-15 已將舊 `XAUT-USDT-SWAP` 更名為 `XAU-USDT-SWAP`。
- 股票（AAPL、NVDA、TSLA）、股指、白銀（XAG）、原油（CL/BZ）、外匯、債券及分類缺失／不明合約均不納入；不依賴股票代號黑名單。
- **黃金同樣需要估算 24 小時成交額 ≥ 1,500,000 USDT**。達標、交易中時保留前 20／40 個掃描名額的其中一席；不會突破掃描上限，不會放寬成交額門檻。未達標時也不保留。
- 官方分類清單快取 5 分鐘。若官方分類查詢失敗、找不到任何有效標的，則停止掃描，不會退回掃描全部 USDT 永續。
- MA × MACD 雙重確認、K 線已收盤確認及背離規則均完全不變；此修改只影響可掃描標的範圍。

參考：OKX [API 變更紀錄](https://www.okx.com/docs-v5/log_en/)（2026-04-22 分類及 2026-01-15 黃金合約更名）。

## 已確認 K 線資料時效驗證（2026-10-30）

- 只使用交易所 confirm=1 的 K 線仍不足以證明行情最新。現在比較最後確認 K 的結束時間與伺服器時間；若落後超過一個完整週期加 2 分鐘，禁止產生新的 MA+MACD 雙確認訊號。
- 例如 15 分鐘圖，08:03 最後確認 K 卻是 07:00 收盤，則視為過期並排除，診斷說明原因。
- 黃金過期時網站顯示「黃金K線延遲，暫停黃金訊號」；原有均線、MACD、24h 成交額 150 萬 USDT 與排除股票規則不變。
- 交易所產品和 TradingView 圖表必須核對完全相同的交易所及合約。網站的 MA 類型目前仍是暫定 SMA，不能保證與 TradingView 自訂 4MA 完全一致。
