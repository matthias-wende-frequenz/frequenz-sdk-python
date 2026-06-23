# License: MIT
# Copyright © 2022 Frequenz Energy-as-a-Service GmbH
"""Definition of the user request."""

import dataclasses
from collections import abc
from datetime import timedelta

from frequenz.client.common.microgrid.components import ComponentId
from frequenz.quantities import Power


@dataclasses.dataclass
class Request:
    """Request to set power to the `PowerDistributingActor`."""

    power: Power
    """The requested power."""

    component_ids: abc.Set[ComponentId]
    """The component ids of the components to be used for this request."""

    adjust_power: bool = True
    """Whether to adjust the power to match the bounds.

    If `True`, the power will be adjusted (lowered) to match the bounds, so
    only the reduced power will be set.

    If `False` and the power is outside the available bounds, the request will
    fail and be replied to with an `OutOfBound` result.
    """

    bounds_validity: timedelta | None = None
    """Maximum validity for external operator bounds produced for this request.

    Component managers that control assets through temporary operator bounds can
    use this value to keep those external bounds aligned with the lifetime of the
    proposals currently determining this target power. Component managers that do
    not write such bounds can ignore it.
    """
