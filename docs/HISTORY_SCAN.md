# Historical scanning and card rates — HISTORY_PRICE_REPLAY_V1

This feature is a separate, user-started **15m price-core simulation**. It is not
CARD_STATISTICS_V1's observed-entry history, calibrated per-trade probability,
net execution P&L, or a recreation of the full live execution scanner.

## Use and scope

Open More → 歷史K棒掃描 (or the link in each card), select 7 or 30 days, and press
Start. Loading the home page or status endpoint never starts a worker. One
low-priority child process downloads and replays one instrument at a time.
All currently listed eligible OKX crypto USDT linear perpetuals are enumerated,
not a fixed five-coin basket and not the current top-20 cards. The manifest is
frozen for resumption. Historical trailing-24h quote volume and the configured
entry/retention hysteresis determine eligibility at each replay time. Delisted
instruments and historical tick-size changes cannot be reconstructed by this
first version: current-universe survivorship and metadata limitations remain.

The latest one day is reserved for outcomes. The chosen signal interval ends
at least 24h plus the entry delay before job creation. It is not a future test.

## Causality and execution assumptions

Each 15m cutoff sees only confirmed contiguous 5m/15m/1H/4H histories ending at
or before that time. At least 60 bars and configured indicator history are
required. Original price engine, plans, Episode/retest/window state are reused
in an isolated in-memory repository. A one-day Episode warmup precedes the
signal interval; indicators have longer warmup data. Historical excursion
profiles use only outcomes then present in that chronological replay repository.

A waiting Episode is reconsidered at subsequent 15m closes; it is not discarded
after its first unsuccessful entry attempt. Once price eligibility passes,
entry is attempted at the 5m open five minutes after the cutoff. Original SL/TP1
must not be touched during the modeled delay. This is an assumed fill reference,
not real historical Ask/Bid, queue position or slippage. A modeled departure
suspends the isolated window; a later quote cannot invent closed-retest proof.
One accepted simulated entry per Episode is counted. No real-order API is used.

The price model preserves price-location/retest, core-conflict and remaining-R
checks. Missing historical order book, spread, OI/CVD or funding is NOT made up.
Execution-context gates and cross-market top-20 selection are not reconstructed,
so the simulation does not assert the live radar would have allowed those trades.
Modeled Ticker Bid/Ask equal the available price solely to satisfy the original
price engine's input contract, and are never labeled actual exchange quotes.

Outcomes use full, confirmed 5m paths for up to 24 hours from assumed entry.
TP1 first and SL first are distinct. The same 5m bar touching both, or missing
intervening bars, is UNKNOWN. Fully covered horizons with no hit are TIMEOUT.
Threshold fills are an assumption; gaps/fees/funding/slippage can make actual
net results worse. Forward bars are evaluated only after chronological signal
generation and are not fed back as past evidence. No live database is modified.

## Card display

The new 歷史 K 棒回測 panel is separate from 上線後掃描觀測. Matching uses the
same setup tuple (15m, side, trigger, stage, higher-timeframe relation, target-R
bucket) as the existing statistics helper, under this model's source/config
fingerprint. Cross-coin pooling is explicit. There is no market-wide fallback,
quality-score conversion, five-coin constant, or 15m-rate reuse on a 4H card.

A headline is released only after enumeration completes, the current instrument
has at least 95% cutoff coverage, at least 80% of the whole manifest has that
coverage, and its group has 50 resolved samples, five UTC entry dates and at
least 80% resolved outcomes. These are chosen display guards, not proof of
predictive reliability. Failures, insufficient history, UNKNOWN and TIMEOUT are
shown, not silently omitted. Rate = TP1_FIRST / (TP1_FIRST + SL_FIRST), gross.
The Wilson 95% interval is descriptive and does not correct coin/time clustering.
All conditions must hold; partial progress never becomes a fabricated full-scope
percentage. A source or relevant configuration change invalidates compatibility.

## Capacity, availability and privacy

The worker yields while the ordinary scan or single-instrument scan lock is
busy; a low OS priority and 256MiB child address-space limit further bound it on
Linux. These do not guarantee a constrained host cannot run out of resources.
The web process never executes the replay in its HTTP thread. Each manual
session pauses after 60 minutes and can be resumed; completed instruments are
retained, an interrupted current instrument is redone. No auto-resume, recurring
cron, startup market request, or canceled 72-hour experiment is added.

Data is in a separate history_replay_v1.sqlite3 under data_dir. The 32MiB limit
is checked before symbols and starts; one symbol can take it slightly over.
Up to three jobs are retained before explicit cleanup is required. Raw candle
arrays are transient; compact model samples stay on the host only. Responses
expose aggregate group statistics and manifest coverage, not individual trades
or the SQLite file. No real samples are committed to GitHub. This does not add
private user authentication: site visitors who can use the existing site can
view the aggregate or deliberately start work. Intent/origin/CSRF checks stop
cross-site accidental operations, not authorized same-site visitors.

Pause then Clear explicitly deletes only this research data, not signals,
observed card_statistics_v1 or code. There is no automatic deletion. Persistence
is only as durable as the host's existing storage; free ephemeral hosting may
lose the research database on restart/deployment. No new paid storage, deployment
configuration, strategy threshold or ranking is provisioned by this patch.

Tests are synthetic/offline unless specifically identified as a limited exchange
compatibility smoke check. Installing this feature does not mean a complete
7-day/30-day all-instrument replay has already been run.
