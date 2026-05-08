"""
D180: On-demand heavy diagnostics (manual button / curl only).
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException

from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

_VALID_SORTS: frozenset[str] = frozenset({"abs_z", "hits", "kyle_n", "recent"})

_TTL_SEC = 30.0
_CC: dict[str, Any] = {"mono": 0.0, "key": "", "payload": None}


@router.get("/market_breakdown")
def api_market_breakdown(
    limit: int = 50,
    sort: str = "abs_z",
) -> dict[str, Any]:
    if sort not in _VALID_SORTS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_VALID_SORTS)}")
    key = f"{limit}:{sort}"
    now = time.monotonic()
    cached = _CC.get("payload")
    if (
        cached is not None
        and _CC.get("key") == key
        and (now - float(_CC.get("mono") or 0)) < _TTL_SEC
    ):
        out = dict(cached)
        out["cache_hit"] = True
        out["cache_ttl_sec"] = round(_TTL_SEC - (now - float(_CC.get("mono") or 0)), 2)
        return out

    payload = build_market_breakdown(limit=limit, sort_key=sort)  # type: ignore[arg-type]
    _CC["payload"] = payload
    _CC["key"] = key
    _CC["mono"] = now
    out = dict(payload)
    out["cache_hit"] = False
    return out
