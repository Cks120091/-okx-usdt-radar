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

- **正式交易訊號必須兩項都成立**：① MA5／MA10／MA20 同向完整排列、MA20 連續向同方向傾斜，新形成完整結構；② MACD(12,26,9) 主線穿越 Signal(9) 訊號線，同方向新金叉／新死叉。這裡的 MACD 交叉不是 EMA12／EMA26 交叉（零軸），零軸狀態仍單獨呈現。
- 兩事件可以**先後發生**，暫定最近 **5 根已收盤 K 線**內完成；不要求同一根。完成當根需兩者當下維持同向，否則不觸發。
- 最新一根已收盤 K 線剛完成第二條件才是 `正式雙確認做多／做空`。若雙條件過去已成立，本輪只標示延續狀態，不當作新訊號重複推播。
- 頁面預設顯示「正式雙確認」；單一 MACD 交叉、背離、均線交叉只放進「觀察池」，不標示正式交易訊號。
- 現有前端按鈕不是全天候後台推播，Render Free 服務可能閒置，仍須另行實作背景監控及通知。
- MA 平均線暫用 SMA，尚未核對 TradingView 的自訂 4MA 指標來源；此規則是研究起始版本，未完成市場真實報價及勝率驗證。
