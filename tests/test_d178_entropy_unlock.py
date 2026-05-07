"""D178: EntropyWindow gap-safe unlock + trigger-lock unlock path tests.

Tests the D178 gap-based safe-unlock in EntropyWindow.push().
Confirms that:
  - A token with _trigger_locked=True, gap <= max_internal_gap_sec,
    >=5 events, and >=2 H samples will auto-unlock.
  - Tokens with insufficient events or H samples remain locked.
  - Env var HUNT_EW_UNLOCK_MAX_GAP_SEC is read and validated.
"""
from __future__ import annotations

import pytest, time, math
from collections import deque
from unittest.mock import patch, MagicMock
from panopticon_py.hunting.entropy_window import EntropyWindow


def _make_ew(tier: str = "t3") -> EntropyWindow:
    return EntropyWindow(tier=tier)


def _push_trades(ew: EntropyWindow, n: int, gap: float = 1.0) -> None:
    """Push n synthetic trades, each `gap` seconds apart."""
    now = time.monotonic()
    for i in range(n):
        ew.push(now + i * gap, buy_vol=1.0, sell_vol=0.0)
        # record_H_sample is called in hot path after push
        # (we mirror that here for test completeness)
        # but for gap-based unlock we only need events, not H samples


def test_gap_safe_unlock_fires_when_all_conditions_met():
    """A locked EW with >=5 events, gap <= max_internal_gap_sec, and >=2 H samples should unlock."""
    ew = _make_ew(tier="t3")
    # Prime: 2 H samples so unlock check sees len(_h_history) >= 2
    ew._h_history.extend([1.0, 1.0])
    # Mark locked
    ew._trigger_locked = True
    ew._last_recv_mono = time.monotonic()
    ew._events.extend([(time.monotonic(), 1.0, 0.0)] * 4)
    # Now push a 5th event with no gap (gap=1.0 <= max_internal_gap_sec=inf)
    now = time.monotonic()
    flushed = ew.push(now, buy_vol=1.0, sell_vol=0.0)
    assert flushed is None, "push should not flush on normal gap"
    assert ew._trigger_locked is False, (
        f"Expected unlock via gap_safe; locked={ew._trigger_locked}, "
        f"events={len(ew._events)}, h_hist={len(ew._h_history)}"
    )


def test_gap_safe_unlock_requires_min_events():
    """Unlocking requires at least 5 events (min of 5 and unlock_event_count)."""
    ew = _make_ew(tier="t3")
    ew._h_history.extend([1.0, 1.0])
    ew._trigger_locked = True
    ew._last_recv_mono = time.monotonic()
    # Only 4 events — should NOT unlock
    ew._events.extend([(time.monotonic(), 1.0, 0.0)] * 3)
    now = time.monotonic()
    ew.push(now, buy_vol=1.0, sell_vol=0.0)
    # 3 pre-existing + 1 push = 4 total (below minimum 5)
    assert ew._trigger_locked is True, "Should remain locked with only 4 events"


def test_gap_safe_unlock_requires_h_samples():
    """Unlocking requires at least 2 H samples."""
    ew = _make_ew(tier="t3")
    ew._h_history.extend([1.0])  # Only 1 sample — not enough
    ew._trigger_locked = True
    ew._last_recv_mono = time.monotonic()
    ew._events.extend([(time.monotonic(), 1.0, 0.0)] * 5)
    now = time.monotonic()
    ew.push(now, buy_vol=1.0, sell_vol=0.0)
    assert ew._trigger_locked is True, "Should remain locked with only 1 H sample"


def test_gap_safe_unlock_not_triggered_by_flushed_locked_ew():
    """An EW locked via flush (gap_flush_sec exceeded) should NOT unlock via D178.

    When gap > gap_flush_sec, _flush() sets _trigger_locked=True and clears _events.
    After a flush, _events will be nearly empty, so the D178 min-events check fails.
    """
    # Patch gap_flush_sec and max_internal_gap_sec via env BEFORE creating EW
    with patch.dict("os.environ", {
        "HUNT_ENTROPY_GAP_FLUSH_SEC": "10.0",
        "HUNT_ENTROPY_MAX_INTERNAL_GAP_SEC": "5.0",
    }):
        ew = EntropyWindow(tier="t3")
        ew._h_history.extend([1.0, 1.0])
        # Normal state — not locked
        ew._last_recv_mono = time.monotonic()
        ew._events.extend([(time.monotonic(), 1.0, 0.0)] * 4)
        ew._trigger_locked = False
        # Push with 20s gap (exceeds gap_flush_sec=10) — triggers flush and lock
        now = time.monotonic() + 20.0
        flushed = ew.push(now, buy_vol=1.0, sell_vol=0.0)
        assert flushed == "recv_gap"
        assert ew._trigger_locked is True, "Gap > gap_flush_sec should trigger lock"
        # flush() clears old events but keeps the NEW one (gap-trigger push)
        # So D178 unlock won't fire because the next push will be within gap_flush_sec
        # We need to push with ANOTHER gap to trigger flush again
        now2 = time.monotonic() + 30.0
        ew.push(now2, buy_vol=1.0, sell_vol=0.0)
        assert ew._trigger_locked is True, "D178 should not unlock: gap flush keeps it locked"


