# License: MIT
# Copyright © 2026 Frequenz Energy-as-a-Service GmbH

"""Tests for EV charger bounds validity and renewal handling."""

# pylint: disable=protected-access

from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Any

from frequenz.client.common.microgrid.components import ComponentId
from frequenz.quantities import Power

from frequenz.sdk.microgrid._power_distributing import Request

_EVM_MODULE = (
    "frequenz.sdk.microgrid._power_distributing._component_managers"
    "._ev_charger_manager._ev_charger_manager"
)
_STATES_MODULE = (
    "frequenz.sdk.microgrid._power_distributing._component_managers"
    "._ev_charger_manager._states"
)
evm: Any = import_module(_EVM_MODULE)
states: Any = import_module(_STATES_MODULE)


class _FakeApi:
    """Fake microgrid API client recording bounds calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[ComponentId, Any, Any, timedelta]] = []

    async def add_component_bounds(
        self,
        component_id: ComponentId,
        metric: Any,
        bounds: Any,
        *,
        validity: timedelta,
    ) -> datetime:
        """Record a bounds request."""
        self.calls.append((component_id, metric, bounds, validity))
        return datetime.now(tz=timezone.utc)


def _manager() -> Any:
    manager = evm.EVChargerManager.__new__(evm.EVChargerManager)
    # pylint: disable=protected-access
    manager._last_sent_allocations = {}
    manager._latest_request = Request(Power.zero(), set())
    manager._target_power = Power.zero()
    manager._evc_states = states.EvcStates()
    return manager


def test_short_request_validity_maps_to_one_second() -> None:
    """A one-second request should map to a one-second validity."""
    request = Request(
        Power.from_watts(1000.0),
        {ComponentId(1)},
        bounds_validity=timedelta(seconds=1.0),
    )

    assert evm._validity_for_request(request) == timedelta(seconds=1.0)


def test_fractional_request_validity_rounds_up_and_clamps() -> None:
    """Fractional and out-of-range validities are rounded up and clamped."""
    assert evm._validity_for_request(
        Request(Power.zero(), {ComponentId(1)}, bounds_validity=timedelta(seconds=2.1))
    ) == timedelta(seconds=3.0)
    assert evm._validity_for_request(
        Request(Power.zero(), {ComponentId(1)}, bounds_validity=timedelta(0))
    ) == timedelta(seconds=1.0)
    assert evm._validity_for_request(
        Request(Power.zero(), {ComponentId(1)}, bounds_validity=timedelta(hours=1))
    ) == timedelta(seconds=900.0)
    assert evm._validity_for_request(
        Request(Power.zero(), {ComponentId(1)})
    ) == timedelta(seconds=60.0)


async def test_send_api_bounds_uses_selected_validity() -> None:
    """EV charger bounds should be sent with the request-aware validity."""
    manager = _manager()
    api = _FakeApi()
    validity = evm._validity_for_request(
        Request(
            Power.from_watts(1000.0),
            {ComponentId(1)},
            bounds_validity=timedelta(seconds=1.0),
        )
    )

    succeeded, failed = await manager._send_api_bounds(
        api,
        {ComponentId(1): Power.from_watts(1000.0)},
        timedelta(seconds=1.0),
        validity,
    )

    assert succeeded == {ComponentId(1)}
    assert not failed
    assert api.calls[-1][3] == timedelta(seconds=1.0)


async def test_zero_request_overwrites_and_renews_previous_nonzero_cap() -> None:
    """After a reset, renewal must keep zero alive instead of the old cap."""
    component_id = ComponentId(1)
    manager = _manager()
    # pylint: disable=protected-access
    manager._latest_request = Request(
        Power.zero(), {component_id}, bounds_validity=timedelta(seconds=1.0)
    )
    manager._last_sent_allocations = {
        component_id: evm._LastSentAllocation(
            power=Power.from_watts(25_000.0),
            validity=timedelta(seconds=60.0),
            sent_at=datetime.now(tz=timezone.utc) - timedelta(seconds=120.0),
        )
    }
    api = _FakeApi()

    result = await manager._set_api_power(
        api, {component_id: Power.zero()}, timedelta(seconds=1.0)
    )

    assert result.succeeded_components == {component_id}
    last_sent = manager._last_sent_allocations[component_id]
    assert last_sent.power == Power.zero()
    assert last_sent.validity == timedelta(seconds=1.0)

    last_sent.sent_at = datetime.now(tz=timezone.utc) - timedelta(seconds=10.0)
    await manager._renew_api_bounds(api, timedelta(seconds=1.0))

    bounds = api.calls[-1][2]
    assert list(bounds)[0].upper == 0.0
    assert api.calls[-1][3] == timedelta(seconds=1.0)
