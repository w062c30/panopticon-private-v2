"""D180: diagnostics router validation."""
from __future__ import annotations

from fastapi.testclient import TestClient

from panopticon_py.api.app import app


def test_market_breakdown_rejects_invalid_sort() -> None:
    client = TestClient(app)
    r = client.get("/api/diagnostics/market_breakdown?sort=not_a_sort")
    assert r.status_code == 400
