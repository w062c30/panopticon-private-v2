"""D180: diagnostics router validation."""
from __future__ import annotations

from fastapi.testclient import TestClient

from panopticon_py.api.app import app


def test_market_breakdown_rejects_invalid_sort() -> None:
    client = TestClient(app)
    r = client.get("/api/diagnostics/market_breakdown?sort=not_a_sort")
    assert r.status_code == 400


def test_market_breakdown_no_store_and_no_cache_hit_by_default(monkeypatch) -> None:
    """D190b: default TTL 0 — each request rebuilds; response must not be HTTP-cached."""
    from panopticon_py.api.routers import diagnostics as diag_mod

    def _fake_build(*, limit: int, sort_key: str):
        return {
            "generated_at": "2026-01-01T00:00:00.000Z",
            "elapsed_ms": 1,
            "sort": sort_key,
            "limit": limit,
            "rows": [],
            "summary": {"total_candidates_scanned": 0},
        }

    monkeypatch.setattr(diag_mod, "build_market_breakdown", _fake_build)

    client = TestClient(app)
    r1 = client.get("/api/diagnostics/market_breakdown?limit=5&sort=abs_z")
    assert r1.status_code == 200
    assert r1.headers.get("cache-control") == "no-store"
    assert r1.json().get("cache_hit") is False

    r2 = client.get("/api/diagnostics/market_breakdown?limit=5&sort=abs_z")
    assert r2.json().get("cache_hit") is False
