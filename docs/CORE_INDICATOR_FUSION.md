# Core indicator fusion

This layer is intentionally unnamed in the user interface. It is part of the radar's internal indicator feature set, not a separate strategy badge.

## Goal

Increase context quality without reducing signal frequency or changing the existing trading contract.

The current formal Trigger, Entry permission, Entry/SL/TP geometry, Signal Episode lifecycle, OI/CVD logic, execution-risk gates and ranking remain unchanged.

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

## Non-gating contract

The fusion score is shadow telemetry in this release.

It is deliberately not read by:

- `radar/market_story.py` formal Trigger selection,
- `radar/strategy.py` Entry / SL / TP / actionable logic,
- `radar/decision.py` hard gates.

Therefore adding this feature layer cannot remove an existing Trigger or make Entry rules stricter. A future change that promotes any fusion component into a gate must be explicit, reviewed, and backed by historical Trigger-time evidence.

## Data-window policy

The live scanner currently uses bounded candle windows (roughly 120–240 bars by timeframe). We do not increase those fetch limits merely to force EMA 576 / 676 into existence because that would materially increase full-market API work and scan latency.

EMA 144 / 169 activates where the existing real window is sufficient. EMA 576 / 676 stays unavailable/neutral unless a future data source already provides enough closed history.
