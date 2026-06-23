# License: MIT
# Copyright © 2026 Frequenz Energy-as-a-Service GmbH

"""Tests for per-proposal lifetime handling in power manager algorithms."""

from datetime import datetime, timedelta, timezone
from typing import TypeAlias

import pytest
from frequenz.client.common.microgrid.components import ComponentId
from frequenz.quantities import Power
from frequenz.sdk import timeseries
from frequenz.sdk.microgrid._power_managing import Proposal
from frequenz.sdk.microgrid._power_managing._base_classes import DefaultPower
from frequenz.sdk.microgrid._power_managing._matryoshka import Matryoshka
from frequenz.sdk.microgrid._power_managing._shifting_matryoshka import (
    ShiftingMatryoshka,
)
from frequenz.sdk.timeseries import _base_types

AlgorithmType: TypeAlias = type[Matryoshka] | type[ShiftingMatryoshka]


def _system_bounds() -> _base_types.SystemBounds:
    return _base_types.SystemBounds(
        timestamp=datetime.now(tz=timezone.utc),
        inclusion_bounds=timeseries.Bounds(
            lower=Power.from_watts(-200.0), upper=Power.from_watts(200.0)
        ),
        exclusion_bounds=timeseries.Bounds(lower=Power.zero(), upper=Power.zero()),
    )


def _proposal(
    source_id: str, priority: int, creation_time: float, max_age: float | None
) -> Proposal:
    return Proposal(
        component_ids=frozenset({ComponentId(1)}),
        source_id=source_id,
        preferred_power=Power.from_watts(10.0),
        bounds=timeseries.Bounds(None, None),
        priority=priority,
        creation_time=creation_time,
        max_age=max_age,
    )


@pytest.mark.parametrize("algorithm_type", [Matryoshka, ShiftingMatryoshka])
def test_per_proposal_short_lifetime_expires_before_default(
    algorithm_type: AlgorithmType,
) -> None:
    """A proposal with a short max age should expire before the default age."""
    algorithm = algorithm_type(
        max_proposal_age=timedelta(seconds=60.0), default_power=DefaultPower.ZERO
    )
    component_ids = frozenset({ComponentId(1)})
    proposal = _proposal("actor-1", 1, creation_time=100.0, max_age=2.0)

    algorithm.calculate_target_power(component_ids, proposal, _system_bounds())
    algorithm.drop_old_proposals(loop_time=103.0)

    # pylint: disable=protected-access
    assert component_ids not in algorithm._component_buckets
    # pylint: enable=protected-access


@pytest.mark.parametrize("algorithm_type", [Matryoshka, ShiftingMatryoshka])
def test_per_proposal_long_lifetime_survives_past_default(
    algorithm_type: AlgorithmType,
) -> None:
    """A proposal with a long max age should survive past the default age."""
    algorithm = algorithm_type(
        max_proposal_age=timedelta(seconds=60.0), default_power=DefaultPower.ZERO
    )
    component_ids = frozenset({ComponentId(1)})
    proposal = _proposal("actor-1", 1, creation_time=100.0, max_age=300.0)

    algorithm.calculate_target_power(component_ids, proposal, _system_bounds())
    algorithm.drop_old_proposals(loop_time=200.0)

    # pylint: disable=protected-access
    assert proposal in algorithm._component_buckets[component_ids]
    # pylint: enable=protected-access


@pytest.mark.parametrize("algorithm_type", [Matryoshka, ShiftingMatryoshka])
def test_proposal_without_lifetime_uses_algorithm_default(
    algorithm_type: AlgorithmType,
) -> None:
    """A proposal without max age should use the algorithm default."""
    algorithm = algorithm_type(
        max_proposal_age=timedelta(seconds=60.0), default_power=DefaultPower.ZERO
    )
    component_ids = frozenset({ComponentId(1)})
    proposal = _proposal("actor-1", 1, creation_time=100.0, max_age=None)

    algorithm.calculate_target_power(component_ids, proposal, _system_bounds())
    algorithm.drop_old_proposals(loop_time=159.0)

    # pylint: disable=protected-access
    assert proposal in algorithm._component_buckets[component_ids]
    # pylint: enable=protected-access

    algorithm.drop_old_proposals(loop_time=161.0)

    # pylint: disable=protected-access
    assert component_ids not in algorithm._component_buckets
    # pylint: enable=protected-access


@pytest.mark.parametrize("algorithm_type", [Matryoshka, ShiftingMatryoshka])
def test_proposals_with_different_lifetimes_expire_independently(
    algorithm_type: AlgorithmType,
) -> None:
    """Two proposals in the same bucket should expire according to their own ages."""
    algorithm = algorithm_type(
        max_proposal_age=timedelta(seconds=60.0), default_power=DefaultPower.ZERO
    )
    component_ids = frozenset({ComponentId(1)})
    short_lived = _proposal("actor-1", 1, creation_time=100.0, max_age=2.0)
    long_lived = _proposal("actor-2", 2, creation_time=100.0, max_age=300.0)

    bounds = _system_bounds()
    algorithm.calculate_target_power(component_ids, short_lived, bounds)
    algorithm.calculate_target_power(component_ids, long_lived, bounds)
    algorithm.drop_old_proposals(loop_time=103.0)

    # pylint: disable=protected-access
    assert short_lived not in algorithm._component_buckets[component_ids]
    assert long_lived in algorithm._component_buckets[component_ids]
    # pylint: enable=protected-access
