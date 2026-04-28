import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from panopticon_py.db import ShadowDB


def _seed_decision(db: ShadowDB, decision_id: str) -> tuple[str, str, str]:
    eid = str(uuid.uuid4())
    fid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    ts = "2026-01-01T00:00:00+00:00"
    db.append_raw_event(
        {
            "event_id": eid,
            "layer": "L1",
            "event_type": "micro_signal",
            "source": "test",
            "source_event_id": None,
            "event_ts": ts,
            "ingest_ts_utc": ts,
            "version_tag": "test",
            "market_id": "m1",
            "asset_id": "a1",
            "payload": {"x": 1},
        }
    )
    db.append_raw_event(
        {
            "event_id": fid,
            "layer": "L2",
            "event_type": "cognitive_signal",
            "source": "test",
            "source_event_id": None,
            "event_ts": ts,
            "ingest_ts_utc": ts,
            "version_tag": "test",
            "market_id": "m1",
            "asset_id": "a1",
            "payload": {"y": 1},
        }
    )
    db.append_raw_event(
        {
            "event_id": mid,
            "layer": "L1",
            "event_type": "micro_signal",
            "source": "test",
            "source_event_id": None,
            "event_ts": ts,
            "ingest_ts_utc": ts,
            "version_tag": "test",
            "market_id": "m1",
            "asset_id": "a1",
            "payload": {"z": 1},
        }
    )
    db.append_strategy_decision(
        {
            "decision_id": decision_id,
            "event_id": mid,
            "feature_snapshot_id": fid,
            "market_snapshot_id": eid,
            "prior_probability": 0.5,
            "likelihood_ratio": 1.0,
            "posterior_probability": 0.5,
            "ev_net": 0.0,
            "kelly_fraction": 0.1,
            "action": "BUY",
            "created_ts_utc": ts,
        }
    )
    return eid, fid, mid


class DbLedgerTests(unittest.TestCase):
    def test_atomic_reserve_and_sum(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.db"
            db = ShadowDB(path.as_posix())
            try:
                db.bootstrap()
                did = str(uuid.uuid4())
                _seed_decision(db, did)
                ex = str(uuid.uuid4())
                db.atomic_execution_and_reserve(
                    execution={
                        "execution_id": ex,
                        "decision_id": did,
                        "accepted": 1,
                        "reason": "TEST",
                        "friction_snapshot_id": None,
                        "gate_reason": None,
                        "latency_bucket": None,
                        "toxicity_tag": None,
                        "tx_hash": None,
                        "settlement_status": "pending_submit",
                        "confirmations": None,
                        "simulated_fill_price": 0.5,
                        "simulated_fill_size": 10.0,
                        "impact_pct": 0.0,
                        "latency_ms": 1.0,
                        "created_ts_utc": "2026-01-01T00:00:00+00:00",
                    },
                    reservation_id=str(uuid.uuid4()),
                    amount_usdc=25.0,
                    idempotency_key="idem-1",
                    created_ts_utc="2026-01-01T00:00:00+00:00",
                )
                self.assertAlmostEqual(db.sum_active_reserved_usdc(), 25.0)
                db.conn.execute("UPDATE execution_records SET tx_hash = ? WHERE execution_id = ?", ("0xabc", ex))
                db.conn.commit()
                db.release_reservations_by_tx_hash("0xabc")
                self.assertAlmostEqual(db.sum_active_reserved_usdc(), 0.0)
            finally:
                db.close()

    def test_parallel_independent_reserves_total(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.db"
            barrier = threading.Barrier(2)

            def worker(offset: int) -> None:
                db = ShadowDB(path.as_posix())
                try:
                    db.bootstrap()
                    barrier.wait()
                    did = str(uuid.uuid4())
                    _seed_decision(db, did)
                    ex = str(uuid.uuid4())
                    db.atomic_execution_and_reserve(
                        execution={
                            "execution_id": ex,
                            "decision_id": did,
                            "accepted": 1,
                            "reason": "TEST",
                            "latency_ms": 1.0,
                            "created_ts_utc": "2026-01-01T00:00:00+00:00",
                        },
                        reservation_id=str(uuid.uuid4()),
                        amount_usdc=10.0 + offset,
                        idempotency_key=f"idem-{offset}",
                        created_ts_utc="2026-01-01T00:00:00+00:00",
                    )
                finally:
                    db.close()

            init = ShadowDB(path.as_posix())
            try:
                init.bootstrap()
            finally:
                init.close()
            t1 = threading.Thread(target=worker, args=(0,))
            t2 = threading.Thread(target=worker, args=(1,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            verify = ShadowDB(path.as_posix())
            try:
                self.assertAlmostEqual(verify.sum_active_reserved_usdc(), 21.0)
            finally:
                verify.close()


if __name__ == "__main__":
    unittest.main()
