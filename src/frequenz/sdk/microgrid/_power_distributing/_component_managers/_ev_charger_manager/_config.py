# License: MIT
# Copyright © 2024 Frequenz Energy-as-a-Service GmbH

"""Configuration for the power distributor's EV charger manager."""

from collections import abc
from dataclasses import dataclass

from frequenz.client.common.microgrid.components import ComponentId


@dataclass(frozen=True)
class EVDistributionConfig:
    """Configuration for the power distributor's EV charger manager."""

    component_ids: abc.Set[ComponentId]
    """The component ids of the EV chargers."""

