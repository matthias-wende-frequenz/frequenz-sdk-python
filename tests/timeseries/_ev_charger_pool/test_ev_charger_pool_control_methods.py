# License: MIT
# Copyright © 2023 Frequenz Energy-as-a-Service GmbH

"""Test the EV charger pool control methods."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import async_solipsism
import pytest
import time_machine
from frequenz.channels import Receiver
from frequenz.client.microgrid.component import ComponentStateCode
from frequenz.quantities import Power
from pytest_mock import MockerFixture

from frequenz.sdk import microgrid
from frequenz.sdk.microgrid import _power_distributing
from frequenz.sdk.microgrid._data_pipeline import _DataPipeline
from frequenz.sdk.microgrid._power_distributing import ComponentPoolStatus
from frequenz.sdk.microgrid._power_distributing._component_pool_status_tracker import (
    ComponentPoolStatusTracker,
)
from frequenz.sdk.timeseries import ResamplerConfig2
from frequenz.sdk.timeseries.ev_charger_pool import EVChargerPoolReport

from ...microgrid.fixtures import _Mocks
from ...utils.component_data_streamer import MockComponentDataStreamer
from ...utils.component_data_wrapper import EvChargerDataWrapper, MeterDataWrapper
from ..mock_microgrid import MockMicrogrid

# pylint: disable=protected-access


@pytest.fixture
def event_loop_policy() -> async_solipsism.EventLoopPolicy:
    """Event loop policy."""
    return async_solipsism.EventLoopPolicy()


@pytest.fixture
async def mocks(mocker: MockerFixture) -> AsyncIterator[_Mocks]:
    """Create the mocks."""
    mockgrid = MockMicrogrid(grid_meter=True)
    mockgrid.add_ev_chargers(4)
    await mockgrid.start(mocker)

    # pylint: disable=protected-access
    if microgrid._data_pipeline._DATA_PIPELINE is not None:
        microgrid._data_pipeline._DATA_PIPELINE = None
    await microgrid._data_pipeline.initialize(
        ResamplerConfig2(resampling_period=timedelta(seconds=0.1))
    )
    streamer = MockComponentDataStreamer(mockgrid.mock_client)

    dp = cast(_DataPipeline, microgrid._data_pipeline._DATA_PIPELINE)

    _mocks = _Mocks(
        mockgrid,
        streamer,
        dp._ev_power_wrapper.status_channel.new_sender(),
    )
    try:
        yield _mocks
    finally:
        await _mocks.stop()


class TestEVChargerPoolControl:
    """Test the EV charger pool control methods."""

    async def _patch_ev_pool_status(
        self,
        mocks: _Mocks,
        mocker: MockerFixture,
        component_ids: list[int] | None = None,
    ) -> None:
        """Patch the EV charger pool status.

        If `component_ids` is not None, the mock will always return `component_ids`.
        Otherwise, it will return the requested components.
        """
        if component_ids:
            mock = MagicMock(spec=ComponentPoolStatusTracker)
            mock.get_working_components.return_value = component_ids
            mocker.patch(
                "frequenz.sdk.microgrid._power_distributing._component_managers"
                "._ev_charger_manager._ev_charger_manager.ComponentPoolStatusTracker",
                return_value=mock,
            )
        else:
            mock = MagicMock(spec=ComponentPoolStatusTracker)
            mock.get_working_components.side_effect = set
            mocker.patch(
                "frequenz.sdk.microgrid._power_distributing._component_managers"
                "._ev_charger_manager._ev_charger_manager.ComponentPoolStatusTracker",
                return_value=mock,
            )
        await mocks.component_status_sender.send(
            ComponentPoolStatus(working=set(mocks.microgrid.evc_ids), uncertain=set())
        )

    async def _patch_power_distributing_actor(
        self,
        mocker: MockerFixture,
    ) -> None:
        # No patching needed after _voltage_cache was removed from the
        # EVChargerManager.  The method is kept as a hook for future patches
        # and to avoid changing callers.
        pass

    async def _init_ev_chargers(
        self,
        mocks: _Mocks,
        *,
        connected_ids: set[int] | None = None,
        lower_bounds: dict[int, float] | None = None,
        upper_bounds: dict[int, float] | None = None,
    ) -> None:
        """Initialize EV charger and meter streams for a test.

        Args:
            mocks: Test mocks providing the mock microgrid and data streamer.
            connected_ids: EV charger IDs that should report an EV as connected.
                When omitted, all EV chargers are connected.
            lower_bounds: Per-charger lower inclusion bounds in watts.
            upper_bounds: Per-charger upper inclusion bounds in watts.
        """
        now = datetime.now(tz=timezone.utc)
        connected_ids = connected_ids or set(mocks.microgrid.evc_ids)
        lower_bounds = lower_bounds or {}
        upper_bounds = upper_bounds or {}

        for evc_id in mocks.microgrid.evc_ids:
            states = {ComponentStateCode.READY}
            if evc_id in connected_ids:
                states |= {
                    ComponentStateCode.EV_CHARGING_CABLE_PLUGGED_AT_EV,
                    ComponentStateCode.EV_CHARGING_CABLE_PLUGGED_AT_STATION,
                }

            mocks.streamer.start_streaming(
                EvChargerDataWrapper(
                    evc_id,
                    now,
                    states=states,
                    active_power=0.0,
                    active_power_inclusion_lower_bound=lower_bounds.get(evc_id, 0.0),
                    active_power_inclusion_upper_bound=upper_bounds.get(
                        evc_id, 16.0 * 230.0 * 3
                    ),
                    voltage_per_phase=(230.0, 230.0, 230.0),
                ),
                0.05,
            )

        for meter_id in mocks.microgrid.meter_ids:
            mocks.streamer.start_streaming(
                MeterDataWrapper(
                    meter_id,
                    now,
                    voltage_per_phase=(230.0, 230.0, 230.0),
                ),
                0.05,
            )

    async def _recv_reports_until(
        self,
        bounds_rx: Receiver[EVChargerPoolReport],
        check: Callable[[EVChargerPoolReport], bool],
    ) -> EVChargerPoolReport | None:
        """Receive reports until the given condition is met."""
        max_reports = 10
        ctr = 0
        while ctr < max_reports:
            ctr += 1
            async with asyncio.timeout(10.0):
                report = await bounds_rx.receive()
            if check(report):
                return report
        return None

    def _assert_report(  # pylint: disable=too-many-arguments
        self,
        report: EVChargerPoolReport | None,
        *,
        power: float | None,
        lower: float,
        upper: float,
        dist_result: _power_distributing.Result | None = None,
        expected_result_pred: (
            Callable[[_power_distributing.Result], bool] | None
        ) = None,
    ) -> None:
        assert report is not None
        assert report.target_power == (
            Power.from_watts(power) if power is not None else None
        )
        assert report.bounds is not None
        assert report.bounds.lower == Power.from_watts(lower)
        assert report.bounds.upper == Power.from_watts(upper)
        if expected_result_pred is not None:
            assert dist_result is not None
            assert expected_result_pred(dist_result)

    def _assert_set_power_calls(
        self,
        set_power: AsyncMock,
        expected_allocations: dict[int, float],
    ) -> None:
        """Assert EV charger power set requests.

        Args:
            set_power: Mocked microgrid API method.
            expected_allocations: Expected power allocations keyed by component ID.
        """
        actual_allocations = {
            call.args[0]: call.args[1] for call in set_power.call_args_list
        }
        for component_id, expected_power in expected_allocations.items():
            assert actual_allocations[component_id] == expected_power

        extra_allocations = {
            component_id: power
            for component_id, power in actual_allocations.items()
            if component_id not in expected_allocations
        }
        assert all(power == 0.0 for power in extra_allocations.values())

    async def test_setting_power(
        self,
        mocks: _Mocks,
        mocker: MockerFixture,
    ) -> None:
        """Test setting power."""
        traveller = time_machine.travel(datetime(2012, 12, 12, tzinfo=timezone.utc))
        mock_time = traveller.start()

        set_power = cast(
            AsyncMock,
            microgrid.connection_manager.get().api_client.set_component_power_active,
        )
        await self._init_ev_chargers(mocks)
        ev_charger_pool = microgrid.new_ev_charger_pool(priority=5)
        await self._patch_ev_pool_status(mocks, mocker)
        await self._patch_power_distributing_actor(mocker)

        bounds_rx = ev_charger_pool.power_status.new_receiver()
        # Receive reports until all chargers are initialized
        latest_report = await self._recv_reports_until(
            bounds_rx,
            lambda x: x.bounds is not None and x.bounds.upper.as_watts() == 44160.0,
        )

        self._assert_report(latest_report, power=None, lower=0.0, upper=44160.0)

        # Check that chargers are initialized to Power.zero()
        assert set_power.call_count == 4
        assert all(x.args[1] == 0.0 for x in set_power.call_args_list)

        set_power.reset_mock()
        await ev_charger_pool.propose_power(Power.from_watts(40000.0))
        latest_report = await self._recv_reports_until(
            bounds_rx,
            lambda r: r.target_power == Power.from_watts(40000.0),
        )
        self._assert_report(latest_report, power=40000.0, lower=0.0, upper=44160.0)
        await asyncio.sleep(0.02)

        self._assert_set_power_calls(
            set_power,
            {evc_id: 10000.0 for evc_id in mocks.microgrid.evc_ids},
        )

        set_power.reset_mock()
        await ev_charger_pool.propose_power(Power.from_watts(32000.0))
        await bounds_rx.receive()
        await asyncio.sleep(0.02)
        self._assert_set_power_calls(
            set_power,
            {evc_id: 8000.0 for evc_id in mocks.microgrid.evc_ids},
        )

        traveller.stop()

    @pytest.mark.parametrize("target_power", [5000.0, 7920.0, 10000.0])
    async def test_minimum_power_distribution(
        self,
        mocks: _Mocks,
        mocker: MockerFixture,
        target_power: float,
    ) -> None:
        """Test redistribution with non-zero EV charger minimum power bounds.

        When the requested pool power is below the aggregate minimum, the pool
        controller may reduce or reshape the effective request before it reaches the
        EV charger manager. This test verifies that no charger receives a positive
        allocation below its minimum bound.
        """
        set_power = cast(
            AsyncMock,
            microgrid.connection_manager.get().api_client.set_component_power_active,
        )
        evc_a, evc_b = mocks.microgrid.evc_ids[:2]
        connected_ids = {evc_a, evc_b}
        lower_bound = 3960.0
        await self._init_ev_chargers(
            mocks,
            connected_ids=connected_ids,
            lower_bounds={evc_id: lower_bound for evc_id in connected_ids},
        )
        ev_charger_pool = microgrid.new_ev_charger_pool(priority=5)
        await self._patch_ev_pool_status(mocks, mocker)
        await self._patch_power_distributing_actor(mocker)

        bounds_rx = ev_charger_pool.power_status.new_receiver()
        latest_report = await self._recv_reports_until(
            bounds_rx,
            lambda x: x.bounds is not None and x.bounds.upper.as_watts() == 22080.0,
        )
        self._assert_report(
            latest_report,
            power=None,
            lower=2 * lower_bound,
            upper=22080.0,
        )

        set_power.reset_mock()
        await ev_charger_pool.propose_power(Power.from_watts(target_power))
        await asyncio.sleep(0.02)

        actual_allocations = {
            call.args[0]: call.args[1] for call in set_power.call_args_list
        }
        for evc_id in connected_ids:
            assert (
                actual_allocations[evc_id] in (0.0, lower_bound)
                or actual_allocations[evc_id] > lower_bound
            )

    async def test_zero_minimum_power_distribution_is_unchanged(
        self,
        mocks: _Mocks,
        mocker: MockerFixture,
    ) -> None:
        """Test redistribution stays unchanged when lower bounds are zero."""
        set_power = cast(
            AsyncMock,
            microgrid.connection_manager.get().api_client.set_component_power_active,
        )
        evc_a, evc_b = mocks.microgrid.evc_ids[:2]
        connected_ids = {evc_a, evc_b}
        await self._init_ev_chargers(mocks, connected_ids=connected_ids)
        ev_charger_pool = microgrid.new_ev_charger_pool(priority=5)
        await self._patch_ev_pool_status(mocks, mocker)
        await self._patch_power_distributing_actor(mocker)

        bounds_rx = ev_charger_pool.power_status.new_receiver()
        await self._recv_reports_until(
            bounds_rx,
            lambda x: x.bounds is not None and x.bounds.upper.as_watts() == 22080.0,
        )

        set_power.reset_mock()
        await ev_charger_pool.propose_power(Power.from_watts(5000.0))
        await self._recv_reports_until(
            bounds_rx,
            lambda r: r.target_power == Power.from_watts(5000.0),
        )
        await asyncio.sleep(0.02)
        self._assert_set_power_calls(set_power, {evc_a: 2500.0, evc_b: 2500.0})

    async def test_mixed_minimum_power_distribution(
        self,
        mocks: _Mocks,
        mocker: MockerFixture,
    ) -> None:
        """Test redistribution excludes chargers whose minimum cannot be met.

        The pool controller may reduce the effective request to the smallest viable
        pool allocation, but the manager must still avoid assigning positive power
        below a charger's minimum bound.
        """
        set_power = cast(
            AsyncMock,
            microgrid.connection_manager.get().api_client.set_component_power_active,
        )
        evc_a, evc_b = mocks.microgrid.evc_ids[:2]
        connected_ids = {evc_a, evc_b}
        await self._init_ev_chargers(
            mocks,
            connected_ids=connected_ids,
            lower_bounds={evc_a: 2000.0, evc_b: 5000.0},
            upper_bounds={evc_a: 6000.0, evc_b: 11040.0},
        )
        ev_charger_pool = microgrid.new_ev_charger_pool(priority=5)
        await self._patch_ev_pool_status(mocks, mocker)
        await self._patch_power_distributing_actor(mocker)

        bounds_rx = ev_charger_pool.power_status.new_receiver()
        latest_report = await self._recv_reports_until(
            bounds_rx,
            lambda x: x.bounds is not None and x.bounds.upper.as_watts() == 17040.0,
        )
        self._assert_report(latest_report, power=None, lower=7000.0, upper=17040.0)

        set_power.reset_mock()
        await ev_charger_pool.propose_power(Power.from_watts(6000.0))
        await asyncio.sleep(0.02)

        actual_allocations = {
            call.args[0]: call.args[1] for call in set_power.call_args_list
        }
        assert (
            actual_allocations[evc_a] in (0.0, 2000.0)
            or actual_allocations[evc_a] > 2000.0
        )
        assert (
            actual_allocations[evc_b] in (0.0, 5000.0)
            or actual_allocations[evc_b] > 5000.0
        )
        assert 0.0 not in (actual_allocations[evc_a], actual_allocations[evc_b]) or (
            actual_allocations[evc_a] != actual_allocations[evc_b]
        )
