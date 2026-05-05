"""
D170 unit tests — L4 Signal Fusion (Alert + L4Fuser).
Run: pytest tests/test_d170_signal_fusion.py -v
"""

import time

import pytest

from panopticon_py.signal_engine import Alert, L4Fuser, L4_BOOST_CAP, L4_BOOST_FACTOR


def _a(source, market_id, direction, confidence, **kw):
    return Alert(
        source=source,
        market_id=market_id,
        direction=direction,
        confidence=confidence,
        **kw,
    )


def test_single_alert_pass_through():
    f = L4Fuser()
    out = f.submit(_a("PATH_A", "M1", "YES", 0.7))
    assert out is not None
    assert out.confidence == pytest.approx(0.7)
    assert out.source == "PATH_A"


def test_same_source_dedup():
    f = L4Fuser()
    f.submit(_a("PATH_A", "M1", "YES", 0.7))
    out = f.submit(_a("PATH_A", "M1", "YES", 0.8))
    assert out is None


def test_cross_source_boost_same_direction():
    f = L4Fuser()
    f.submit(_a("PATH_A", "M1", "YES", 0.6))
    out = f.submit(_a("PATH_B", "M1", "YES", 0.6))
    assert out is not None
    assert out.confidence == pytest.approx(min(0.6 * L4_BOOST_FACTOR, L4_BOOST_CAP))


def test_cross_source_skip_opposite():
    f = L4Fuser()
    f.submit(_a("PATH_A", "M1", "YES", 0.6))
    out = f.submit(_a("PATH_B", "M1", "NO", 0.7))
    assert out is None


def test_window_expiry(monkeypatch):
    import panopticon_py.signal_engine as se

    monkeypatch.setattr(se, "L4_WINDOW_SEC", 0.05)
    f = L4Fuser()
    f.submit(_a("PATH_A", "M1", "YES", 0.6))
    time.sleep(0.1)
    out = f.submit(_a("PATH_A", "M1", "YES", 0.6))
    assert out is not None


def test_boost_cap():
    f = L4Fuser()
    f.submit(_a("PATH_A", "M1", "YES", 0.9))
    out = f.submit(_a("PATH_B", "M1", "YES", 0.9))
    assert out is not None
    assert out.confidence <= L4_BOOST_CAP


def test_voided_alert_purged(monkeypatch):
    import panopticon_py.signal_engine as se

    monkeypatch.setattr(se, "L4_WINDOW_SEC", 60.0)
    f = L4Fuser()
    first = f.submit(_a("PATH_A", "M1", "YES", 0.6))
    assert first is not None
    f.submit(_a("PATH_B", "M1", "NO", 0.7))
    f._purge_expired("M1", time.monotonic())
    remaining = f._window.get("M1", [])
    assert all(not a.voided for a in remaining)


def test_alert_invalid_source():
    with pytest.raises(ValueError, match="PATH_A or PATH_B"):
        Alert(source="PATH_C", market_id="M1", direction="YES", confidence=0.5)


def test_alert_invalid_direction():
    with pytest.raises(ValueError, match="YES or NO"):
        Alert(source="PATH_A", market_id="M1", direction="MAYBE", confidence=0.5)


def test_alert_invalid_confidence():
    with pytest.raises(ValueError, match="0..1"):
        Alert(source="PATH_A", market_id="M1", direction="YES", confidence=1.5)
