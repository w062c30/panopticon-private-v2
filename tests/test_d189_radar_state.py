"""D189: Radar boot state — first_payload_seen reset on DEGRADED / CONNECTING."""

import pytest

from panopticon_py.hunting.run_radar import (
    RadarBootState,
    RadarState,
    _set_radar_state,
)


@pytest.fixture(autouse=True)
def reset_radar_boot_state():
    import panopticon_py.hunting.run_radar as rr

    rr._radar_boot_state = None
    yield
    rr._radar_boot_state = None


def test_first_payload_seen_reset_on_degraded():
    import panopticon_py.hunting.run_radar as rr

    rr._radar_boot_state = RadarBootState(boot_id="test-boot", state=RadarState.STARTING)
    _set_radar_state(RadarState.CONNECTING)
    _set_radar_state(RadarState.SYNCING)
    _set_radar_state(RadarState.READY)
    rr._radar_boot_state.first_payload_seen = True

    _set_radar_state(RadarState.DEGRADED)
    assert rr._radar_boot_state.state == RadarState.DEGRADED
    assert rr._radar_boot_state.first_payload_seen is False


def test_first_payload_seen_reset_on_connecting_from_starting():
    import panopticon_py.hunting.run_radar as rr

    rr._radar_boot_state = RadarBootState(boot_id="test-boot2", state=RadarState.STARTING)
    rr._radar_boot_state.first_payload_seen = True
    _set_radar_state(RadarState.CONNECTING)
    assert rr._radar_boot_state.first_payload_seen is False
