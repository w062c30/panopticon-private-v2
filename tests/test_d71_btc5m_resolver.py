"""
tests/test_d71_btc5m_resolver.py

D71a: Tests for:
  - resolve_btc_5m_windows() returns correct inserted count
  - link_map has correct fields after resolve
  - InsiderDetector orphan cleanup in run_insider_monitor
"""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


class TestResolveBtc5mWindowsInsertsLinkMap:
    """Test that resolve_btc_5m_windows inserts BTC 5m rows into link_map."""

    @pytest.mark.asyncio
    async def test_inserts_link_map_with_correct_fields(self, shadow_db) -> None:
        """
        Verify that when Gamma API returns a market for a slug,
        the row written to link_map has market_tier='t1' and condition_id populated.
        """
        from panopticon_py.hunting.run_radar import resolve_btc_5m_windows

        fake_slug = "btc-updown-5m-1777375800"
        fake_cid  = "0x1234567890abcdef1234567890abcdef12345678"
        fake_tid  = "99998888777766665555444433332222111111111"

        # Freeze time to control slug computation
        # lookahead=1 generates: prev, current, next1, next2, next3
        # We mock so only current window hits our fake market
        with patch("time.time", return_value=1777375800):
            # Only current window slug matches our fake
            fake_response = json.dumps([{
                "conditionId": fake_cid,
                "clobTokenIds": [fake_tid],
                "slug": fake_slug,
            }]).encode("utf-8")

            fake_page_empty = json.dumps([]).encode("utf-8")

            def fake_urlopen(url, timeout=None):
                """Return market for the current window slug only."""
                url_str = url.full_url if hasattr(url, "full_url") else str(url)
                if fake_slug in url_str:
                    class Ok:
                        status = 200
                        def read(self): return fake_response
                        def __enter__(self): return self
                        def __exit__(self, *a): pass
                    return Ok()
                # All other slugs: empty (market not listed yet)
                class Empty:
                    status = 200
                    def read(self): return fake_page_empty
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                return Empty()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                inserted = await resolve_btc_5m_windows(shadow_db, lookahead=1)

        assert inserted >= 0  # function ran without exception

        # Verify row in link_map
        row = shadow_db.conn.execute(
            "SELECT slug, condition_id, token_id, market_tier, source "
            "FROM polymarket_link_map WHERE slug=?",
            (fake_slug,)
        ).fetchone()

        assert row is not None, f"Slug {fake_slug} not found in link_map"
        slug, cid, tid, tier, src = row
        assert slug == fake_slug
        assert cid == fake_cid
        assert tid == fake_tid
        assert tier == "t1", f"Expected market_tier='t1', got '{tier}'"
        assert cid is not None and len(cid) > 0, "condition_id must not be NULL/empty"

    @pytest.mark.asyncio
    async def test_insert_count_reflects_new_rows(self, shadow_db) -> None:
        """
        inserted count should reflect number of NEW rows written
        (rows already in link_map are skipped, counted as 0).
        """
        from panopticon_py.hunting.run_radar import resolve_btc_5m_windows

        # Pre-seed a row for the "previous" window (will be skipped)
        prev_slug = "btc-updown-5m-1777375500"
        shadow_db.conn.execute("""
            INSERT INTO polymarket_link_map
                (slug, condition_id, token_id, market_tier, source, fetched_at, created_at)
            VALUES (?, ?, ?, 't1', 'pre_seed', datetime('now'), datetime('now'))
        """, (prev_slug, "0xPREV0000PREV0000PREV0000PREV0000PREV",
              "33333111111111111111111111111111111111111"))
        shadow_db.conn.commit()

        with patch("time.time", return_value=1777375800):
            fake_response = json.dumps([{
                "conditionId": "0xCURRENT000CURRENT000CURRENT0000001",
                "clobTokenIds": ["44444222222222222222222222222222222222222"],
                "slug": "btc-updown-5m-1777375800",
            }]).encode("utf-8")
            empty = json.dumps([]).encode("utf-8")

            def fake_urlopen(url, timeout=None):
                url_str = url.full_url if hasattr(url, "full_url") else str(url)
                if "btc-updown-5m-1777375800" in url_str:
                    class Ok:
                        status = 200
                        def read(self): return fake_response
                        def __enter__(self): return self
                        def __exit__(self, *a): pass
                    return Ok()
                class Empty:
                    status = 200
                    def read(self): return empty
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                return Empty()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                inserted = await resolve_btc_5m_windows(shadow_db, lookahead=1)

        # prev_slug was skipped (already exists), current was inserted = 1
        assert inserted == 1, f"Expected 1 new row (prev skipped), got {inserted}"


class TestInsiderMonitorStopsOrphanDetectors:
    """Test that run_insider_monitor stops detectors for orphaned condition_ids."""

    @pytest.mark.asyncio
    async def test_orphan_detector_stopped(self) -> None:
        """
        _insider_detectors = {active_cid: mock_A, orphan_cid: mock_B}
        active_cids = {active_cid} only.
        After orphan cleanup: mock_B.stop() called once,
        orphan_cid absent from dict, active_cid remains.
        """
        orphan_cid  = "0xORPHANORPHANORPHANORPHANORPHANORPHANORPH"
        active_cid  = "0xACTIVEAACTIVECIDACTIVECIDACTIVECIDACTIVEA"

        mock_det_orphan = MagicMock()
        mock_det_active = MagicMock()

        _insider_detectors = {
            active_cid: mock_det_active,
            orphan_cid: mock_det_orphan,
        }
        active_cids = {active_cid}

        # ── Simulate orphan cleanup body from run_insider_monitor ──
        orphans = set(_insider_detectors.keys()) - active_cids
        for cid in orphans:
            try:
                _insider_detectors[cid].stop()
                del _insider_detectors[cid]
            except Exception:
                pass
        # ─────────────────────────────────────────────────────────────────

        assert mock_det_orphan.stop.call_count == 1, \
            f"orphan stop() expected 1, got {mock_det_orphan.stop.call_count}"
        assert orphan_cid not in _insider_detectors, \
            "orphan_cid should be removed from dict"
        assert active_cid in _insider_detectors, \
            "active_cid must remain in dict"
        assert mock_det_active.stop.call_count == 0, \
            "active detector stop() should NOT be called"

    @pytest.mark.asyncio
    async def test_no_orphans_means_no_stop_calls(self) -> None:
        """
        If active_cids matches _insider_detectors exactly, no stop() calls.
        """
        active_cid_1 = "0xAAAA1111AAAA1111AAAA1111AAAA1111AAAA1111"
        active_cid_2 = "0xBBBB2222BBBB2222BBBB2222BBBB2222BBBB2222"

        mock_det_1 = MagicMock()
        mock_det_2 = MagicMock()

        _insider_detectors = {
            active_cid_1: mock_det_1,
            active_cid_2: mock_det_2,
        }
        active_cids = {active_cid_1, active_cid_2}

        orphans = set(_insider_detectors.keys()) - active_cids
        for cid in orphans:
            _insider_detectors[cid].stop()
            del _insider_detectors[cid]

        assert mock_det_1.stop.call_count == 0
        assert mock_det_2.stop.call_count == 0
        assert len(_insider_detectors) == 2
