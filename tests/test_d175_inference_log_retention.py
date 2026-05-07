from pathlib import Path

from panopticon_py.db import ShadowDB


def _count_logs(db: ShadowDB) -> int:
    row = db.conn.execute("SELECT COUNT(*) AS c FROM insider_score_inference_log").fetchone()
    return int(row["c"])


def test_prune_inference_log_keeps_recent_rows(tmp_path: Path):
    db = ShadowDB((tmp_path / "d175_retention.db").as_posix())
    db.bootstrap()
    try:
        db.conn.execute(
            """
            INSERT INTO insider_score_inference_log (wallet_address, inference_payload, created_at)
            VALUES ('0xold', '{}', datetime('now', '-31 days'))
            """
        )
        db.conn.execute(
            """
            INSERT INTO insider_score_inference_log (wallet_address, inference_payload, created_at)
            VALUES ('0xnew', '{}', datetime('now', '-1 days'))
            """
        )
        db.conn.commit()

        deleted = db.prune_insider_score_inference_log(days=30)
        assert deleted == 1
        assert _count_logs(db) == 1

        row = db.conn.execute(
            "SELECT wallet_address FROM insider_score_inference_log LIMIT 1"
        ).fetchone()
        assert row["wallet_address"] == "0xnew"
    finally:
        db.close()


def test_prune_inference_log_is_repeatable(tmp_path: Path):
    db = ShadowDB((tmp_path / "d175_retention_repeatable.db").as_posix())
    db.bootstrap()
    try:
        db.conn.execute(
            """
            INSERT INTO insider_score_inference_log (wallet_address, inference_payload, created_at)
            VALUES ('0xnew', '{}', datetime('now'))
            """
        )
        db.conn.commit()

        first = db.prune_insider_score_inference_log(days=30)
        second = db.prune_insider_score_inference_log(days=30)
        assert first == 0
        assert second == 0
        assert _count_logs(db) == 1
    finally:
        db.close()
