"""D189: PolygonListener WSS loop must not raise UnboundLocalError on failure path."""

import asyncio

import pytest

pytest.importorskip("websockets")


@pytest.mark.asyncio
async def test_pol_wss_loop_except_increments_global_counter(monkeypatch, tmp_path):
    """RULE-CLOSURE-1: except branch uses += on module global — must not UnboundLocalError."""
    import panopticon_py.hunting.pol_monitor as pol

    class FailingConnect:
        async def __aenter__(self):
            raise OSError("fake ws connect failure")

        async def __aexit__(self, *_exc):
            return False

    def fake_connect(*_a, **_k):
        return FailingConnect()

    monkeypatch.setattr(pol.websockets, "connect", fake_connect)
    pol._pol_ws_consecutive_failures = 0

    q: asyncio.Queue = asyncio.Queue()
    listener = pol.PolygonListener(api_key="fake-key", outbound=q, db_path=str(tmp_path / "nodb.db"))

    async def noop_http_fallback() -> None:
        return None

    monkeypatch.setattr(listener, "_http_fallback", noop_http_fallback)

    err: BaseException | None = None
    try:
        await asyncio.wait_for(listener._wss_loop(), timeout=1.5)
    except asyncio.TimeoutError:
        pass
    except BaseException as e:
        err = e
    assert err is None or "cannot access local variable '_pol_ws_consecutive_failures'" not in str(
        err
    ), f"UnboundLocalError leak: {err}"
    assert pol._pol_ws_consecutive_failures >= 1
