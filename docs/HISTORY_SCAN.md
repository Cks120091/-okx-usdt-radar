# Single-coin 15m historical statistics — HISTORY_SINGLE_15M_V1

This feature is a separate, user-started **single-coin 15m price-core replay**.
It is not CARD_STATISTICS_V1's live observed-entry history, not a calibrated
per-trade probability, not net execution P&L, and not a recreation of every
historical live-execution input.

## Scope

A user selects one current OKX USDT perpetual and requests **3 days, 7 days,
14 days, or 30 days**. Those are the only supported user ranges. Only that
instrument is downloaded and replayed. Finished results are cached per
instrument in a separate research database so normal card rendering only reads
cached aggregates; opening a card never starts a history job.

This feature is **15m short-term only**. 4H / long-horizon cards do not render,
reference, inherit or reuse these rates. There is no cross-coin pooling or
fallback: a MINA card can only use MINA historical replay data, a BTC card can
only use BTC data, and so on.

To give every historical sample a complete 24-hour outcome horizon, the signal
window deliberately ends about one day before the current time. A selected
range therefore means that many rolling days of 15m signal cutoffs ending
roughly 24 hours ago, not a calendar range extending to the present minute.

## Which signals become samples

Every completed 15m cutoff in the requested signal window is replayed
chronologically using only confirmed data that existed at that cutoff. The
existing price engine, Episode/retest/window state and price-entry permission
logic run in an isolated in-memory repository.

The first sample for an Episode is admitted at its **first actionable / 可進場
15m close**. If the same card simply remains actionable for later 15m bars,
those later bars do not create duplicate samples.

A later same-Episode sample is admitted only as a **valid re-entry opportunity**:
after a prior admitted opportunity, the Episode must remain non-actionable for
at least **four consecutive completed 15m bars (one hour)** and then become
actionable again. A one-, two- or three-bar permission flicker therefore does
not manufacture another trade. After a re-entry is admitted, another full
four-bar non-actionable reset is required before a further re-entry can count.

Waiting, early or otherwise non-actionable states do not count until they
actually become actionable. The historical signal does not require an
additional five-minute revalidation before admission. Entry reference, original
SL and original TP1 are frozen from that actionable state.

Historical trailing-24h quote volume and the configured inclusion/retention
hysteresis still determine whether the instrument is eligible at each cutoff.
The replay does not invent signals outside those conditions.

## Outcome classification

After chronological signal generation is finished, future confirmed 5m bars are
used only for outcome classification. Each sample is followed for up to 24 hours:

- `TP1_FIRST`: TP1 is reached before SL.
- `SL_FIRST`: SL is reached before TP1.
- `UNKNOWN`: the path is missing or the same 5m bar touches TP1 and SL so the
  order cannot be known from OHLC alone.
- `TIMEOUT`: a complete 24-hour path exists but neither barrier is reached.

The headline rate is `TP1_FIRST / (TP1_FIRST + SL_FIRST)`. Timeout and Unknown
stay visible and are not silently converted to wins or losses. The UI shows the
overall selected-coin rate plus the split between first entries and valid
re-entries.

## What the card shows

The main 15m signal card places two compact read-first summaries ahead of the
full decision details:

1. **快看** — action, setup, basis and funding/participation summary.
2. **本幣 15m 歷史勝率** — overall selected-coin rate, range, valid entry
   opportunities, first-entry/re-entry split and TP1/SL counts.

The older **same-scenario / 同類情境歷史勝率** blocks are intentionally hidden
from the main UI to reduce duplication and prevent small scenario cohorts from
competing with the overall coin-level historical reference. The underlying
research grouping can remain in cached data for compatibility; it is not shown
or used as an entry permission.

Sample-size labels remain descriptive:

- 1–9 resolved: `極低樣本`
- 10–19 resolved: `低樣本參考`
- 20–49 resolved: `中等樣本`
- 50+ resolved: `樣本充足`

## Historical data limitations

The replay requires contiguous confirmed 5m/15m/1H/4H candle history and enough
warm-up bars for the existing indicators. It does **not** fabricate historical
OI/CVD, real Bid/Ask spread, order-book depth, queue position, slippage or
funding. Fees, slippage and funding are not deducted. The result is a gross
**price-core historical TP1-first statistic**, not actual filled-trade win rate
or realized profitability.

Same-Episode re-entry samples are deliberately correlated with their parent
Episode. They estimate distinct entry windows a trader could encounter, but are
not statistically independent market regimes.

## Worker, cache and isolation

The user starts history work explicitly from the single-coin history page. One
low-priority child process runs at a time and yields while the normal live scan
or single-instrument scan is busy. On supported systems the child attempts a
256MiB address-space limit and lower OS priority. Each session pauses after 60
minutes and can be resumed.

The 14-day and 30-day ranges are split into contiguous 7-day chunks. Each
completed chunk is persisted before the next chunk starts, so a pause, restart
or 60-minute safety stop resumes from remaining chunks instead of restarting the
whole range.

Research data is stored in `history_single_15m_v1.sqlite3` under `data_dir`,
separate from live signals and CARD_STATISTICS_V1. The database has a 32MiB
safety limit and keeps a bounded set of recent coin caches. Clearing a coin's
history removes only that coin's research rows; it does not delete live signals,
observed statistics or strategy code.

The same hosting caveat remains: persistence is only as durable as the service's
existing storage. Ephemeral/free hosting can lose the research cache when the
service sleeps, restarts or redeploys.

The history status endpoint is read-only and does not start a worker. Home and
health routes do not initialize history work just by being opened. Strategy,
Entry/SL/TP, live permissions, ranking, direction locks and risk logic are not
changed by this feature.
