import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from panopticon_py import l4_ts_bridge


class _Handler(BaseHTTPRequestHandler):
    attempts = 0

    def log_message(self, *_args):  # noqa: ANN001
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/orders:submit":
            self.send_error(404)
            return
        _Handler.attempts += 1
        if _Handler.attempts < 2:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error":"retry"}')
            return
        body = {
            "request_id": "mock",
            "accepted": True,
            "clob_order_id": "c1",
            "tx_hash": "0x" + "a" * 64,
            "raw_error": None,
            "dry_run": True,
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class L4TsBridgeTests(unittest.TestCase):
    def test_retries_then_accepts(self) -> None:
        _Handler.attempts = 0
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            os.environ["L4_SIGN_SUBMIT_URL"] = f"http://127.0.0.1:{port}/v1/orders:submit"
            os.environ["L4_BRIDGE_MAX_RETRIES"] = "4"
            os.environ["L4_BRIDGE_BACKOFF_START_SEC"] = "0.01"
            payload = {
                "idempotency_key": "idem1234567890",
                "decision_id": "decision123456789",
                "protected_payload": {
                    "side": "BUY",
                    "price": 0.5,
                    "size": 1.0,
                    "time_in_force": "FOK",
                    "expires_in_seconds": 10,
                    "expected_avg_price": 0.51,
                    "slippage_tolerance": 0.01,
                },
            }
            out = l4_ts_bridge.submit_order_to_ts(payload, timeout_sec=5.0)
            self.assertTrue(out.accepted)
            self.assertTrue(out.tx_hash and out.tx_hash.startswith("0x"))
            self.assertGreaterEqual(_Handler.attempts, 2)
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=2.0)

    def test_validation_rejects_short_idem(self) -> None:
        out = l4_ts_bridge.submit_order_to_ts({"idempotency_key": "short", "decision_id": "x" * 10, "protected_payload": {}})
        self.assertFalse(out.accepted)
        self.assertIn("bad", (out.raw_error or "").lower())


if __name__ == "__main__":
    unittest.main()
