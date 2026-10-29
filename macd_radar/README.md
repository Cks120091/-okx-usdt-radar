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
