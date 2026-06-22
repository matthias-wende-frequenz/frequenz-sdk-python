# License: MIT
# Copyright © 2026 Frequenz Energy-as-a-Service GmbH

"""Tests for EV charger component data parsing."""

from datetime import datetime, timezone

from frequenz.client.microgrid.component import (
    ComponentDataSamples,
    ComponentErrorCode,
    ComponentStateCode,
)
from frequenz.client.microgrid.metrics import Bounds, Metric, MetricSample

from frequenz.sdk.microgrid._old_component_data import EVChargerData


def test_ev_charger_data_parses_rated_bounds_independently() -> None:
    """EV charger data should preserve rated and system bounds separately."""
    now = datetime.now(tz=timezone.utc)
    sample = MetricSample(
        sampled_at=now,
        metric=Metric.AC_ACTIVE_POWER,
        value=1234.0,
        bounds=[Bounds(lower=0.0, upper=5000.0)],
    )
    object.__setattr__(sample, "rated_bounds", [Bounds(lower=0.0, upper=22000.0)])

    data = EVChargerData.from_samples(
        ComponentDataSamples(component_id=1, metric_samples=[sample], states=[])
    )

    assert data.active_power == 1234.0
    assert data.active_power_inclusion_lower_bound == 0.0
    assert data.active_power_inclusion_upper_bound == 5000.0
    assert data.active_power_rated_lower_bound == 0.0
    assert data.active_power_rated_upper_bound == 22000.0


def test_ev_charger_data_treats_sentinel_rated_bounds_as_unknown() -> None:
    """The +/-99.99 GW sentinel should be surfaced as unavailable rated bounds."""
    now = datetime.now(tz=timezone.utc)
    sample = MetricSample(
        sampled_at=now,
        metric=Metric.AC_ACTIVE_POWER,
        value=0.0,
        bounds=[Bounds(lower=0.0, upper=5000.0)],
    )
    object.__setattr__(
        sample,
        "rated_bounds",
        [Bounds(lower=-99_990_000_000.0, upper=99_990_000_000.0)],
    )

    data = EVChargerData.from_samples(
        ComponentDataSamples(component_id=1, metric_samples=[sample], states=[])
    )

    assert data.active_power_rated_lower_bound is None
    assert data.active_power_rated_upper_bound is None


# ---------------------------------------------------------------------------
#  is_ev_connected() tests
# ---------------------------------------------------------------------------


def _make_ev_charger_data(
    states: frozenset[ComponentStateCode | int] = frozenset(),
    errors: frozenset[ComponentErrorCode | int] = frozenset(),
    warnings: frozenset[ComponentErrorCode | int] = frozenset(),
) -> EVChargerData:
    """Create an EVChargerData instance with the given state/error sets."""
    return EVChargerData(
        component_id=1,
        timestamp=datetime.now(tz=timezone.utc),
        states=states,
        errors=errors,
        warnings=warnings,
    )


def test_is_ev_connected_no_states_assumes_connected() -> None:
    """When no cable state is reported, assume charger is available."""
    data = _make_ev_charger_data()
    assert data.is_ev_connected() is True


def test_is_ev_connected_cable_locked_at_ev() -> None:
    """Cable locked at EV side implies connected."""
    data = _make_ev_charger_data(
        states=frozenset({ComponentStateCode.EV_CHARGING_CABLE_LOCKED_AT_EV})
    )
    assert data.is_ev_connected() is True


def test_is_ev_connected_cable_plugged_at_station() -> None:
    """Cable plugged at station side implies connected."""
    data = _make_ev_charger_data(
        states=frozenset({ComponentStateCode.EV_CHARGING_CABLE_PLUGGED_AT_STATION})
    )
    assert data.is_ev_connected() is True


def test_is_ev_connected_cable_unplugged() -> None:
    """Explicit UNPLUGGED state means disconnected."""
    data = _make_ev_charger_data(
        states=frozenset({ComponentStateCode.EV_CHARGING_CABLE_UNPLUGGED})
    )
    assert data.is_ev_connected() is False


def test_is_ev_connected_error_state() -> None:
    """ERROR state means not connected, even with no cable info."""
    data = _make_ev_charger_data(
        states=frozenset({ComponentStateCode.ERROR})
    )
    assert data.is_ev_connected() is False


def test_is_ev_connected_error_overrides_cable() -> None:
    """ERROR state takes precedence over a connected cable state."""
    data = _make_ev_charger_data(
        states=frozenset({
            ComponentStateCode.ERROR,
            ComponentStateCode.EV_CHARGING_CABLE_LOCKED_AT_EV,
        })
    )
    assert data.is_ev_connected() is False


def test_is_ev_connected_unauthorized() -> None:
    """UNAUTHORIZED error means not connected."""
    data = _make_ev_charger_data(
        errors=frozenset({ComponentErrorCode.UNAUTHORIZED})
    )
    assert data.is_ev_connected() is False
