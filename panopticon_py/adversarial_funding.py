from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FundingRisk(str, Enum):
    NORMAL = "NORMAL"
    CEX_HOT_WALLET = "CEX_HOT_WALLET"
    MIXER = "MIXER"


@dataclass(frozen=True)
class FundingProbe:
    source_label: str
    risk: FundingRisk


def classify_funding_source(source_label: str) -> FundingProbe:
    label = source_label.lower()
    if "tornado" in label or "mixer" in label:
        return FundingProbe(source_label=source_label, risk=FundingRisk.MIXER)
    if any(x in label for x in ["binance", "okx", "coinbase", "kraken", "hot wallet", "cex"]):
        return FundingProbe(source_label=source_label, risk=FundingRisk.CEX_HOT_WALLET)
    return FundingProbe(source_label=source_label, risk=FundingRisk.NORMAL)


def should_disable_graph_trace(probe: FundingProbe) -> bool:
    return probe.risk in {FundingRisk.CEX_HOT_WALLET, FundingRisk.MIXER}
