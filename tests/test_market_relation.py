from radar.market_scope import XAU_INST_ID
from radar.scanner import MarketScanner


def test_countertrend_strength_uses_broad_market_benchmark():
    meta = MarketScanner._market_resonance_meta(
        "LONG",
        {
            "score": 20.0,
            "market_core_change_pct": -1.2,
            "market_24h_change_pct": -6.0,
            "btc": {"core_change_pct": -0.8},
        },
        "ALT-USDT-SWAP",
        metrics={
            "price_change_core_pct": 0.4,
            "price_change_24h_pct": -1.0,
        },
        driver={"key": "INDEPENDENT"},
    )
    assert meta["state"] == "COUNTER"
    assert meta["path_state"] == "COUNTER_STRONG"
    assert meta["path_label"] == "逆勢強勢"
    assert meta["relative_strength_pct"] == 1.6
    assert meta["priority"] == 3
    assert meta["affects_trigger"] is False


def test_leading_resonance_is_aligned_and_outperforming():
    meta = MarketScanner._market_resonance_meta(
        "LONG",
        {
            "score": 80.0,
            "market_core_change_pct": 0.5,
            "market_24h_change_pct": 4.0,
            "btc": {"core_change_pct": 0.7},
        },
        "ALT-USDT-SWAP",
        metrics={
            "price_change_core_pct": 2.0,
            "price_change_24h_pct": 5.0,
        },
        driver={"key": "INDEPENDENT"},
    )
    assert meta["state"] == "ALIGNED"
    assert meta["path_state"] == "LEADING_RESONANCE"
    assert meta["path_label"] == "領先共振"
    assert meta["relative_strength_pct"] == 1.5
    assert meta["strength_confirmed"] is True
    assert meta["priority"] == 4


def test_recovery_resonance_tracks_prior_market_underperformance():
    meta = MarketScanner._market_resonance_meta(
        "LONG",
        {
            "score": 80.0,
            "market_core_change_pct": 1.0,
            "market_24h_change_pct": 5.0,
            "btc": {"core_change_pct": 1.2},
        },
        "ALT-USDT-SWAP",
        metrics={
            "price_change_core_pct": 0.6,
            "price_change_24h_pct": 1.5,
        },
    )
    assert meta["state"] == "ALIGNED"
    assert meta["path_state"] == "RECOVERY_RESONANCE"
    assert meta["path_label"] == "弱勢修復 → 共振"
    assert meta["relative_strength_24h_pct"] == -3.5
    assert meta["priority"] == 3


def test_plain_resonance_stays_plain_when_no_outperformance_or_recovery():
    meta = MarketScanner._market_resonance_meta(
        "LONG",
        {
            "score": 80.0,
            "market_core_change_pct": 1.0,
            "market_24h_change_pct": 3.0,
            "btc": {"core_change_pct": 1.1},
        },
        "ALT-USDT-SWAP",
        metrics={
            "price_change_core_pct": 1.2,
            "price_change_24h_pct": 3.1,
        },
    )
    assert meta["path_state"] == "ALIGNED"
    assert meta["path_label"] == "大盤共振"
    assert meta["priority"] == 2


def test_short_side_leading_resonance_is_direction_aware():
    meta = MarketScanner._market_resonance_meta(
        "SHORT",
        {
            "score": 20.0,
            "market_core_change_pct": -0.8,
            "market_24h_change_pct": -4.0,
            "btc": {"core_change_pct": -0.9},
        },
        "ALT-USDT-SWAP",
        metrics={
            "price_change_core_pct": -2.0,
            "price_change_24h_pct": -5.0,
        },
        driver={"key": "INDEPENDENT"},
    )
    assert meta["path_state"] == "LEADING_RESONANCE"
    assert meta["relative_strength_pct"] == -1.2
    assert meta["priority"] == 4


def test_xau_is_never_classified_against_crypto_market():
    meta = MarketScanner._market_resonance_meta(
        "LONG",
        {"score": 80.0, "market_core_change_pct": 1.0},
        XAU_INST_ID,
        metrics={"price_change_core_pct": 3.0},
    )
    assert meta["path_state"] == "NOT_APPLICABLE"
    assert meta["affects_trigger"] is False
