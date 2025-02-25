# License: MIT
# Copyright © 2023 Frequenz Energy-as-a-Service GmbH

"""Tests for the `EVChargerPool`."""

from __future__ import annotations

import asyncio

from pytest_mock import MockerFixture

from frequenz.sdk import microgrid
from frequenz.sdk.microgrid.component import (
    EVChargerCableState,
    EVChargerComponentState,
)
from frequenz.sdk.timeseries._quantities import Current, Power
from frequenz.sdk.timeseries.ev_charger_pool._state_tracker import (
    EVChargerState,
    StateTracker,
)
from tests.timeseries.mock_microgrid import MockMicrogrid


class TestEVChargerPool:
    """Tests for the `EVChargerPool`."""

    async def test_state_updates(self, mocker: MockerFixture) -> None:
        """Test ev charger state updates are visible."""

        mockgrid = MockMicrogrid(
            grid_side_meter=False, api_client_streaming=True, sample_rate_s=0.01
        )
        mockgrid.add_ev_chargers(5)
        await mockgrid.start(mocker)

        state_tracker = StateTracker(set(mockgrid.evc_ids))
        await asyncio.sleep(0.05)

        async def check_states(
            expected: dict[int, EVChargerState],
        ) -> None:
            await mockgrid.send_ev_charger_data(
                [0.0] * 5  # for testing status updates, the values don't matter.
            )
            await asyncio.sleep(0.05)
            for comp_id, exp_state in expected.items():
                assert state_tracker.get(comp_id) == exp_state

        ## check that all chargers are in idle state.
        expected_states = {evc_id: EVChargerState.IDLE for evc_id in mockgrid.evc_ids}
        assert len(expected_states) == 5
        await check_states(expected_states)

        ## check that EV_PLUGGED state gets set
        evc_2_id = mockgrid.evc_ids[2]
        mockgrid.evc_cable_states[evc_2_id] = EVChargerCableState.EV_PLUGGED
        mockgrid.evc_component_states[evc_2_id] = EVChargerComponentState.READY
        expected_states[evc_2_id] = EVChargerState.EV_PLUGGED
        await check_states(expected_states)

        ## check that EV_LOCKED state gets set
        evc_3_id = mockgrid.evc_ids[3]
        mockgrid.evc_cable_states[evc_3_id] = EVChargerCableState.EV_LOCKED
        mockgrid.evc_component_states[evc_3_id] = EVChargerComponentState.READY
        expected_states[evc_3_id] = EVChargerState.EV_LOCKED
        await check_states(expected_states)

        ## check that ERROR state gets set
        evc_1_id = mockgrid.evc_ids[1]
        mockgrid.evc_cable_states[evc_1_id] = EVChargerCableState.EV_LOCKED
        mockgrid.evc_component_states[evc_1_id] = EVChargerComponentState.ERROR
        expected_states[evc_1_id] = EVChargerState.ERROR
        await check_states(expected_states)

        await state_tracker.stop()
        await mockgrid.cleanup()

    async def test_ev_power(  # pylint: disable=too-many-locals
        self,
        mocker: MockerFixture,
    ) -> None:
        """Test the ev power formula."""
        mockgrid = MockMicrogrid(grid_side_meter=False)
        mockgrid.add_ev_chargers(3)
        await mockgrid.start(mocker)

        ev_pool = microgrid.ev_charger_pool()
        power_receiver = ev_pool.power.new_receiver()
        production_receiver = ev_pool.production_power.new_receiver()
        consumption_receiver = ev_pool.consumption_power.new_receiver()

        await mockgrid.mock_resampler.send_evc_power([2.0, 4.0, 10.0])
        assert (await power_receiver.receive()).value == Power.from_watts(16.0)
        assert (await production_receiver.receive()).value == Power.from_watts(0.0)
        assert (await consumption_receiver.receive()).value == Power.from_watts(16.0)

        await mockgrid.mock_resampler.send_evc_power([2.0, 4.0, -10.0])
        assert (await power_receiver.receive()).value == Power.from_watts(-4.0)
        assert (await production_receiver.receive()).value == Power.from_watts(4.0)
        assert (await consumption_receiver.receive()).value == Power.from_watts(0.0)

        await mockgrid.cleanup()

    async def test_ev_component_data(self, mocker: MockerFixture) -> None:
        """Test the component_data method of EVChargerPool."""
        mockgrid = MockMicrogrid(
            grid_side_meter=False,
            api_client_streaming=True,
            sample_rate_s=0.05,
        )
        mockgrid.add_ev_chargers(1)

        await mockgrid.start(mocker)

        evc_id = mockgrid.evc_ids[0]
        ev_pool = microgrid.ev_charger_pool()

        recv = ev_pool.component_data(evc_id)

        await mockgrid.send_ev_charger_data(
            [0.0]  # only the status gets used from this.
        )
        await asyncio.sleep(0.05)
        await mockgrid.mock_resampler.send_evc_current([[2, 3, 5]])
        status = await recv.receive()
        assert (
            status.current.value_p1,
            status.current.value_p2,
            status.current.value_p3,
        ) == (
            Current.from_amperes(2),
            Current.from_amperes(3),
            Current.from_amperes(5),
        )
        assert status.state == EVChargerState.MISSING

        await mockgrid.send_ev_charger_data(
            [0.0]  # only the status gets used from this.
        )
        await asyncio.sleep(0.05)
        await mockgrid.mock_resampler.send_evc_current([[2, 3, None]])
        status = await recv.receive()
        assert (
            status.current.value_p1,
            status.current.value_p2,
            status.current.value_p3,
        ) == (
            Current.from_amperes(2),
            Current.from_amperes(3),
            None,
        )
        assert status.state == EVChargerState.IDLE

        await mockgrid.send_ev_charger_data(
            [0.0]  # only the status gets used from this.
        )
        await asyncio.sleep(0.05)
        await mockgrid.mock_resampler.send_evc_current([[None, None, None]])
        status = await recv.receive()
        assert (
            status.current.value_p1,
            status.current.value_p2,
            status.current.value_p3,
        ) == (
            None,
            None,
            None,
        )
        assert status.state == EVChargerState.MISSING

        mockgrid.evc_cable_states[evc_id] = EVChargerCableState.EV_PLUGGED
        await mockgrid.send_ev_charger_data(
            [0.0]  # only the status gets used from this.
        )
        await asyncio.sleep(0.05)
        await mockgrid.mock_resampler.send_evc_current([[None, None, None]])
        status = await recv.receive()
        assert (
            status.current.value_p1,
            status.current.value_p2,
            status.current.value_p3,
        ) == (
            None,
            None,
            None,
        )
        assert status.state == EVChargerState.MISSING

        await mockgrid.send_ev_charger_data(
            [0.0]  # only the status gets used from this.
        )
        await asyncio.sleep(0.05)
        await mockgrid.mock_resampler.send_evc_current([[4, None, None]])
        status = await recv.receive()
        assert (
            status.current.value_p1,
            status.current.value_p2,
            status.current.value_p3,
        ) == (
            Current.from_amperes(4),
            None,
            None,
        )
        assert status.state == EVChargerState.EV_PLUGGED

        await mockgrid.cleanup()
        await ev_pool.stop()