def test_push_clears_trigger_locked_via_event_count():
    """Original D165 unlock via event_count still works."""
    ew = _make_ew(tier="t3")
    ew._h_history.extend([1.0] * 35)
    ew._trigger_locked = True
    ew._last_recv_mono = time.monotonic() - 1000.0  # old recv
    now = time.monotonic()
    for i in range(30):
        ew.push(now + i, buy_vol=1.0, sell_vol=0.0)
    # After 30 events, should unlock via event_count (D165 path)
    assert ew._trigger_locked is False


def test_push_preserves_existing_unlock_via_healthy_span():
    """Original D165 unlock via healthy_span still works."""
    ew = _make_ew(tier="t3")
    ew._h_history.extend([1.0] * 10)
    ew._trigger_locked = True
    # healthy_span is accumulated only when gap <= max_internal_gap_sec
    ew._healthy_span = 5.1  # above default 5.0 threshold
    ew._last_recv_mono = time.monotonic() - 1000.0
    now = time.monotonic()
    ew.push(now, buy_vol=1.0, sell_vol=0.0)
    assert ew._trigger_locked is False


def test_uninitialized_ew_does_not_unlock_on_first_push():
    """Fresh EW starts unlocked; first push should not set _trigger_locked."""
    ew = _make_ew(tier="t3")
    assert ew._trigger_locked is False
    now = time.monotonic()
    ew.push(now, buy_vol=1.0, sell_vol=0.0)
    assert ew._trigger_locked is False


def test_flush_still_sets_trigger_locked():
    """Explicit flush (mark_reconnect) should set _trigger_locked=True."""
    ew = _make_ew(tier="t3")
    ew._events.append((time.monotonic(), 1.0, 0.0))
    ew._trigger_locked = False
    ew.mark_reconnect("test_reconnect")
    assert ew._trigger_locked is True


def test_gap_safe_unlock_logs_at_info(caplog):
    """Gap-safe unlock should log at INFO level."""
    import logging
    ew = _make_ew(tier="t3")
    ew._h_history.extend([1.0, 1.0])
    ew._trigger_locked = True
    ew._last_recv_mono = time.monotonic()
    ew._events.extend([(time.monotonic(), 1.0, 0.0)] * 4)
    now = time.monotonic()
    # D178 unlock logs at INFO level
    with caplog.at_level(logging.INFO, logger="panopticon_py.hunting.entropy_window"):
        ew.push(now, buy_vol=1.0, sell_vol=0.0)
    assert any("D178" in r.message and "gap_safe" in r.message for r in caplog.records), (
        f"Expected INFO log with 'D178' and 'gap_safe', got: {[r.message for r in caplog.records]}"
    )


def test_uninitialized_ew_has_no_gap_to_unlock():
    """EW with no prior recv_mono should not trigger gap-safe unlock."""
    ew = _make_ew(tier="t3")
    ew._h_history.extend([1.0, 1.0])
    ew._trigger_locked = True
    # _last_recv_mono is None
    now = time.monotonic()
    ew.push(now, buy_vol=1.0, sell_vol=0.0)
    assert ew._trigger_locked is True, "Cannot unlock via gap when no prior recv_mono"


def test_gap_safe_unlock_with_t2_tier():
    """T2 EW (60s window) should also benefit from gap-safe unlock."""
    ew = _make_ew(tier="t2")
    ew._h_history.extend([1.0, 1.0])
    ew._trigger_locked = True
    ew._last_recv_mono = time.monotonic()
    ew._events.extend([(time.monotonic(), 1.0, 0.0)] * 4)
    now = time.monotonic()
    flushed = ew.push(now, buy_vol=1.0, sell_vol=0.0)
    assert flushed is None
    assert ew._trigger_locked is False


def test_hunt_unlock_max_gap_sec_env_validated():
    """HUNT_EW_UNLOCK_MAX_GAP_SEC < 30.0 should raise ValueError."""
    with patch.dict("os.environ", {"HUNT_EW_UNLOCK_MAX_GAP_SEC": "20.0"}):
        with pytest.raises(ValueError, match="min=30"):
            EntropyWindow(tier="t3")
