from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from panopticon_py.contracts import build_event
from panopticon_py.db import AsyncDBWriter, ShadowDB
from panopticon_py.ingestion.insider_ranker import rank_insider
from panopticon_py.ingestion.wallet_features import aggregate_from_observations

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InsiderAnalysisWorker:
    """Periodically aggregates ``clob_trade`` observations and writes insider scores."""

    def __init__(
        self,
        db: ShadowDB,
        writer: AsyncDBWriter,
        *,
        interval_sec: float | None = None,
        version_tag: str = "v0.1.0:ingestion:analysis",
    ) -> None:
        self.db = db
        self.writer = writer
        self.interval_sec = float(interval_sec if interval_sec is not None else os.getenv("INSIDER_ANALYSIS_INTERVAL_SEC", "25"))
        self.version_tag = version_tag
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as exc:
                logger.exception("[ANALYSIS_WORKER] tick failed: %s", exc)
            time.sleep(self.interval_sec)

    def _tick(self) -> None:
        emit_raw = os.getenv("EMIT_INSIDER_RAW_EVENTS", "0").lower() in ("1", "true", "yes")
        min_score = float(os.getenv("INSIDER_EMIT_MIN_SCORE", "0.65"))

        # D72: Log token scope at tick start for diagnostic verification
        t1_count = self.db.conn.execute(
            "SELECT COUNT(DISTINCT token_id) FROM polymarket_link_map WHERE market_tier='t1' AND token_id IS NOT NULL"
        ).fetchone()[0] or 0
        total_obs = self.db.conn.execute(
            "SELECT COUNT(*) FROM wallet_observations WHERE obs_type='clob_trade'"
        ).fetchone()[0] or 0

        addrs = self.db.fetch_distinct_trade_wallets(30)
        logger.info(
            "[D72_ANALYSIS_SCOPE] tick wallets=%d total_obs=%d t1_linkmap_tokens=%d",
            len(addrs), total_obs, t1_count,
        )
        emitted = 0
        for addr in addrs:
            obs = self.db.fetch_recent_wallet_observations(addr, 160)

            # Update wallet market positions via LIFO for each clob_trade observation
            for o in obs:
                if o.get("obs_type") == "clob_trade":
                    payload = o.get("payload", {})
                    side = payload.get("side", "")
                    price = payload.get("price")
                    size = payload.get("size")
                    if side in ("BUY", "SELL") and price is not None and size is not None:
                        self.db.upsert_wallet_market_position_lifo(
                            wallet_address=o["address"].lower(),
                            market_id=o["market_id"],
                            fill_price=float(price),
                            fill_qty=float(size),
                            side=side,
                            updated_ts_utc=o["ingest_ts_utc"],
                        )

            feats = aggregate_from_observations(obs)
            score, reasons = rank_insider(feats)
            if feats.trade_count < 2 and score < 0.06:
                continue
            self.writer.submit(
                "insider_score",
                {
                    "score_id": str(uuid4()),
                    "address": addr,
                    "market_id": None,
                    "score": score,
                    "reasons_json": reasons,
                    "ingest_ts_utc": _utc_now(),
                },
            )
            emitted += 1
            if emit_raw and score >= min_score:
                ev = build_event(
                    layer="L2",
                    event_type="wallet_insider_score",
                    source="ingestion_layer",
                    version_tag=self.version_tag,
                    payload={
                        "address": addr,
                        "insider_risk_score": score,
                        "reasons": reasons,
                        "trade_count": feats.trade_count,
                        "volume_proxy": feats.volume_proxy,
                    },
                    market_id=None,
                    asset_id=None,
                )
                self.writer.submit("raw", ev.to_dict())
        logger.info("[ANALYSIS_WORKER] tick done: emitted %d insider_score snapshots", emitted)


def main() -> int:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db = ShadowDB()
    writer = AsyncDBWriter(db)
    writer.start()  # Must start queue worker before submitting items
    worker = InsiderAnalysisWorker(db, writer)
    worker.start()
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        worker.stop()
        writer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
