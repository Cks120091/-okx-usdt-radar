# Card history statistics (CARD_STATISTICS_V1)

This is historical **TP1-first rate among resolved observed-entry plans**, not
an individual probability forecast, trading permission, or live execution P&L.
It adds no market requests, scheduler, automatic trades, score or entry gate.
The canceled 72-hour experiment remains canceled.

## Sampling

An additive `card_statistics_v1` table lives in the existing SQLite database.
Original signal records, outcomes, adaptive targets and performance calculations
are not changed. Only final top-card full/15m/4H scan results and final merged
single-coin scan results may enroll. Previews, blocked/waiting plans, stale or
missing executable quotes and unknown plans cannot enroll. A given Episode
gets one immutable first-observed-ready snapshot: actual scan publication time,
directional Ask/Bid reference, original SL/TP1, original grouping. This is a
quote-based hypothetical reference, not evidence of a fill. Old signal rows
are not backfilled because their first executable readiness is not proven.

Only statistics aggregates are exposed on existing cards and single-coin views.
No raw samples or SQLite database are uploaded to GitHub. Data survives only
as long as the configured SQLite storage does; ephemeral host storage can lose
it on restart/deployment. This change does not provision durable storage.

## Fixed comparison groups and horizon

Same source/config fingerprint, horizon, direction, trigger type, entry stage,
higher-timeframe direction relation and target-R bucket. Cross-coin pooling is
explicit. No fallback to a market-wide rate, no post-outcome reclassification,
no quality-to-percentage conversion. The current Episode is always excluded
from its own statistic. Samples use the preceding 90 calendar days, capped at
5,000 newest matching records with the cap disclosed. No automatic record
purging is added.

SHORT observes at most 24 hours; LONG at most 7 days. A sample must reach that
full age before contributing, even if it won or lost earlier, to avoid only
counting early resolved winners while comparable signals are still open.
Outcomes must have been observed by the summary's as-of timestamp. The display
is calculated as of the refresh, not claimed to have been known at original
signal issuance. Re-scanning a frozen sample retains its original cohort.

## Outcome path

Normal scans reuse their existing confirmed `_core_path`, never a new external
request or a last ticker. The independent ledger continues tracking even when
the original card is no longer in the current top 20 or changes lifecycle,
provided the instrument's path is scanned. Long/short scans and unrelated
single-coin scans do not advance another instrument's records. No ticker-only
preflight can fabricate TP/SL ordering. Intervening bars must be continuous.
When a partial entry/expiry bar hits a barrier, or the same full bar hits both,
record UNKNOWN instead of selecting the favorable order. Core resolution is
15m or 4H: no claim is made of tick-level monitoring or exact fills.

Missing paths can be recovered on a later normal scan. At the fixed horizon
plus one core-bar close, irrecoverable paths encountered in a scan become
UNKNOWN. No subsequent scan means pending/unresolved, never an assumed loss.
A fully covered horizon with neither barrier reached becomes TIMEOUT. Both
categories are disclosed, not hidden in a headline percentage. No result is
retroactively changed after being finalized.

## Display policy

Headline percentage is TP1_FIRST / (TP1_FIRST + SL_FIRST). At least **50 resolved
samples**, **5 UTC sampling dates**, and **80% resolved coverage among mature
samples** are required; otherwise show insufficient/low coverage. These are
chosen display guardrails, NOT proof of reliability. The card exposes wins,
losses, immature samples, pending paths, unknown results, timeouts, dates and
observation horizon. All rates are gross, excluding fees/slippage/funding.
No net expectancy is claimed.

A 95% Wilson interval is shown only with a released rate. Its binomial model is
not corrected for contemporaneous/correlated market moves; it is descriptive,
not calibrated prediction uncertainty. Reference formula: NIST Dataplot,
`https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/propconf.htm`.

The module never changes SL/TP, Signal identity, direction lock, risk controls,
ranking, Universe filters or trading permission. Statistics failures degrade
the statistics display rather than failing a scan. Tests use synthetic/offline
data only and cannot establish the live system's win rate.
