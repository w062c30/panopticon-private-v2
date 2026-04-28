from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


def _validate_submit_payload(payload: dict[str, Any]) -> str | None:
    """Minimal structural validation aligned with shared/contracts/l4-order-submit.schema.json."""
    if not isinstance(payload.get("idempotency_key"), str) or len(payload["idempotency_key"]) < 8:
        return "bad_idempotency_key"
    if not isinstance(payload.get("decision_id"), str) or len(payload["decision_id"]) < 8:
        return "bad_decision_id"
    pp = payload.get("protected_payload")
    if not isinstance(pp, dict):
        return "bad_protected_payload"
    for k in ("side", "price", "size", "time_in_force", "expires_in_seconds", "expected_avg_price", "slippage_tolerance"):
        if k not in pp:
            return f"missing_{k}"
    if pp["side"] not in ("BUY", "SELL"):
        return "bad_side"
    allowed_extra = {"idempotency_key", "decision_id", "market_id", "asset_id", "protected_payload"}
    if set(payload.keys()) - allowed_extra:
        return "extra_keys"
    return None


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    clob_order_id: str | None
    tx_hash: str | None
    raw_error: str | None
    request_id: str
    dry_run: bool
    status_code: int


def submit_order_to_ts(payload: dict[str, Any], *, timeout_sec: float | None = None) -> SubmitResult:
    err = _validate_submit_payload(payload)
    if err:
        return SubmitResult(
            accepted=False,
            clob_order_id=None,
            tx_hash=None,
            raw_error=err,
            request_id="",
            dry_run=False,
            status_code=0,
        )
    default = "http://127.0.0.1:3751/v1/orders:submit"
    base = (os.getenv("L4_SIGN_SUBMIT_URL") or default).strip()
    url = base if base.endswith("/v1/orders:submit") else base.rstrip("/") + "/v1/orders:submit"
    to = float(timeout_sec if timeout_sec is not None else os.getenv("L4_BRIDGE_TIMEOUT_SEC", "12"))
    max_attempts = max(1, int(os.getenv("L4_BRIDGE_MAX_RETRIES", "3")))
    delay = float(os.getenv("L4_BRIDGE_BACKOFF_START_SEC", "0.08"))
    request_id = f"py_{int(time.time() * 1000)}_{random.randint(0, 999_999)}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_status = 0
    last_err: str | None = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Request-ID": request_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                last_status = int(resp.status or resp.getcode() or 200)
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    return SubmitResult(False, None, None, "invalid_json_object", request_id, False, last_status)
                return SubmitResult(
                    accepted=bool(data.get("accepted")),
                    clob_order_id=data.get("clob_order_id"),
                    tx_hash=data.get("tx_hash"),
                    raw_error=data.get("raw_error"),
                    request_id=str(data.get("request_id") or request_id),
                    dry_run=bool(data.get("dry_run")),
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
            return SubmitResult(False, None, None, last_err or "http_error", request_id, False, last_status)
        except urllib.error.URLError as e:
            return SubmitResult(False, None, None, str(e.reason or e), request_id, False, 0)
        except TimeoutError:
            return SubmitResult(False, None, None, "timeout", request_id, False, 0)
        except json.JSONDecodeError as e:
            return SubmitResult(False, None, None, f"json_decode:{e}", request_id, False, last_status)
    return SubmitResult(False, None, None, last_err or "exhausted_retries", request_id, False, last_status)
