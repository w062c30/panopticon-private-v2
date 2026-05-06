import asyncio

import pytest

from panopticon_py.hunting.run_radar import (
    RadarBootError,
    _acquire_radar_boot_lock,
    _radar_boot_lock,
    mark_radar_boot_released,
)


def test_boot_lock_prevents_reentry():
    async def _run():
        await _radar_boot_lock.acquire()
        try:
            with pytest.raises(RadarBootError, match="already_initializing"):
                await _acquire_radar_boot_lock()
        finally:
            mark_radar_boot_released()

    asyncio.run(_run())


def test_boot_lock_releases_via_mark_released():
    async def _run():
        await _acquire_radar_boot_lock()
        assert _radar_boot_lock.locked()
        mark_radar_boot_released()
        assert not _radar_boot_lock.locked()

    asyncio.run(_run())

