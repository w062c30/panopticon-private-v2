import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from panopticon_py.db import AsyncDBWriter, ShadowDB
from panopticon_py.ingestion.clob_client import fetch_book, fetch_trades
from panopticon_py.ingestion.clob_poller import extract_addresses_from_trade
from panopticon_py.ingestion.insider_ranker import rank_insider
from panopticon_py.ingestion.wallet_features import WalletAggFeatures, aggregate_from_observations


class _ClobHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: ANN002
        return

    def do_GET(self) -> None:
        if self.path.startswith("/book"):
            body = {"bids": [], "asks": []}
        elif "trades" in self.path:
            body = [{"maker_address": "0x" + "ab" * 20, "size": 12.5, "asset_id": "tok1"}]
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestObservationIngestion(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server = HTTPServer(("127.0.0.1", 0), _ClobHandler)
        cls._port = cls._server.server_address[1]
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._thread.join(timeout=2.0)

    def setUp(self) -> None:
        self._prev_base = os.environ.get("POLYMARKET_CLOB_BASE")
        os.environ["POLYMARKET_CLOB_BASE"] = f"http://127.0.0.1:{self._port}"

    def tearDown(self) -> None:
        if self._prev_base is None:
            os.environ.pop("POLYMARKET_CLOB_BASE", None)
        else:
            os.environ["POLYMARKET_CLOB_BASE"] = self._prev_base

    def test_clob_client_against_local_http(self) -> None:
        book = fetch_book("tok1")
        self.assertIsInstance(book, dict)
        trades = fetch_trades("tok1")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].get("size"), 12.5)

    def test_extract_addresses_from_trade(self) -> None:
        tr = {"maker_address": "0x" + "cd" * 20, "user": {"address": "0x" + "ef" * 20}}
        addrs = extract_addresses_from_trade(tr)
        self.assertEqual(len(addrs), 2)
        self.assertTrue(all(a.startswith("0x") and len(a) == 42 for a in addrs))

    def test_wallet_features_empty(self) -> None:
        feats = aggregate_from_observations([])
        self.assertEqual(feats.trade_count, 0)
        self.assertEqual(feats.volume_proxy, 0.0)

    def test_insider_ranker_bounds(self) -> None:
        s, r = rank_insider(WalletAggFeatures(0, 0.0, 0, 0.0))
        self.assertEqual(s, 0.0)
        self.assertEqual(r, [])
        s2, _ = rank_insider(WalletAggFeatures(50, 1e9, 5, 1.0))
        self.assertLessEqual(s2, 1.0)

    def test_observation_tables_after_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.db"
            db = ShadowDB(p.as_posix())
            db.bootstrap()
            names = {
                str(r[0])
                for r in db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("watched_wallets", names)
            self.assertIn("wallet_observations", names)
            self.assertIn("insider_score_snapshots", names)
            db.conn.close()

    def test_async_writer_wallet_and_insider_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "w.db"
            db = ShadowDB(p.as_posix())
            db.bootstrap()
            w = AsyncDBWriter(db)
            w.start()
            ts = "2026-04-01T00:00:00+00:00"
            w.submit(
                "watched_wallet",
                {"address": "0x" + "11" * 20, "label": "x", "source": "test", "created_ts_utc": ts, "active": 1},
            )
            w.submit(
                "wallet_observation",
                {
                    "obs_id": str(uuid.uuid4()),
                    "address": "0x" + "11" * 20,
                    "market_id": "m1",
                    "obs_type": "clob_trade",
                    "payload_json": {"trade": {"size": 3}},
                    "ingest_ts_utc": ts,
                },
            )
            w.submit(
                "insider_score",
                {
                    "score_id": str(uuid.uuid4()),
                    "address": "0x" + "11" * 20,
                    "market_id": None,
                    "score": 0.4,
                    "reasons_json": ["test_reason"],
                    "ingest_ts_utc": ts,
                },
            )
            time.sleep(0.3)
            w.stop()
            n_obs = db.conn.execute("SELECT COUNT(*) FROM wallet_observations").fetchone()[0]
            n_sc = db.conn.execute("SELECT COUNT(*) FROM insider_score_snapshots").fetchone()[0]
            self.assertEqual(int(n_obs), 1)
            self.assertEqual(int(n_sc), 1)
            db.conn.close()
