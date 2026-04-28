from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4
import logging
import os
import time

from panopticon_py.cognitive_cache import CognitiveCache, CognitiveWorker
from panopticon_py.committee_shadow import CommitteeMemberScore, append_shadow_observation
from panopticon_py.contracts import build_event
from panopticon_py.correlation_job import run_correlation_tick
from panopticon_py.decision_guard import SignalArtifactMeta, assert_signal_artifacts_contract
from panopticon_py.db import AsyncDBWriter, ShadowDB
from panopticon_py.fast_gate import FastSignalInput, GateDecision, fast_execution_gate
from panopticon_py.friction_state import FrictionStateWorker, GlobalFrictionState
from panopticon_py.l4_ts_bridge import submit_order_to_ts
from panopticon_py.liquidity_asymmetry import OrderBookSlice, bid_ask_imbalance, weighted_ask_entry_price
from panopticon_py.meta_review import PostTradeAttributionQueue
from panopticon_py.order_payload import build_protected_limit_dict
from panopticon_py.portfolio_risk import Position, allocate_kelly_with_correlation
from panopticon_py.state_reconciliation import ChainReconcileQueue, PendingTx
from panopticon_py.strategy import StrategyInput, decide
from panopticon_py.strategy.bayesian_engine import (
    PortfolioPosition,
    check_cluster_exposure_limit,
    load_cluster_mapping_for_engine,
)
from panopticon_py.hunting.semantic_router import load_semantic_router_record
from panopticon_py.load_env import load_repo_env

load_repo_env()

VERSION_TAG = "v0.1.0:bootstrap:nvidia-m2.7"
CLUSTER_ID = os.getenv("PANOPTICON_DEFAULT_CLUSTER_ID", "politics_us_demo")
DRY_RUN = os.getenv("PANOPTICON_DRY_RUN", "1").lower() in ("1", "true", "yes")
logger = logging.getLogger(__name__)
ALLOWED_SIGNAL_SOURCES = {"sensor_layer", "cognitive_layer", "friction_state"}


