"""Evidence-labelled, read-only Solana wallet candidate roster.

No account here is verified profitable by this app. Public activity leaderboards
are NOT profit leaderboards. Addresses are snapshots, not auto-copy signals.
"""
from __future__ import annotations

from .intelligence import PUBLIC_WATCHLIST, valid_address

ACTIVITY_LEADERBOARD = "https://sol-radar.com/top-solana-traders"
SNAPSHOT_AT = "2026-10-27"
# Source: publicly visible SolRadar active-wallet leaderboard, 2026-10-27.
# Source reports activity counts/volume, NOT verified net PnL or win rate.
ACTIVE_RESEARCH_CANDIDATES = (
    ("SolRadar active #1", "9PepJaHtErQZZvsswhUorkMxa9YH7xUv1p4h4HobdaB9"),
    ("SolRadar active #2", "F3yRaZxxnihZp7Qong8V3nRxFBtFEhJp8PoupgDEpSRY"),
    ("SolRadar active #3", "4JSJp3yuLRFwboNw2G5k2vJkonYvPB71SjRcJpvuRMJ3"),
    ("SolRadar active #4", "8hShZUKKKAUMzeH2L6m4SYrpMmjJPzt6niHxnTW6TNG1"),
    ("SolRadar active #5", "GHoJLEzik3QrdFBEEYUvhHi3f12PQ2ZC3Eg99ACebh9z"),
    ("SolRadar active #6", "7SLPYMSA5FxaEvJRk7C6Pxav8KSWt7xcqGw4no4s8p7z"),
    ("SolRadar active #7", "DyNiyDgYg3A4sHKXFLccUEVHX3zBDGxx1m7xtqxnKjKP"),
    ("SolRadar active #8", "FixukbsKfJZEr996ubcWvwTAt1b6QJ2SfP3FJsBSbH33"),
    ("SolRadar active #9", "94iQuYjVgTwGXxtM7ywfpzfLvx3gN4oQRuc9osRE2pEm"),
    ("SolRadar active #10", "HmytQ4aW9v7hxar9NLqBPfuLU3cP7NypzR51XWpschjn"),
)


def roster():
    """Original public-attribution addresses plus unique activity candidates."""
    output, seen = [], set()
    for w in PUBLIC_WATCHLIST:
        addr = w["address"]
        if valid_address(addr) and addr not in seen:
            seen.add(addr)
            output.append({
                "label": w["label"], "address": addr,
                "category": "PUBLIC_ATTRIBUTION_UNVERIFIED",
                "source": "https://blog.bananagun.io/blog/fomo-copy-trading-top-wallets",
                "observed_at": "2026-09-30",
                "pnl_verified": False, "win_rate_verified": False,
            })
    for label, addr in ACTIVE_RESEARCH_CANDIDATES:
        if valid_address(addr) and addr not in seen:
            seen.add(addr)
            output.append({
                "label": label, "address": addr,
                "category": "ACTIVE_RESEARCH_CANDIDATE",
                "source": ACTIVITY_LEADERBOARD,
                "observed_at": SNAPSHOT_AT,
                "pnl_verified": False, "win_rate_verified": False,
            })
    return output


def selected(limit=10):
    """One Helius socket; cap subscriptions to reduce free-plan credit use."""
    return roster()[:max(0, min(int(limit), 13))]
