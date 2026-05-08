"""Offline / manual diagnostics (heavy queries). Not on trading hot path."""

from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

__all__ = ["build_market_breakdown"]
