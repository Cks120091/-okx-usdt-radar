# 15m background authority and entry-window continuity

Policy: `SHORT_CONTEXT_WINDOW_V2`. This change applies to the 15m radar only.

* 1H/4H direction and their strength remain visible context. They no longer add
  an entry veto, alone or paired with one ordinary core warning. Two actual core
  price-conflict domains, formal opposite events, missing core/execution quotes,
  invalid plans, liquidity, spread, slippage, cost and remaining-R checks retain
  their existing authority. The 4H swing radar keeps its existing policy.
* The 15m candidate tie-break, continuation trend alignment and trend evidence
  use the 15m core instead of averaged 1H/4H direction. Price-event definitions,
  candle confirmation and numeric price thresholds are unchanged. Evidence
  groups carry CORE/15m provenance; public data preserves it.
* A distinct, persisted entry-window record belongs to one exact Episode and
  immutable Entry/SL/TP. Refreshing a recent open window is not a second entry
  event. New core bars require continuous closed-bar price-envelope evidence;
  unavailable history does not prove continuity. This does not claim to monitor
  unsampled intrabar movements between manual scans.
* Observed suspension is durable, including standalone preflight. Returning to
  the old Entry by price alone cannot revive it. A newer confirmed retest is
  still required. Monotonic timestamps, exact-plan and generation guards stop
  old/concurrent responses from overwriting a newer suspension. The existing
  30-minute data freshness bound is retained, not a new profit/holding rule.
* Single-coin preflight uses the just-refreshed same Episode when available,
  rather than recalculating against an older report's permission. Known retest
  waits preserve their actual reason; they are not described as an out-of-zone
  price or a generic risk failure.

No SL/TP, universe thresholds, scanner schedules, automatic trading, deployment
configuration, or 4H swing rules are changed. Old Episode IDs and trigger times
are retained. All validation fixtures are synthetic/offline; no assertion about
live profitability or the precise historical GRASS incident is made.