def run_once() -> None:
    db = ShadowDB()
    db.bootstrap()
    db_writer = AsyncDBWriter(db)
    friction_state = GlobalFrictionState()
    friction_worker = FrictionStateWorker(friction_state)
    cognitive_cache = CognitiveCache()
    cognitive_worker = CognitiveWorker(cognitive_cache)
    attribution_queue = PostTradeAttributionQueue("data/post_trade_attribution.jsonl")

    def _on_chain_reconcile(tx_hash: str, confirmations: int, status: str, meta: dict | None = None) -> None:
        meta = meta or {}
        db_writer.submit(
            "pending_chain",
            {
                "tx_hash": tx_hash,
                "required_confirmations": 3,
                "status": status,
                "confirmations": confirmations,
                "updated_ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        db_writer.submit(
            "settlement_update",
            {
                "tx_hash": tx_hash,
                "confirmations": confirmations,
                "status": status,
                "mined_block_hash": meta.get("mined_block_hash"),
            },
        )
        if status == "confirmed" and not meta.get("reorg_suspected"):
            db_writer.submit("reservation_release_tx", {"tx_hash": tx_hash, "reason": "CHAIN_CONFIRMED"})
        elif status == "failed":
            db_writer.submit(
                "reservation_forfeit_tx",
                {"tx_hash": tx_hash, "reason": meta.get("failure_reason") or "CHAIN_FAILED"},
            )

    chain_queue = ChainReconcileQueue(_on_chain_reconcile)

    friction_worker.start()
    cognitive_worker.start()
    attribution_queue.start()
    db_writer.start()
    chain_queue.start()
    time.sleep(0.15)

    run_correlation_tick(db)

    nominal_capital = float(os.getenv("PANOPTICON_CAPITAL_USDC", "100"))
    reserved = db.sum_active_reserved_usdc()
    capital_in = max(0.0, nominal_capital - reserved)

    l1_event = build_event(
        layer="L1",
        event_type="micro_signal",
        source="sensor_layer",
        version_tag=VERSION_TAG,
        payload={
            "delta_h": -0.12,
            "ofi": 0.41,
            "lambda_impact": 0.018,
            "best_bid": 0.47,
            "best_ask": 0.49,
            "bid1": 0.05,
            "bid2": 0.04,
            "bid3": 0.03,
            "ask1": 0.05,
            "ask2": 0.12,
            "ask3": 0.18,
            "latency_ms": 128.0,
        },
        market_id="demo-market",
        asset_id="demo-token",
    )
    db_writer.submit("raw", l1_event.to_dict())

    l2 = cognitive_cache.get()
    l2_event = build_event(
        layer="L2",
        event_type="cognitive_signal",
        source="cognitive_layer",
        version_tag=VERSION_TAG,
        payload={
            "trust_score": l2.trust_score,
            "signal_reliability": l2.signal_reliability,
            "external_event_score": l2.external_event_score,
            "sentiment_score": l2.sentiment_score,
            "kelly_cap": l2.kelly_cap,
            "degraded": l2.degraded,
            "timeout_ms": l2.timeout_ms,
            "graph_disabled": l2.graph_disabled,
            "funding_risk": l2.funding_risk,
            "behavior_score": l2.behavior_score,
        },
        market_id="demo-market",
        asset_id="demo-token",
    )
    db_writer.submit("raw", l2_event.to_dict())

    snapshot = friction_state.get()
    likelihood_ratio = 1 + l2.external_event_score
    p = l1_event.payload
    book = OrderBookSlice(
        bid1=float(p.get("bid1", p["best_bid"])),
        bid2=float(p.get("bid2", p["best_bid"])),
        bid3=float(p.get("bid3", p["best_bid"])),
        ask1=float(p.get("ask1", p["best_ask"])),
        ask2=float(p.get("ask2", p["best_ask"])),
        ask3=float(p.get("ask3", p["best_ask"])),
    )
    ask_entry = weighted_ask_entry_price(book)
    bai = bid_ask_imbalance(book)
    gate = fast_execution_gate(
        FastSignalInput(
            p_prior=0.49,
            quote_price=ask_entry,
            payout=1.0,
            capital_in=capital_in,
            order_size=100.0,
            delta_t_ms=snapshot.network_ping_ms,
            gamma=0.001,
            slippage_tolerance=0.009,
            min_ev_threshold=0.0,
            daily_opp_cost=0.0008,
            days_to_resolution=3,
            bid_ask_imbalance=bai,
        ),
        snapshot,
    )
    target_market_id = "demo-market"
    cluster_mapping = load_cluster_mapping_for_engine(os.getenv("CLUSTER_MAPPING_PATH"))
    semantic_row = load_semantic_router_record(os.getenv("CLUSTER_MAPPING_PATH"), target_market_id) or {}
    target_cluster = cluster_mapping.get(target_market_id, "UNKNOWN_CLUSTER")
    target_direction = int(semantic_row.get("internal_direction", 1) or 1)
    proposed_notional_usd = abs(float(os.getenv("PANOPTICON_ORDER_SIZE_USD", "100")))
    proposed_signed_notional_usd = proposed_notional_usd if target_direction >= 0 else -proposed_notional_usd

    rows = db.fetch_open_positions()
    inventory = [
        Position(market_id=r["market_id"], cluster_id=r["cluster_id"], kelly_fraction=float(r["kelly_fraction"]))
        for r in rows
    ]
    peers = list({r["market_id"] for r in rows if r.get("market_id") and r["market_id"] != "demo-market"})
    window = int(os.getenv("CORR_WINDOW_SEC", "300"))
    rho_matrix = db.fetch_max_rho("demo-market", peers, window) if peers else None
    base_alpha = min(l2.kelly_cap, gate.kelly_cap)
    alpha = allocate_kelly_with_correlation(
        proposed_kelly=base_alpha,
        cluster_id=CLUSTER_ID,
        inventory=inventory,
        rho_matrix=rho_matrix,
    )
    current_portfolio = [
        PortfolioPosition(
            market_id=str(r["market_id"]),
            signed_notional_usd=float(r.get("signed_notional_usd", 0.0)),
        )
        for r in rows
    ]
    cluster_map_for_risk = dict(cluster_mapping)
    if target_market_id not in cluster_map_for_risk:
        cluster_map_for_risk[target_market_id] = target_cluster
    cluster_anchor = {target_cluster: target_market_id}
    corr_fallback_matrix: dict[tuple[str, str], float] = {}
    for r in rows:
        mkt = str(r["market_id"])
        if not mkt:
            continue
        active_cluster = cluster_mapping.get(mkt, "UNKNOWN_CLUSTER")
        active_side = str(r.get("side") or "YES").upper()
        active_dir = 1 if active_side == "YES" else -1
        if target_cluster == "UNKNOWN_CLUSTER" or active_cluster == "UNKNOWN_CLUSTER":
            continue
        if active_cluster == target_cluster:
            rho = 1.0 if active_dir == target_direction else -1.0
        else:
            rho = 0.0
        corr_fallback_matrix[(mkt, target_market_id)] = rho
        corr_fallback_matrix[(target_market_id, mkt)] = rho
    risk_ok, risk_reason, risk_audit = check_cluster_exposure_limit(
        target_market=target_market_id,
        proposed_signed_notional_usd=proposed_signed_notional_usd,
        portfolio=current_portfolio,
        cluster_map=cluster_map_for_risk,
        cluster_anchor=cluster_anchor,
        correlation_matrix=corr_fallback_matrix,
        total_capital_usd=nominal_capital,
        cluster_cap_fraction=float(os.getenv("CLUSTER_CAP_FRACTION", "0.25")),
        unknown_individual_cap_fraction=float(os.getenv("UNKNOWN_MARKET_CAP_FRACTION", "0.05")),
    )
    risk_rejected = not risk_ok
    if risk_rejected:
        logger.info("[REJECTED_DUE_TO_CLUSTER_CAP] reason=%s audit=%s", risk_reason, risk_audit.__dict__)

    def paper_execution_logger(*, decision_id: str, created_ts_utc: str, kelly_fraction: float, side: str) -> None:
        paper_trade_id = str(uuid4())
        cluster_before = (risk_audit.rho_notes or {}).get("before")
        cluster_after = (risk_audit.rho_notes or {}).get("after")
        estimated_ev_usd = float(gate.ev_net)
        realized_pnl_usd = float(gate.ev_net - abs(gate.ev_net) * 0.15)
        trade_outcome = "win" if realized_pnl_usd > 0 else ("loss" if realized_pnl_usd < 0 else None)
        db_writer.submit(
            "paper_trade",
            {
                "paper_trade_id": paper_trade_id,
                "decision_id": decision_id,
                "wallet_address": "system",
                "market_id": target_market_id,
                "cluster_id": target_cluster,
                "side": side,
                "sizing_notional": proposed_notional_usd,
                "kelly_fraction": float(kelly_fraction),
                "cluster_delta_before": cluster_before,
                "cluster_delta_after": cluster_after,
                "reason": "DRY_RUN_PAPER_EXECUTED",
                "outcome": trade_outcome,
                "created_ts_utc": created_ts_utc,
            },
        )
        db_writer.submit(
            "trade_settlement",
            {
                "trade_id": str(uuid4()),
                "paper_trade_id": paper_trade_id,
                "decision_id": decision_id,
                "market_id": target_market_id,
                "event_name": "demo-market",
                "direction": side,
                "confidence": float(out.posterior_probability),
                "open_reason": "DRY_RUN_PAPER_EXECUTED",
                "close_reason": "AUTO_SETTLE_SIM",
                "close_condition": "dry_run_auto_close",
                "entry_price": float(out.price_used),
                "exit_price": float(out.price_used + (0.01 if realized_pnl_usd > 0 else -0.01)),
                "position_size_usd": proposed_notional_usd,
                "estimated_ev_usd": estimated_ev_usd,
                "realized_pnl_usd": realized_pnl_usd,
                "opened_ts_utc": created_ts_utc,
                "closed_ts_utc": created_ts_utc,
                "source_event": l3_event.event_id,
            },
        )
        logger.info(
            "[DRY_RUN] paper trade logged size=%s cluster_delta_before=%s cluster_delta_after=%s ts=%s",
            proposed_notional_usd,
            cluster_before,
            cluster_after,
            created_ts_utc,
        )

    if gate.decision == GateDecision.ABORT or risk_rejected:
        assert_signal_artifacts_contract(
            [
                SignalArtifactMeta(
                    name="l1_event",
                    source=l1_event.source,
                    timestamp=l1_event.ingest_ts_utc,
                    version=l1_event.version_tag,
                ),
                SignalArtifactMeta(
                    name="l2_event",
                    source=l2_event.source,
                    timestamp=l2_event.ingest_ts_utc,
                    version=l2_event.version_tag,
                ),
                SignalArtifactMeta(
                    name="friction_snapshot",
                    source="friction_state",
                    timestamp=f"{snapshot.last_update_ts}",
                    version=VERSION_TAG,
                ),
            ],
            allowed_sources=ALLOWED_SIGNAL_SOURCES,
        )
        out = decide(
            StrategyInput(
                prior_probability=0.49,
                likelihood_ratio=0.9,
                price=(book.ask1 + book.bid1) / 2,
                fee_rate=snapshot.current_base_fee,
                slippage_pct=gate.expected_slippage,
                alpha=0.0,
                ask_entry_price=ask_entry,
                bid_exit_price=book.bid1,
                allow_trade=False,
            )
        )
    else:
        assert_signal_artifacts_contract(
            [
                SignalArtifactMeta(
                    name="l1_event",
                    source=l1_event.source,
                    timestamp=l1_event.ingest_ts_utc,
                    version=l1_event.version_tag,
                ),
                SignalArtifactMeta(
                    name="l2_event",
                    source=l2_event.source,
                    timestamp=l2_event.ingest_ts_utc,
                    version=l2_event.version_tag,
                ),
                SignalArtifactMeta(
                    name="friction_snapshot",
                    source="friction_state",
                    timestamp=f"{snapshot.last_update_ts}",
                    version=VERSION_TAG,
                ),
            ],
            allowed_sources=ALLOWED_SIGNAL_SOURCES,
        )
        out = decide(
            StrategyInput(
                prior_probability=gate.p_adjusted,
                likelihood_ratio=likelihood_ratio,
                price=(book.ask1 + book.bid1) / 2,
                fee_rate=snapshot.current_base_fee,
                slippage_pct=gate.expected_slippage,
                alpha=alpha,
                ask_entry_price=ask_entry,
                bid_exit_price=book.bid1,
                allow_trade=True,
            )
        )

    l3_event = build_event(
        layer="L3",
        event_type="strategy_decision",
        source="strategy_layer",
        version_tag=VERSION_TAG,
        payload=asdict(out),
        market_id="demo-market",
        asset_id="demo-token",
    )
    db_writer.submit("raw", l3_event.to_dict())
    db.append_raw_event(l3_event.to_dict())

    decision_id = str(uuid4())
    created_ts = l3_event.ingest_ts_utc

    shadow_experiment_id = os.getenv("PANOPTICON_SHADOW_EXPERIMENT_ID", "").strip()
    if shadow_experiment_id:
        # Shadow committee output is audit-only and must not alter actions.
        append_shadow_observation(
            output_path=os.getenv(
                "PANOPTICON_SHADOW_COMMITTEE_OUT",
                "data/committee_shadow_scores.jsonl",
            ),
            experiment_id=shadow_experiment_id,
            decision_id=decision_id,
            market_id=target_market_id,
            members=[
                CommitteeMemberScore(model="baseline_posterior", score=float(out.posterior_probability)),
                CommitteeMemberScore(model="fast_gate_adjusted_p", score=float(gate.p_adjusted)),
                CommitteeMemberScore(model="cognitive_external_event", score=float(l2.external_event_score)),
            ],
        )

    db.append_strategy_decision(
        {
            "decision_id": decision_id,
            "event_id": l3_event.event_id,
            "feature_snapshot_id": l2_event.event_id,
            "market_snapshot_id": l1_event.event_id,
            "prior_probability": gate.p_adjusted,
            "likelihood_ratio": likelihood_ratio,
            "posterior_probability": out.posterior_probability,
            "ev_net": gate.ev_net,
            "kelly_fraction": min(out.kelly_fraction, gate.kelly_cap),
            "action": out.action,
            "created_ts_utc": created_ts,
        }
    )

    use_ts_bridge = os.getenv("PANOPTICON_USE_TS_BRIDGE", "0").lower() in ("1", "true", "yes")
    reserve_usdc = float(os.getenv("PANOPTICON_RESERVE_USDC", "100"))

    if out.action == "BUY" and gate.decision != GateDecision.ABORT and not risk_rejected:
        execution_id = str(uuid4())
        reservation_id = str(uuid4())
        idem_key = execution_id.replace("-", "")[:32]
        try:
            db.atomic_execution_and_reserve(
                execution={
                    "execution_id": execution_id,
                    "decision_id": decision_id,
                    "accepted": 1,
                    "reason": "PENDING_SUBMIT",
                    "friction_snapshot_id": f"{snapshot.last_update_ts}",
                    "gate_reason": gate.reason,
                    "latency_bucket": "gt200" if snapshot.network_ping_ms > 200 else "lte200",
                    "toxicity_tag": "none",
                    "tx_hash": None,
                    "settlement_status": "pending_submit",
                    "confirmations": None,
                    "simulated_fill_price": out.price_used,
                    "simulated_fill_size": 100.0,
                    "impact_pct": gate.expected_slippage,
                    "latency_ms": snapshot.network_ping_ms,
                    "created_ts_utc": created_ts,
                    "reservation_reason": "PRE_SUBMIT_LOCK",
                },
                reservation_id=reservation_id,
                amount_usdc=reserve_usdc,
                idempotency_key=idem_key,
                created_ts_utc=created_ts,
            )
        except Exception:
            db_writer.submit(
                "execution",
                {
                    "execution_id": str(uuid4()),
                    "decision_id": decision_id,
                    "accepted": 0,
                    "reason": "RESERVE_FAILED",
                    "friction_snapshot_id": f"{snapshot.last_update_ts}",
                    "gate_reason": gate.reason,
                    "latency_bucket": "gt200" if snapshot.network_ping_ms > 200 else "lte200",
                    "toxicity_tag": "none",
                    "tx_hash": None,
                    "settlement_status": None,
                    "confirmations": None,
                    "simulated_fill_price": out.price_used,
                    "simulated_fill_size": 0.0,
                    "impact_pct": gate.expected_slippage,
                    "latency_ms": snapshot.network_ping_ms,
                    "created_ts_utc": created_ts,
                },
            )
        else:
            tx_hash: str | None = None
            clob_id: str | None = None
            if DRY_RUN:
                paper_execution_logger(
                    decision_id=decision_id,
                    created_ts_utc=created_ts,
                    kelly_fraction=min(out.kelly_fraction, gate.kelly_cap),
                    side="YES" if target_direction >= 0 else "NO",
                )
                db.update_execution_post_submit(
                    execution_id,
                    tx_hash=None,
                    clob_order_id=None,
                    settlement_status="paper_dry_run",
                    accepted=1,
                    reason="DRY_RUN_PAPER_EXECUTED",
                )
            elif use_ts_bridge:
                pp = build_protected_limit_dict(
                    side="BUY",
                    quote_price=ask_entry,
                    order_size=100.0,
                    slippage_tolerance=float(gate.expected_slippage),
                    kyle_lambda=float(snapshot.kyle_lambda),
                )
                br = submit_order_to_ts(
                    {
                        "idempotency_key": idem_key,
                        "decision_id": decision_id,
                        "market_id": "demo-market",
                        "asset_id": "demo-token",
                        "protected_payload": pp,
                    }
                )
                if br.accepted:
                    tx_hash = br.tx_hash
                    clob_id = br.clob_order_id
                    db.update_execution_post_submit(
                        execution_id,
                        tx_hash=tx_hash,
                        clob_order_id=clob_id,
                        settlement_status="pending_chain",
                        accepted=1,
                        reason="SUBMITTED",
                    )
                else:
                    db.update_execution_post_submit(
                        execution_id,
                        tx_hash=None,
                        clob_order_id=None,
                        settlement_status="bridge_failed",
                        accepted=0,
                        reason="BRIDGE_REJECTED",
                    )
                    db.update_reservation_status(execution_id, "FORFEITED", br.raw_error or "bridge")
            else:
                tx_hash = f"0x{'f' * 64}"
                db.update_execution_post_submit(
                    execution_id,
                    tx_hash=tx_hash,
                    clob_order_id=None,
                    settlement_status="pending_chain",
                    accepted=1,
                    reason="PAPER_TX_SIM",
                )

            if tx_hash:
                chain_queue.submit(PendingTx(tx_hash=tx_hash, required_confirmations=3))
                db_writer.submit(
                    "position",
                    {
                        "position_id": str(uuid4()),
                        "market_id": target_market_id,
                        "cluster_id": target_cluster,
                        "side": "YES" if target_direction >= 0 else "NO",
                        "signed_notional_usd": proposed_signed_notional_usd,
                        "kelly_fraction": min(out.kelly_fraction, gate.kelly_cap),
                        "opened_ts_utc": created_ts,
                    },
                )
    else:
        db_writer.submit(
            "execution",
            {
                "execution_id": str(uuid4()),
                "decision_id": decision_id,
                "accepted": 0,
                "reason": (
                    "REJECTED_DUE_TO_CLUSTER_CAP"
                    if risk_rejected
                    else ("SKIPPED" if out.action != "BUY" else "GATE_ABORT")
                ),
                "friction_snapshot_id": f"{snapshot.last_update_ts}",
                "gate_reason": gate.reason,
                "latency_bucket": "gt200" if snapshot.network_ping_ms > 200 else "lte200",
                "toxicity_tag": "none",
                "tx_hash": None,
                "settlement_status": None,
                "confirmations": None,
                "simulated_fill_price": out.price_used,
                "simulated_fill_size": 0.0,
                "impact_pct": gate.expected_slippage,
                "latency_ms": snapshot.network_ping_ms,
                "created_ts_utc": created_ts,
            },
        )

    attribution_queue.submit(
        {
            "decision_id": decision_id,
            "friction_snapshot": asdict(snapshot),
            "theoretical_ev_net": gate.ev_net,
            "theoretical_ev_time_adj": gate.ev_time_adj,
            "actual_action": out.action,
            "actual_price": out.price_used,
        }
    )

    time.sleep(0.6)
    attribution_queue.stop()
    chain_queue.stop()
    time.sleep(0.2)
    db_writer.stop()
    cognitive_worker.stop()
    friction_worker.stop()
    db.close()


if __name__ == "__main__":
    run_once()
    print("Panopticon_Main_Loop (paper mode) bootstrap run completed.")
