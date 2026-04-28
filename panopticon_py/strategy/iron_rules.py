"""全視之眼 3.0 鐵律：顯式例外類別與硬斷言（審計用）。"""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

REJECTED_CLUSTER_FRICTION_LOG = "[REJECTED_DUE_TO_CLUSTER_CAP_AND_FRICTION]"


class IronRuleViolation(Exception):
    """Base for all hard risk rules."""

    code: str = "IRON_RULE"


class StaticPnLBypassError(IronRuleViolation):
    """DoNotTrustStaticPnL: macro / 排行榜候選不得繞過 fingerprint_scrubber。"""

    code = "DONOT_TRUST_STATIC_PNL"


class ClusterExposureCapError(IronRuleViolation):
    """EventClusterExposureIsolation: 叢集 |Net_Delta| 觸頂後仍試圖加碼（非對沖）。"""

    code = "CLUSTER_EXPOSURE_CAP"


class NonFilledTradeEvidenceError(IronRuleViolation):
    """StateReconciliation: 共識或下單證據鏈不得使用未成交 / 已取消訂單。"""

    code = "STATE_RECONCILIATION_NON_FILLED"


class ClusterCapFrictionRebalanceError(IronRuleViolation):
    """
    AntiFrictionNoCrossRebalance: 禁止在同一叢集內「賣 parent 買 sub」等雙邊摩擦換倉。
    觸發時必須記錄 REJECTED_CLUSTER_FRICTION_LOG。
    """

    code = "CLUSTER_CAP_FRICTION_REBALANCE"

    def __init__(self, message: str, *, audit: dict[str, Any] | None = None) -> None:
        full = f"{REJECTED_CLUSTER_FRICTION_LOG} {message}"
        super().__init__(full)
        self.audit = audit or {}
        logger.warning(full, extra={"audit": self.audit})


def assert_no_macro_bypass(candidate_source: str, scrubbed: bool) -> None:
    """macro_harvester 產出僅在 scrubbed=True 時可進交易鏈。"""
    src = (candidate_source or "").upper()
    if "MACRO" in src or "HARVEST" in src or "RAW" in src:
        if not scrubbed:
            raise StaticPnLBypassError(
                f"candidate_source={candidate_source!r} must pass fingerprint_scrubber before trading"
            )


def assert_filled_trade_rows(trades: list[dict[str, Any]]) -> None:
    """只允許 filled / matched 成交列進入共識聚合。"""
    for i, tr in enumerate(trades):
        status = str(tr.get("status") or tr.get("order_status") or "").upper()
        if status in {"OPEN", "PENDING", "PLACED", "ORDER_PLACED", "CANCELLED", "CANCELED"}:
            raise NonFilledTradeEvidenceError(f"trade[{i}] has non-filled status={status!r}")
        if tr.get("filled") is False:
            raise NonFilledTradeEvidenceError(f"trade[{i}] explicit filled=False")


def assert_no_parent_sub_friction_rebalance(
    *,
    sell_market_id: str | None,
    buy_market_id: str | None,
    cluster_map: dict[str, str],
    market_roles: dict[str, Literal["parent", "child", "leaf"]] | None,
    cluster_cap_breached: bool,
) -> None:
    """
    若叢集已觸 cap，禁止在同一 cluster 內賣 parent 買 child 的動態再平衡。
    market_roles 未提供時僅在兩市場同叢集且一賣一買時記警告型 log（較鬆）；
    有 roles 時嚴格拒絕 parent→child 資金路徑。
    """
    if not cluster_cap_breached or not sell_market_id or not buy_market_id:
        return
    c_s = cluster_map.get(sell_market_id)
    c_b = cluster_map.get(buy_market_id)
    if c_s is None or c_b is None or c_s != c_b:
        return
    if market_roles:
        rs, rb = market_roles.get(sell_market_id), market_roles.get(buy_market_id)
        if rs == "parent" and rb == "child":
            raise ClusterCapFrictionRebalanceError(
                "blocked parent sell + child buy under cluster cap (double spread / friction)",
                audit={
                    "sell_market_id": sell_market_id,
                    "buy_market_id": buy_market_id,
                    "cluster_id": c_s,
                },
            )
    else:
        logger.warning(
            "%s same-cluster sell+buy without roles; verify not a parent/sub rebalance",
            REJECTED_CLUSTER_FRICTION_LOG,
            extra={"sell": sell_market_id, "buy": buy_market_id, "cluster": c_s},
        )
