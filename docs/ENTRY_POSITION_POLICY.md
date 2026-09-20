# Signal and entry location separation

`SIGNAL_LOCATION_SEPARATION_V1` keeps Entry/SL/TP immutable. The canonical final
status `ENTER` / preflight compatibility status `ENTRY_READY` means an active
signal passed the independent core checks. It does NOT assert the quote is inside
Entry and does not represent an order or fill. Browser labels say 訊號已觸發.

`final.entry_position` (preflight: `entry_position`) carries original bounds,
current request quote, source, above/inside/below relation and gap. It is advice
only. Raw positional `entry_eligibility.status` remains available for history and
statistics. Do not enroll an entry-price cohort merely because signal_active=true.

Binding checks run BEFORE signal permission: actual terminal status, complete
plan/core data, formal opposite trigger, current core evidence conflict and known
1H/15m or 1D/4H disagreement. Unknown live quotes are never fabricated. A closed
trade cannot be resurrected by a quote returning to the old entry range.

Preflight obtains fresh Bid/Ask and reevaluates original plan validity; it does
not manufacture a fresh multi-timeframe candle analysis. Saved direction remains
as-of its original core analysis, unless a separate full scan re-confirms it.
