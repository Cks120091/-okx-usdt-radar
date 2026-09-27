# Core indicator fusion

This layer is intentionally unnamed in the user interface. It is part of the radar's internal indicator feature set, not a separate strategy badge.

## Goal

Use one direction score to keep the completed 1H direction, short-radar display and 15m entry permission consistent. This contract was updated on 2026-09-27 at the user's request.

A formal 15m price pattern is still required. The 1H score cannot create a price Trigger by itself. Original Entry/SL/TP geometry and episode identity stay fixed. See `SHORT_DIRECTION_20260927.md` for the direction contract.

## Internal fusion

Every timeframe feature calculation now also derives:

- EMA 7: fast continuation / weakening context.
- EMA 12: retest / reclaim context.
- EMA 144 / 169: medium trend envelope when the real candle window is long enough.
- EMA 576 / 676: deep trend envelope only when at least that much real history is present. Missing deep history is neutral and never treated as opposition.
- Smoothed RSI with a volatility-adjusted dynamic band: momentum persistence context. It is not counted as a second independent RSI vote.
- Existing EMA 21 / 55 and MACD are blended into the same hidden fusion score rather than duplicated as extra votes.

The resulting hidden directional score contains four descriptive components:

1. trend structure,
2. EMA12 retest/reclaim state,
3. smoothed-RSI + MACD momentum persistence,
4. EMA7 fast continuation state.

## Direction contract (2026-09-27)

The completed 1H fusion score is the shared short-radar direction source in `short_direction.py`. Market-story selection, the visible 1H label, scanner permission and preflight use the same classification. Bullish weakening still permits only LONG; bearish weakening only SHORT. Neutral or missing 1H data waits. A 15m counter-direction event cannot become a new entry.

The 15m price structure supplies the formal Trigger, while 4H remains background. The direction score is not a probability or a historical win-rate claim.

## Data-window policy

The live scanner currently uses bounded candle windows (roughly 120–240 bars by timeframe). We do not increase those fetch limits merely to force EMA 576 / 676 into existence because that would materially increase full-market API work and scan latency.

EMA 144 / 169 activates where the existing real window is sufficient. EMA 576 / 676 stays unavailable/neutral unless a future data source already provides enough closed history.
