from __future__ import annotations

import json
import os
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, request


@dataclass(frozen=True)
class PendingTx:
    tx_hash: str
    required_confirmations: int


@dataclass
class _TxWatch:
    """Per-tx memory for simplified reorg detection."""

    last_block_number: int | None = None
    last_block_hash: str | None = None
    had_receipt: bool = False


ChainReconcileCallback = Callable[[str, int, str, dict[str, Any]], None]


@dataclass
class RpcReceiptView:
    block_number: int
    block_hash: str
    status_ok: bool
    logs: list[dict[str, Any]] = field(default_factory=list)


class ChainReconcileQueue:
    """Background reconciliation: confirmations, mined block hash, optional exchange log verify, reorg hints."""

    def __init__(self, db_callback: ChainReconcileCallback) -> None:
        self._q: queue.Queue[PendingTx] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._db_callback = db_callback
        self._rpc_url = os.getenv("POLYGON_RPC_URL")
        self._watch: dict[str, _TxWatch] = {}
        self._exchange_addr = (os.getenv("POLYMARKET_CTF_EXCHANGE_ADDRESS") or "").lower()
        self._fill_topic0 = (os.getenv("POLYMARKET_EXCHANGE_FILL_TOPIC0") or "").lower()
        self._verify_logs = os.getenv("POLYMARKET_VERIFY_EXCHANGE_LOGS", "").lower() in ("1", "true", "yes")

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def submit(self, pending: PendingTx) -> None:
        self._q.put(pending)

    def _loop(self) -> None:
        while self._running:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            meta = self._reconcile_one(item.tx_hash, item.required_confirmations)
            status = str(meta.get("settlement_status", "pending"))
            confirmations = int(meta.get("confirmations", 0))
            self._db_callback(item.tx_hash, confirmations, status, meta)

    def _reconcile_one(self, tx_hash: str, required_confirmations: int) -> dict[str, Any]:
        if not self._rpc_url:
            return {
                "confirmations": required_confirmations,
                "settlement_status": "confirmed",
                "mined_block_hash": None,
                "reorg_suspected": False,
                "exchange_log_ok": None,
                "failure_reason": None,
            }

        receipt = self._fetch_receipt(tx_hash)
        watch = self._watch.setdefault(tx_hash, _TxWatch())

        if receipt is None:
            if watch.had_receipt:
                return {
                    "confirmations": 0,
                    "settlement_status": "reorg_suspected",
                    "mined_block_hash": watch.last_block_hash,
                    "reorg_suspected": True,
                    "exchange_log_ok": None,
                    "failure_reason": "receipt_disappeared",
                }
            return {
                "confirmations": 0,
                "settlement_status": "pending",
                "mined_block_hash": None,
                "reorg_suspected": False,
                "exchange_log_ok": None,
                "failure_reason": None,
            }

        if not receipt.status_ok:
            return {
                "confirmations": 0,
                "settlement_status": "failed",
                "mined_block_hash": receipt.block_hash,
                "reorg_suspected": False,
                "exchange_log_ok": False,
                "failure_reason": "receipt_reverted",
            }

        if watch.had_receipt and watch.last_block_hash and watch.last_block_number is not None:
            if receipt.block_hash != watch.last_block_hash or receipt.block_number != watch.last_block_number:
                return {
                    "confirmations": 0,
                    "settlement_status": "reorg_suspected",
                    "mined_block_hash": receipt.block_hash,
                    "reorg_suspected": True,
                    "exchange_log_ok": None,
                    "failure_reason": "head_changed",
                }

        watch.had_receipt = True
        watch.last_block_hash = receipt.block_hash
        watch.last_block_number = receipt.block_number

        latest = self._fetch_block_number()
        confirmations = max(0, latest - receipt.block_number + 1) if latest >= receipt.block_number else 0

        exchange_log_ok: bool | None = None
        if self._verify_logs and self._exchange_addr and self._fill_topic0:
            exchange_log_ok = self._logs_contain_fill(receipt)

        meta = {
            "confirmations": confirmations,
            "settlement_status": "confirmed" if confirmations >= required_confirmations else "pending",
            "mined_block_hash": receipt.block_hash,
            "reorg_suspected": False,
            "exchange_log_ok": exchange_log_ok,
            "failure_reason": None,
        }
        if exchange_log_ok is False:
            meta["settlement_status"] = "pending"
            meta["failure_reason"] = "exchange_log_mismatch"
        return meta

    def _logs_contain_fill(self, receipt: RpcReceiptView) -> bool:
        for lg in receipt.logs:
            addr = str(lg.get("address", "")).lower()
            topics = lg.get("topics") or []
            if not topics:
                continue
            t0 = str(topics[0]).lower()
            if addr == self._exchange_addr and t0 == self._fill_topic0:
                return True
        return False

    def _rpc(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(self._rpc_url or "", data=data, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("error"):
            return None
        return body.get("result")

    def _fetch_receipt(self, tx_hash: str) -> RpcReceiptView | None:
        result = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if not result or not isinstance(result, dict):
            return None
        block_hex = result.get("blockNumber")
        bh = result.get("blockHash")
        if not block_hex or not bh:
            return None
        status_hex = result.get("status", "0x1")
        status_ok = int(status_hex, 16) == 1
        logs = result.get("logs") or []
        if not isinstance(logs, list):
            logs = []
        return RpcReceiptView(
            block_number=int(block_hex, 16),
            block_hash=str(bh),
            status_ok=status_ok,
            logs=[x for x in logs if isinstance(x, dict)],
        )

    def _fetch_block_number(self) -> int:
        result = self._rpc("eth_blockNumber", [])
        if not result or not isinstance(result, str):
            return 0
        return int(result, 16)
