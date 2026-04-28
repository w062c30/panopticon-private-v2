from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from panopticon_py.execution.constants import (
    REASON_CLOB_REJECT,
    REASON_DRY_RUN,
    REASON_PASS,
    REASON_PING_ABORT,
)
from panopticon_py.friction_state import GlobalFrictionState

PING_ABORT_THRESHOLD_MS = float(os.getenv("PING_ABORT_THRESHOLD_MS", "250"))
CLOB_SUBMIT_URL = os.getenv("POLYMARKET_CLOB_SUBMIT_URL", "https://clob.polymarket.com/order").strip()
SIGNER_PRIVATE_KEY = os.getenv("CLOB_SIGNER_PRIVATE_KEY", "").strip()


@dataclass(frozen=True)
class CLOBSubmitResult:
    accepted: bool
    clob_order_id: str | None
    tx_hash: str | None
    reason: str
    request_id: str
    dry_run: bool
    status_code: int


def _get_cached_ping_ms(state: GlobalFrictionState | None) -> float:
    """Read network_ping_ms from friction state. Returns 0.0 if state is None."""
    if state is None:
        return 0.0
    return state.get().network_ping_ms


def sign_eip712(order: dict[str, Any], private_key: str) -> dict[str, Any]:
    """
    Produce EIP-712 signature over the order dict using the provided private key.

    Falls back to a deterministic mock signature when the key is empty
    (dev / dry-run mode only).
    """
    if not private_key:
        order["signature"] = "0x" + ("00" * 65)
        return order

    try:
        from eth_keys import keys as eth_keys
    except ImportError:
        order["signature"] = "0x" + ("ab" * 65)
        return order

    pk_str = private_key if private_key.startswith("0x") else "0x" + private_key
    try:
        pk = eth_keys.PrivateKey(bytes.fromhex(pk_str[2:]))
        msg = json.dumps(order, ensure_ascii=False, sort_keys=True).encode("utf-8")
        sig = pk.sign_msg(msg)
        order["signature"] = "0x" + sig.to_bytes().hex()
    except Exception:
        order["signature"] = "0x" + ("ff" * 65)

    return order


async def submit_fok_order(
    market_id: str,
    token_id: str,
    side: Literal["BUY", "SELL"],
    size: float,
    price: float,
    decision_id: str,
    *,
    private_key: str | None = None,
    state: GlobalFrictionState | None = None,
    dry_run: bool = True,
    timeout_sec: float = 12.0,
) -> CLOBSubmitResult:
    """
    Submit a Fill-Or-Kill (FOK) order to the Polymarket CLOB.

    Phase 2-C-2: DB write responsibility has moved to signal_engine._process_event().
    This function returns CLOBSubmitResult only — it does NOT write execution_records.

    Parameters
    ----------
    market_id, token_id, side, size, price
        Standard order parameters.
    decision_id : str
        Idempotency / audit trail key.
    private_key : str | None
        EIP-712 signing key. Reads from CLOB_SIGNER_PRIVATE_KEY env if None.
    state : GlobalFrictionState | None
        Friction state for ping circuit breaker. If None, ping check is bypassed.
    dry_run : bool, default True
        If True, do not actually send to the CLOB; return a mock accepted result.
    timeout_sec : float, default 12.0

    Invariants
    ----------
    - orderType is **always** "FOK", hard-coded, never accepted as a parameter.
    - Latency circuit breaker: if cached ping > PING_ABORT_THRESHOLD_MS → ABORT.
    - NEVER writes execution_records — caller (_process_event) is responsible for that.
    """

    # ── Circuit breaker ──────────────────────────────────────────────────────
    ping_ms = _get_cached_ping_ms(state)
    if ping_ms > PING_ABORT_THRESHOLD_MS:
        return CLOBSubmitResult(
            accepted=False,
            clob_order_id=None,
            tx_hash=None,
            reason=REASON_PING_ABORT,
            request_id="",
            dry_run=False,
            status_code=0,
        )

    # ── Side validation ───────────────────────────────────────────────────────
    eff_side = side.upper()
    if eff_side not in ("BUY", "SELL"):
        return CLOBSubmitResult(
            accepted=False,
            clob_order_id=None,
            tx_hash=None,
            reason=f"invalid_side:{side}",
            request_id="",
            dry_run=False,
            status_code=0,
        )

    # ── Build order payload ────────────────────────────────────────────────────
    order: dict[str, Any] = {
        "orderType": "FOK",
        "marketId": market_id,
        "tokenId": token_id,
        "side": eff_side,
        "size": size,
        "price": price,
        "nonce": int(time.time() * 1000),
    }

    pk = private_key or SIGNER_PRIVATE_KEY
    if not pk and not dry_run:
        return CLOBSubmitResult(
            accepted=False,
            clob_order_id=None,
            tx_hash=None,
            reason=REASON_CLOB_REJECT,
            request_id="",
            dry_run=False,
            status_code=0,
        )

    order = sign_eip712(order, pk)

    request_id = f"py_{int(time.time() * 1000)}"
    idem_key = decision_id.replace("-", "")[:32]
    body = {
        "idempotency_key": idem_key,
        "decision_id": decision_id,
        "market_id": market_id,
        "asset_id": token_id,
        "protected_payload": order,
    }

    # ── Dry run ─────────────────────────────────────────────────────────────
    if dry_run:
        import uuid
        return CLOBSubmitResult(
            accepted=True,
            clob_order_id=f"mock_clob_{uuid.uuid4().hex[:12]}",
            tx_hash="0x" + ("ab" * 32),
            reason=REASON_DRY_RUN,
            request_id=request_id,
            dry_run=True,
            status_code=200,
        )

    # ── Live submission ───────────────────────────────────────────────────────
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_status = 0
    last_err: str | None = None
    max_attempts = 3
    delay = 0.08

    for attempt in range(max_attempts):
        req = urllib.request.Request(
            CLOB_SUBMIT_URL,
            data=body_bytes,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Request-ID": request_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                last_status = int(resp.status or resp.getcode() or 200)
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    return CLOBSubmitResult(
                        False, None, None, REASON_CLOB_REJECT, request_id, False, last_status
                    )
                return CLOBSubmitResult(
                    accepted=bool(data.get("accepted")),
                    clob_order_id=data.get("clob_order_id"),
                    tx_hash=data.get("tx_hash"),
                    reason=REASON_PASS if data.get("accepted") else REASON_CLOB_REJECT,
                    request_id=str(data.get("request_id") or request_id),
                    dry_run=False,
                    status_code=last_status,
                )
        except urllib.error.HTTPError as e:
            last_status = e.code
            try:
                body_err = e.read().decode("utf-8")
                last_err = body_err[:512]
            except Exception:
                last_err = str(e)
            if e.code in (429, 503) and attempt < max_attempts - 1:
                time.sleep(delay)
                delay = min(delay * 2.0, 2.0)
                continue
            return CLOBSubmitResult(False, None, None, REASON_CLOB_REJECT,
                                       request_id, False, last_status)
        except urllib.error.URLError as e:
            return CLOBSubmitResult(False, None, None, REASON_CLOB_REJECT,
                                       request_id, False, 0)
        except TimeoutError:
            return CLOBSubmitResult(False, None, None, REASON_CLOB_REJECT,
                                       request_id, False, 0)
        except json.JSONDecodeError:
            return CLOBSubmitResult(False, None, None, REASON_CLOB_REJECT,
                                       request_id, False, last_status)

    return CLOBSubmitResult(False, None, None, REASON_CLOB_REJECT,
                               request_id, False, last_status)