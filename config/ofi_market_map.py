"""
OFI → Polymarket static market mapping (v4-FINAL).

Maps Hyperliquid OFI lead signals (BTC-USD, ETH-USD) to Polymarket market_ids.
This is the authoritative mapping per [Invariant 1.2] — static configuration,
deterministic, auditable. NOT derived from ML or Graph similarity scores.

Manual maintenance required: Polymarket does not expose market categories in the
Gamma API /markets endpoint. Update these mappings when new crypto binary markets
become active.

Update procedure:
  1. Query: https://gamma-api.polymarket.com/markets?closed=false&limit=200
  2. Filter for markets where question/slug contains "bitcoin"/"btc" or "ethereum"/"eth"
     (exact string match, avoid partial matches like "hegseth")
  3. Add market IDs to the corresponding list below

Usage:
  from config.ofi_market_map import OFI_MARKET_MAP, get_polymarket_market_ids

Author: Panopticon Architect
Date: 2026-04-23
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Static OFI → Polymarket mapping
# --------------------------------------------------------------------------- #
# Format: hl_market_id → [polymarket_market_id, ...]
#
# CURRENT MAPPINGS (2026-04-23 — from Gamma API query):
#   BTC-USD OFI: 1 active crypto market ("Will bitcoin hit $1m before GTA VI?")
#   ETH-USD OFI: MegaETH markets exist but may be too new/specific
#
# These should be updated daily. A scripts/update_ofi_market_map.py should be created.

OFI_MARKET_MAP: dict[str, list[str]] = {
    # BTC-USD lead: OFI shocks on Hyperliquid BTC-USD predict Polymarket BTC markets
    # Source: Gamma API /markets (2026-04-23) — questions containing "bitcoin"/"btc"
    "BTC-USD": [
        "540844",  # Will bitcoin hit $1m before GTA VI?
    ],
    # ETH-USD lead: OFI shocks on Hyperliquid ETH-USD predict Polymarket ETH markets
    # Source: Gamma API /markets (2026-04-23) — questions containing "ethereum"/"eth"
    # Note: MegaETH is a specific chain, not ETH price — map only true ETH price markets
    "ETH-USD": [
        # TODO: Add ETH price markets from Gamma API when available
        # Current active ETH markets on Polymarket are MegaETH-specific (not general ETH)
    ],
}


def get_polymarket_market_ids(hl_market_id: str) -> list[str]:
    """
    Look up Polymarket market_ids for a given Hyperliquid market.
    Returns empty list if no mapping exists.
    """
    return OFI_MARKET_MAP.get(hl_market_id, [])


def primary_polymarket_market_id(hl_market_id: str) -> str | None:
    """
    Return the first (primary) Polymarket market_id for a Hyperliquid market.
    Returns None if no mapping exists.
    """
    ids = OFI_MARKET_MAP.get(hl_market_id, [])
    return ids[0] if ids else None


# --------------------------------------------------------------------------- #
# T2 / T3 / T4 / T5 Markets — OFI Exclusion Note
# --------------------------------------------------------------------------- #
# Per Architecture Ruling v6-FINAL:
#
# OFI (Order Flow Imbalance) on Hyperliquid applies ONLY to crypto markets
# (BTC-USD, ETH-USD, SOL-USD). OFI lead-lag only has predictive power when
# the OFI-shocked instrument (e.g. BTC perpetual) is the SAME instrument
# traded on Polymarket (e.g. BTC binary up/down).
#
# T2 (short-duration geo/科技 events like Iran/Hormuz/GPT-5.5) and
# T3/T4 (long-tail geo/macro events) and T5 (sports) are NOT driven by
# Hyperliquid OFI. Their Smart Money signals come from wallet_observations
# (L2 Discovery) and Shannon Entropy fires (L1 Radar). These market types
# MUST NOT be added to OFI_MARKET_MAP as they would create false OFI lead-lag
# correlations.
#
# Structure retained for future extensibility if OFI coverage expands to
# non-crypto instruments (not currently planned).
# --------------------------------------------------------------------------- #
