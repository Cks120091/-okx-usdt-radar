# Single-coin 15m historical statistics — HISTORY_SINGLE_15M_V1

This feature is a separate, user-started **single-coin 15m price-core replay**.
It is not CARD_STATISTICS_V1's live observed-entry history, not a calibrated
per-trade probability, not net execution P&L, and not a recreation of every
historical live-execution input.

## Scope

The old fixed eight-major pooled updater is no longer the active historical
statistics source. A user selects one current OKX USDT perpetual and requests
**3 days, 7 days, 30 days, 3 months (90 days), 6 months (180 days), 9 months (270 days), or 12 months (365 days)**. Only that instrument is downloaded and replayed. Finished
results are cached per instrument in a separate research database so normal
card rendering only reads cached aggregates; opening a card never starts a
history job.

This feature is **15m short-term only**. 4H / long-horizon cards do not render,
reference, inherit or reuse these rates. There is no cross-coin pooling or
fallback: a MINA card can only use MINA historical replay data, a BTC card can
only use BTC data, and so on.

To give every historical sample a complete 24-hour outcome horizon, the signal
window deliberately ends about one day before the current time. Therefore a
selected range means that many rolling days of 15m signal cutoffs ending roughly 24 hours ago,
not a calendar-month boundary extending to the present minute. Month labels map to 90/180/270/365 rolling days.

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
This intentionally keeps the sample expansion conservative rather than counting
every actionable candle as a separate trade.

Waiting, early or otherwise non-actionable states do not count until they
actually become actionable. Unlike the prior pooled research model, this
single-coin statistic does **not** require an additional five-minute
revalidation before admitting the sample. The historical signal has already
passed the price-core actionable decision at the 15m cutoff, so that cutoff is
the sample point. Entry reference, original SL and original TP1 are frozen from
that actionable state.

Historical trailing-24h quote volume and the configured inclusion/retention
hysteresis still determine whether the instrument is eligible at each cutoff.
Thus "all actionable entry opportunities" means historical 15m opportunities
the price model could actually mark actionable while the instrument satisfied
the historical liquidity-universe rule; it does not invent signals outside
those conditions.

## Outcome classification

After chronological signal generation is finished, future confirmed 5m bars are
used only for outcome classification. Each sample is followed for up to 24 hours:

- `TP1_FIRST`: TP1 is reached before SL.
- `SL_FIRST`: SL is reached before TP1.
- `UNKNOWN`: the path is missing or the same 5m bar touches TP1 and SL so the
  order cannot be known from OHLC alone.
- `TIMEOUT`: a complete 24-hour path exists but neither barrier is reached.

The headline rate is `TP1_FIRST / (TP1_FIRST + SL_FIRST)`. Timeout and Unknown
stay visible and are not silently converted to wins or losses. The card also
shows how many valid entry opportunities were found, split into first entries
and valid re-entries, and how many have a resolved TP1-vs-SL result.

## What the card shows

A 15m card first shows the selected coin's **overall recent valid-entry-opportunity
rate** for its latest cached selected-range replay. The total is split into
`首進` and `有效再進` so users can see whether a larger sample came from new
Episodes or conservative same-Episode re-entry windows.

If the current card's exact setup tuple also exists in that coin's history, the
card additionally shows a "current same-scenario" rate using the exact tuple:

- 15m horizon
- direction
- trigger type
- signal stage
- 4H relation
- R:R bucket

There is no relaxed cross-coin or market-wide fallback.

Sample-size labels are descriptive rather than a permission gate:

- 1–9 resolved: `極低樣本`
- 10–19 resolved: `低樣本參考`
- 20–49 resolved: `中等樣本`
- 50+ resolved: `樣本充足`

Small samples are deliberately shown instead of being hidden behind a 50-sample
threshold. The counts and uncertainty remain visible so the percentage is not
presented as a guaranteed per-trade probability.

## Historical data limitations

The replay requires contiguous confirmed 5m/15m/1H/4H candle history and enough
warm-up bars for the existing indicators. It does **not** fabricate historical
OI/CVD, real Bid/Ask spread, order-book depth, queue position, slippage or
funding. The modeled Ticker price used by the price engine is not represented as
a real historical executable quote.

Fees, slippage and funding are not deducted. Therefore the result is a gross
**price-core historical TP1-first statistic**, not actual filled-trade win rate
or realized profitability. Historical metadata uses the currently listed
instrument definition; delisted instruments or historical tick-size changes are
not reconstructed.

Same-Episode re-entry samples are deliberately correlated with their parent
Episode. They are useful for estimating distinct entry windows a trader could
actually encounter, but they must not be interpreted as statistically
independent market regimes.

## Worker, cache and isolation

The user starts history work explicitly from the single-coin history page. One
low-priority child process runs at a time and yields while the normal live scan
or single-instrument scan is busy. On supported systems the child attempts a
256MiB address-space limit and lower OS priority. Each session pauses after 60
minutes and can be resumed. Longer 30-day through 12-month jobs download substantially more
historical candles and may take longer; they remain the same 15m SHORT replay and never become
a 4H/long strategy. There is no cron, startup scan or automatic order submission.

Research data is stored in `history_single_15m_v1.sqlite3` under `data_dir`,
separate from live signals and CARD_STATISTICS_V1. The database has a 32MiB
safety limit and keeps a bounded set of recent coin caches. Clearing a coin's
history removes only that coin's research rows; it does not delete live signals,
observed statistics or strategy code.

Because the replay fingerprint includes the historical replay source, this
sampling-rule change makes older cached results incompatible until that coin is
manually refreshed. It does not alter live strategy state.

The same hosting caveat remains: persistence is only as durable as the service's
existing storage. Ephemeral/free hosting can lose the research cache when the
service sleeps, restarts or redeploys. Phone screen lock does not itself perform
or stop the server-side computation, but host lifecycle can still interrupt it.

The history status endpoint is read-only and does not start a worker. Home and
health routes do not initialize history work just by being opened. Strategy,
Entry/SL/TP, live permissions, ranking, direction locks and risk logic are not
changed by this feature.
