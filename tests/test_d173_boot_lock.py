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
            with pytest.raises(RadarBootError, match="deprecated_boot_lock_path"):
                await _acquire_radar_boot_lock()
        finally:
            _radar_boot_lock.release()

    asyncio.run(_run())


def test_boot_lock_release_helper_is_noop():
    async def _run():
        await _radar_boot_lock.acquire()
        assert _radar_boot_lock.locked()
        mark_radar_boot_released()
        assert _radar_boot_lock.locked()
        _radar_boot_lock.release()

    asyncio.run(_run())

