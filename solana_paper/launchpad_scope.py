"""Strict launchpad attribution for new-token research candidates.

Whitelist exactly Pump.fun, BONK.fun and Bags.fm. Network messages are
untrusted; never infer a launchpad from token name, mint suffix or generic DEX.
Generic Raydium and Meteora launches are NOT proof of BONK or Bags.
"""
from __future__ import annotations

ALLOWED_LAUNCHPADS = ("PUMP", "BONK", "BAGS")
BAGS_ALIASES = frozenset(("bags", "bagsprotocol", "bags.fm"))
BONK_ALIASES = frozenset(("bonk", "bonk.fun", "letsbonk", "letsbonk.fun"))
PUMP_ALIASES = frozenset(("pump", "pumpfun", "pump.fun"))


def _s(value):
    return str(value).strip().lower() if isinstance(value, str) else ""


def identify_stream_launchpad(packet):
    """Attribution via provider's explicit pool+platform metadata."""
    if not isinstance(packet, dict):
        return None
    pool = _s(packet.get("pool"))
    platform = _s(packet.get("platform"))
    if pool == "pump":
        # A mislabeled pump event is not enough to override conflicting source.
        return "PUMP" if not platform or platform in PUMP_ALIASES else None
    if pool == "raydium-launchpad" and platform in BONK_ALIASES:
        return "BONK"
    if pool in ("meteora-launchpad", "bags-bonding-curve", "bags-protocol"):
        return "BAGS" if platform in BAGS_ALIASES else None
    return None


def identify_bags_indexer_launchpad(item):
    """Accept exact indexer launchpad ID, not substring / trade protocol."""
    if not isinstance(item, dict):
        return None
    launch = item.get("launch")
    explicit = item.get("launchpad")
    if isinstance(explicit, dict):
        explicit = explicit.get("name") or explicit.get("id")
    if not explicit and isinstance(launch, dict):
        explicit = launch.get("launchpad")
        if isinstance(explicit, dict):
            explicit = explicit.get("name") or explicit.get("id")
    return "BAGS" if _s(explicit) in BAGS_ALIASES else None
