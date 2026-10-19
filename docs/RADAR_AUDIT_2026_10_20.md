# OKX USDT Radar — 2026-10-20 live-code audit (development branch only)

> Scope: public GitHub main and `feature/always-on-market-monitor`; read-only Render service/log inspection. No production deploy, main merge, live order, or credential change.

## Verified production baseline

- Render `okx-radar-v2-live`, Singapore, plan **Free**, running `main` commit `5d7cde19d21eb04355bb5bca622c9e12b3173db4` (2026-10-02).
- Render app logs show three manually started FULL scans:
  - 2026-10-13 10:25:46Z → 10:28:02Z: ~136 sec, `PARTIAL_DATA`, coverage 100%, 4 signals.
  - 2026-10-14 14:30:21Z → 14:33:09Z: ~168 sec, `PARTIAL_DATA`, coverage 100%, 20 signals.
  - 2026-10-15 07:07:14Z → 07:09:30Z: ~136 sec, `PARTIAL_DATA`, coverage 100%, 7 signals.
- `PARTIAL_DATA` is emitted if some `all_failures` exist. **100% coverage does not imply no per-instrument/analysis errors**. The log summaries alone do not identify exact failed symbols/reasons; inspect latest report `failed_instruments` and `data_quality` to diagnose.

## Confirmed and repaired in feature branch (still not deployed)

1. **Critical: incorrect swing polling cadence.** `radar/market_story.py::analyze_long` explicitly uses 1D background → 4H MACD/MA direction → **1H closed price Trigger**. However `radar/always_on.py::MonitorConfig.long_scan_seconds` was 14400 (4h), allowing swing formal signals to go undiscovered between scheduled scans. Set to **3600** (1h), keeping strategy untouched; added test. Also aligned LONG notification freshness with 1H (formerly 4h).
2. **High: confirmed SHORT card vanishes from triggered tab while waiting.** `radar/static/pages.html::isFormalSignal` previously required `final.status==='ENTER'` and `new_entry_allowed` plus current alignment, conflating historical triggered episode with whether the user may enter right now. LONG already considered episode status separate. Changed SHORT group membership to require confirmed stage and trigger ID, valid fresh snapshot, nonterminal, non-preview; existing entry control continues to consult decision context. Dedicated UI regression testing recommended before merge.
3. **High: REENTRY formal signal skipped by notifications.** `radar/formal_alerts.py` previously handled only `CONFIRMED` but the scanner/UI use `REENTRY` too. Added REENTRY to true formal notification stages; `EARLY_SIGNAL`, `TRENDING`, `EXTENDED` are **not** newly confirmed notifications by themselves.
4. **High: no Telegram transport silently marks notifications as delivered.** Previously the unconfigured sender no-op could still populate dedup storage, causing notifications to be suppressed when credentials are configured later. Guard for sender.enabled and add test.

## Further recommended work: not yet implemented

1. **P0 deployment/latency:** The tested real production FULL scans took 136–168 seconds. For time-sensitive alerts, measure SHORT/LONG independent timings; use a two-stage pipeline (fast closed-candle formal criteria, deferred OI/Order Book contextual enrichment) only after preserving full original hard-gate semantics and avoiding premature formal signals.
2. **P1 partial-data diagnosis:** Surface `failed_instruments`, `api_metrics`, `data_quality.core_failed_count`, `data_quality.long_failed_count`, failure codes, and coverage; distinguish market-data incompleteness vs optional deep-source unavailability. Add alerts when missingness crosses thresholds.
3. **P1 notifications end-to-end:** Provision persistent storage, secrets, dedup and retry, verify real Telegram delivery and iPhone lock-screen behavior, restart and WS reconnection. Front-end Web Push still only covers user-triggered scan-completion; it is not background formal-signal notification.
4. **P1 latency measurement:** Record timestamps `1H/15m close → detection → validated notification sent → device received`, and report p50/p95 end-to-end delay. Treat scheduled scans as post-close scans, never treat mid-candle price acceleration as formal.
5. **P2 quote clarity:** Distinguish `scan snapshot price`, `original trigger price`, `last quote time`, `entry-preflight live quote`; show price age and forbid entering stale plans. Do not replace immutable trigger fields with live prices.
6. **P2 evidence over anecdotes:** Record signals' forward 15m/1h/4h outcomes and MFE/MAE, comparing trend continuation vs other categories with fees/slippage/funding; avoid asserting winning probability from selection scores alone.
7. **P2 operational hygiene:** Avoid excessive INFO logs from 5-second `/health` checks; preserve errors and health alerts, but sample/filter routine successes. Add CPU/RAM/disk usage, API rate-limit and WebSocket health monitoring.

## Safety and test notes

- Code changes confined to `feature/always-on-market-monitor`. Render still deploys `main`, Free. No main push, deploy, private OKX API, live trading, stop-loss modification, or forced TP percentage changes.
- Added offline tests for hourly swing scheduling and formal REENTRY/no sender cases. **Tests added is not tests passed.** The latest branch revision still needs CI full regression, live OKX/VPS running tests, and UI interaction acceptance before approval.
- Keep original Entry/SL/TP1/TP2 and R:R. User manually enters and sets TP/SL in OKX.
