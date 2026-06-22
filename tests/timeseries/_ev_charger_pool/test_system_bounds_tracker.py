# License: MIT
# Copyright © 2026 Frequenz Energy-as-a-Service GmbH

"""Tests for the EV charger system bounds tracker."""

# pylint: disable=protected-access

from datetime import datetime, timezone

from frequenz.channels import Broadcast
from frequenz.quantities import Power

from frequenz.sdk.microgrid._old_component_data import EVChargerData
from frequenz.sdk.microgrid._power_distributing import ComponentPoolStatus
from frequenz.sdk.timeseries._base_types import SystemBounds
from frequenz.sdk.timeseries.ev_charger_pool._system_bounds_tracker import (
    EVCSystemBoundsTracker,
)


async def test_system_bounds_tracker_aggregates_rated_bounds() -> None:
    """The tracker should emit rated bounds without changing inclusion bounds."""
    bounds_channel: Broadcast[SystemBounds] = Broadcast(name="evc-bounds")
    status_channel: Broadcast[ComponentPoolStatus] = Broadcast(name="evc-status")
    bounds_rx = bounds_channel.new_receiver()
    tracker = EVCSystemBoundsTracker(
        {1, 2}, status_channel.new_receiver(), bounds_channel.new_sender()
    )
    now = datetime.now(tz=timezone.utc)
    tracker._latest_component_data = {
        1: EVChargerData(
            component_id=1,
            timestamp=now,
            active_power_inclusion_lower_bound=0.0,
            active_power_inclusion_upper_bound=5000.0,
            active_power_exclusion_lower_bound=0.0,
            active_power_exclusion_upper_bound=0.0,
            active_power_rated_lower_bound=0.0,
            active_power_rated_upper_bound=11000.0,
        ),
        2: EVChargerData(
            component_id=2,
            timestamp=now,
            active_power_inclusion_lower_bound=0.0,
            active_power_inclusion_upper_bound=6000.0,
            active_power_exclusion_lower_bound=0.0,
            active_power_exclusion_upper_bound=0.0,
            active_power_rated_lower_bound=0.0,
            active_power_rated_upper_bound=22000.0,
        ),
    }
    tracker._component_pool_status = ComponentPoolStatus(
        working={1, 2}, uncertain=set()
    )

    await tracker._send_bounds()
    bounds = await bounds_rx.receive()

    assert bounds.inclusion_bounds is not None
    assert bounds.inclusion_bounds.upper == Power.from_watts(11000.0)
    assert bounds.rated_bounds is not None
    assert bounds.rated_bounds.lower == Power.zero()
    assert bounds.rated_bounds.upper == Power.from_watts(33000.0)


async def test_system_bounds_tracker_missing_rated_bounds_yield_none() -> None:
    """Missing rated bounds should make aggregate rated bounds unknown."""
    bounds_channel: Broadcast[SystemBounds] = Broadcast(name="evc-bounds-missing")
    status_channel: Broadcast[ComponentPoolStatus] = Broadcast(
        name="evc-status-missing"
    )
    bounds_rx = bounds_channel.new_receiver()
    tracker = EVCSystemBoundsTracker(
        {1}, status_channel.new_receiver(), bounds_channel.new_sender()
    )
    tracker._latest_component_data = {
        1: EVChargerData(
            component_id=1,
            timestamp=datetime.now(tz=timezone.utc),
            active_power_inclusion_lower_bound=0.0,
            active_power_inclusion_upper_bound=5000.0,
            active_power_exclusion_lower_bound=0.0,
            active_power_exclusion_upper_bound=0.0,
            active_power_rated_lower_bound=None,
            active_power_rated_upper_bound=None,
        )
    }

    await tracker._send_bounds()
    bounds = await bounds_rx.receive()

    assert bounds.rated_bounds is None
