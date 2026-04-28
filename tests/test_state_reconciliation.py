import unittest

from panopticon_py.state_reconciliation import ChainReconcileQueue, PendingTx


class StateReconciliationTests(unittest.TestCase):
    def test_queue_invokes_callback(self) -> None:
        seen: list[tuple[str, int, str, dict]] = []

        def cb(tx_hash: str, confirmations: int, status: str, meta: dict | None = None) -> None:
            seen.append((tx_hash, confirmations, status, meta or {}))

        q = ChainReconcileQueue(cb)
        q._reconcile_one = lambda *_a, **_k: {  # type: ignore[method-assign]
            "confirmations": 3,
            "settlement_status": "confirmed",
            "mined_block_hash": "0xbb",
            "reorg_suspected": False,
            "exchange_log_ok": None,
            "failure_reason": None,
        }
        q.start()
        q.submit(PendingTx(tx_hash="0xabc", required_confirmations=3))
        q.stop()
        self.assertTrue(any(s[2] == "confirmed" for s in seen))
        self.assertTrue(any(s[3].get("mined_block_hash") == "0xbb" for s in seen))


if __name__ == "__main__":
    unittest.main()
