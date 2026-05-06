import asyncio

import pytest

from panopticon_py.hunting.run_radar import RadarBootError, _radar_boot_lock, mark_radar_boot_released


@pytest.mark.asyncio
async def test_option_b_lock_auto_releases_on_exception():
    async def _boot_stub():
        if _radar_boot_lock.locked():
            raise RadarBootError("already_initializing")
        async with _radar_boot_lock:
            raise RuntimeError("simulated fatal")

    with pytest.raises(RuntimeError):
        await _boot_stub()

    assert not _radar_boot_lock.locked()


@pytest.mark.asyncio
async def test_option_b_reentry_still_blocked():
    entered = asyncio.Event()

    async def _slow_boot():
        async with _radar_boot_lock:
            entered.set()
            await asyncio.sleep(0.05)

    async def _fast_second():
        await entered.wait()
        if _radar_boot_lock.locked():
            raise RadarBootError("already_initializing")

    task = asyncio.create_task(_slow_boot())
    with pytest.raises(RadarBootError, match="already_initializing"):
        await _fast_second()
    await task


def test_mark_radar_boot_released_is_noop():
    mark_radar_boot_released()
