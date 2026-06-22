# License: MIT
# Copyright © 2026 Frequenz Energy-as-a-Service GmbH

"""Tests for EV charger component data parsing."""

from datetime import datetime, timezone

from frequenz.client.microgrid.component import ComponentDataSamples
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
