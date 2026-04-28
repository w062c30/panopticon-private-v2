"""Tests for kyle_lambda_samples table, append_kyle_lambda_sample, and get_kyle_lambda_p75."""
import tempfile
import unittest
from pathlib import Path

from panopticon_py.db import ShadowDB


class TestKyleLambdaSamples(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "kyle.db"
        self.db = ShadowDB(str(self.db_path))
        self.db.bootstrap()

    def tearDown(self) -> None:
        self.db.conn.close()
        self.td.cleanup()

    def test_append_and_retrieve_kyle_lambda_sample(self) -> None:
        """Append a kyle_lambda_sample and verify it can be retrieved."""
        self.db.append_kyle_lambda_sample({
            "asset_id": "0x1234",
            "ts_utc": "2026-04-24T00:00:00+00:00",
            "delta_price": 0.01,
            "trade_size": 100.0,
            "lambda_obs": 0.0001,
            "market_id": "0xabcd",
            "created_at": "2026-04-24T00:00:00+00:00",
        })
        row = self.db.conn.execute(
            "SELECT asset_id, delta_price, trade_size, lambda_obs FROM kyle_lambda_samples"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "0x1234")
        self.assertAlmostEqual(row[1], 0.01)
        self.assertAlmostEqual(row[2], 100.0)
        self.assertAlmostEqual(row[3], 0.0001)

    def test_get_kyle_lambda_p75_returns_none_when_insufficient_data(self) -> None:
        """P75 returns None when fewer than 10 samples available."""
        for i in range(9):
            self.db.append_kyle_lambda_sample({
                "asset_id": "0xtest",
                "ts_utc": "2026-04-24T00:00:00+00:00",
                "delta_price": 0.01,
                "trade_size": 100.0,
                "lambda_obs": 0.0001 + i * 0.00001,
                "market_id": None,
            })
        result = self.db.get_kyle_lambda_p75("0xtest", days=7)
        self.assertIsNone(result)

    def test_get_kyle_lambda_p75_returns_correct_percentile(self) -> None:
        """P75 of sorted lambda_obs values is returned correctly."""
        asset = "0xp75test"
        # Insert 20 samples with known lambda_obs values
        for i in range(20):
            self.db.append_kyle_lambda_sample({
                "asset_id": asset,
                "ts_utc": "2026-04-24T00:00:00+00:00",
                "delta_price": 0.01,
                "trade_size": 100.0,
                "lambda_obs": 0.0001 * (i + 1),  # 0.0001, 0.0002, ..., 0.0020
                "market_id": None,
            })
        result = self.db.get_kyle_lambda_p75(asset, days=7)
        self.assertIsNotNone(result)
        # P75 index = int(20 * 0.75) = 15 (0-indexed) → 16th smallest = 0.0001 * 16 = 0.0016
        self.assertAlmostEqual(result, 0.0016, places=7)

    def test_get_kyle_lambda_p75_returns_none_for_unknown_asset(self) -> None:
        """P75 returns None for an asset with no samples."""
        result = self.db.get_kyle_lambda_p75("0xnonexistent", days=7)
        self.assertIsNone(result)

    def test_append_kyle_lambda_normalizes_epoch_ms_ts_to_iso(self) -> None:
        """D30: numeric epoch-ms ts_utc must be normalized for datetime queries."""
        self.db.append_kyle_lambda_sample({
            "asset_id": "0xepoch",
            "ts_utc": "1777093740517",
            "delta_price": 0.01,
            "trade_size": 100.0,
            "lambda_obs": 0.0001,
            "market_id": "0xepoch",
        })
        row = self.db.conn.execute(
            "SELECT ts_utc FROM kyle_lambda_samples WHERE asset_id='0xepoch' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("T", row[0])


class TestKyleLambdaSamplesTableSchema(unittest.TestCase):
    def test_kyle_lambda_samples_table_exists(self) -> None:
        """The kyle_lambda_samples table is created by bootstrap."""
        with tempfile.TemporaryDirectory() as td:
            db = ShadowDB(Path(td) / "k.db")
            db.bootstrap()
            tables = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]
            self.assertIn("kyle_lambda_samples", table_names)
            db.conn.close()

    def test_kyle_lambda_samples_index_exists(self) -> None:
        """The idx_kls_asset_ts index is created by bootstrap."""
        with tempfile.TemporaryDirectory() as td:
            db = ShadowDB(Path(td) / "k.db")
            db.bootstrap()
            indexes = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            index_names = [i[0] for i in indexes]
            self.assertIn("idx_kls_asset_ts", index_names)
            db.conn.close()

    def test_source_column_defaults_to_standalone(self) -> None:
        """append_kyle_lambda_sample defaults source to 'standalone' when not provided."""
        with tempfile.TemporaryDirectory() as td:
            db = ShadowDB(Path(td) / "k.db")
            db.bootstrap()
            db.append_kyle_lambda_sample({
                "asset_id": "0xtest",
                "ts_utc": "2026-04-24T00:00:00+00:00",
                "delta_price": 0.01,
                "trade_size": 100.0,
                "lambda_obs": 0.0001,
                "market_id": None,
            })
            row = db.conn.execute(
                "SELECT source FROM kyle_lambda_samples"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "standalone")
            db.conn.close()

    def test_source_column_can_be_set_to_book_embedded(self) -> None:
        """append_kyle_lambda_sample stores explicit source value (e.g. 'book_embedded')."""
        with tempfile.TemporaryDirectory() as td:
            db = ShadowDB(Path(td) / "k.db")
            db.bootstrap()
            db.append_kyle_lambda_sample({
                "asset_id": "0xtest",
                "ts_utc": "2026-04-24T00:00:00+00:00",
                "delta_price": 0.01,
                "trade_size": 100.0,
                "lambda_obs": 0.0001,
                "market_id": None,
                "source": "book_embedded",
            })
            row = db.conn.execute(
                "SELECT source FROM kyle_lambda_samples"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "book_embedded")
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
