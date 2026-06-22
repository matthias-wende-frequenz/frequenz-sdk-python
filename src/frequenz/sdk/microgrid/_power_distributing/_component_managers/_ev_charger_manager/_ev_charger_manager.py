# License: MIT
# Copyright © 2024 Frequenz Energy-as-a-Service GmbH

"""Manage EV chargers for the power distributor."""

import asyncio
import collections.abc
import logging
from datetime import datetime, timedelta

from frequenz.channels import (
    Broadcast,
    Sender,
    merge,
    select,
    selected_from,
)
from frequenz.channels.timer import SkipMissedAndDrift, Timer
from frequenz.client.common.microgrid.components import ComponentId
from frequenz.client.microgrid import ApiClientError, MicrogridApiClient, Validity
from frequenz.client.microgrid.component import EvCharger
from frequenz.client.microgrid.metrics import Bounds, Metric
from frequenz.quantities import Power
from typing_extensions import override

from ....._internal._asyncio import run_forever
from ....._internal._math import is_close_to_zero
from .... import connection_manager
from ...._old_component_data import EVChargerData, _is_unknown_rated_power_bound
from ..._component_pool_status_tracker import ComponentPoolStatusTracker
from ..._component_status import ComponentPoolStatus, EVChargerStatusTracker
from ...request import Request
from ...result import PartialFailure, Result, Success
from .._component_manager import ComponentManager
from ._states import EvcState, EvcStates

_logger = logging.getLogger(__name__)

# EV chargers are controlled by constraining their AC active-power inclusion
# bounds rather than by writing a direct power setpoint. The microgrid service
# (nitrogen) continuously relays the effective ``AC_POWER_ACTIVE`` upper bound
# to the asset, so a direct setpoint would be immediately overwritten by that
# relay; an upper bound, on the other hand, is honoured.
#
# Bounds expire after ``_BOUNDS_VALIDITY``. Unlike an expiring setpoint (which
# resets the asset to 0 W), an expiring upper bound *removes* the cap and lets
# the charger draw full power, so the currently-desired caps must be re-asserted
# well before they lapse.
_BOUNDS_VALIDITY = Validity.ONE_MINUTE
_BOUNDS_RENEWAL_INTERVAL = timedelta(seconds=_BOUNDS_VALIDITY.value / 2)


class EVChargerManager(ComponentManager):
    """Manage ev chargers for the power distributor."""

    @override
    def __init__(
        self,
        component_pool_status_sender: Sender[ComponentPoolStatus],
        results_sender: Sender[Result],
        api_power_request_timeout: timedelta,
    ):
        """Initialize the ev charger data manager.

        Args:
            component_pool_status_sender: Channel for sending information about which
                components are expected to be working.
            results_sender: Channel for sending results of power distribution.
            api_power_request_timeout: Timeout to use when making power requests to
                the microgrid API.
        """
        self._results_sender = results_sender
        self._api_power_request_timeout = api_power_request_timeout
        self._ev_charger_ids = self._get_ev_charger_ids()
        self._evc_states = EvcStates()
        self._component_pool_status_tracker = ComponentPoolStatusTracker(
            component_ids=self._ev_charger_ids,
            component_status_sender=component_pool_status_sender,
            max_data_age=timedelta(seconds=10.0),
            max_blocking_duration=timedelta(seconds=30.0),
            component_status_tracker_type=EVChargerStatusTracker,
        )
        self._target_power = Power.zero()
        self._target_power_channel = Broadcast[Request](name="target_power")
        self._target_power_tx = self._target_power_channel.new_sender()
        self._task: asyncio.Task[None] | None = None
        self._latest_request: Request = Request(Power.zero(), set())
        # The per-charger active-power caps (watts) most recently sent to the
        # API. Used to periodically re-assert the bounds before they expire.
        self._last_sent_allocations: dict[ComponentId, Power] = {}
        # The maximum non-zero active-power inclusion upper bound seen for each
        # charger. EV chargers are controlled by adding operator bounds
        # ``[0, allocation]``. Nitrogen intersects those with device bounds and
        # echoes the result back as the current inclusion bound, so using the
        # latest inclusion upper bound directly would make the allocator read
        # its own cap as the physical ceiling and ratchet to zero. Keep the
        # pre-cap high-water mark and use it as the physical ceiling whenever a
        # separate rated bound is unavailable.
        self._max_seen_inclusion_upper: dict[ComponentId, float] = {}

    @override
    def component_ids(self) -> collections.abc.Set[ComponentId]:
        """Return the set of ev charger ids."""
        return self._ev_charger_ids

    @override
    async def start(self) -> None:
        """Start the ev charger data manager."""
        # Need to start a task only if there are EV chargers in the component graph.
        if self._ev_charger_ids:
            self._task = asyncio.create_task(run_forever(self._run))

    @override
    async def distribute_power(self, request: Request) -> None:
        """Distribute the requested power to the ev chargers.

        Args:
            request: Request to get the distribution for.
        """
        if self._ev_charger_ids:
            await self._target_power_tx.send(request)

    @override
    async def stop(self) -> None:
        """Stop the ev charger manager."""
        await self._component_pool_status_tracker.stop()

    def _get_ev_charger_ids(self) -> collections.abc.Set[ComponentId]:
        """Return the IDs of all EV chargers present in the component graph."""
        return {
            evc.id
            for evc in connection_manager.get().component_graph.components(
                matching_types=EvCharger
            )
        }

    def _effective_upper_bound(self, evc: EvcState) -> float:
        """Return the physical charging ceiling to use for allocation.

        Args:
            evc: The EV charger state whose current data should be inspected.

        Returns:
            A non-negative active-power upper bound in watts. Prefer the
                non-echoed rated bound when available. Otherwise use the maximum
                usable inclusion upper bound seen before operator caps were
                echoed back by the API. If the API only reports the unbounded
                +/-99 GW sentinel, treat the charger as having unknown headroom
                so the finite target can still be distributed and clipped by the
                service/device.
        """
        rated_upper = evc.last_data.active_power_rated_upper_bound
        if rated_upper is not None:
            return max(0.0, rated_upper)

        component_id = evc.component_id
        remembered_upper = self._max_seen_inclusion_upper.get(component_id)
        current_upper = evc.last_data.active_power_inclusion_upper_bound
        if _is_unknown_rated_power_bound(current_upper):
            return remembered_upper if remembered_upper is not None else float("inf")

        return max(max(0.0, current_upper), remembered_upper or 0.0)

    def _remember_inclusion_upper_bound(self, data: EVChargerData) -> None:
        """Remember a per-charger inclusion upper-bound high-water mark.

        Args:
            data: The latest EV charger telemetry sample.

        The latest inclusion upper bound can be our own operator cap echoed back
        by the API, but the highest value seen is still the best available
        estimate of the physical ceiling when rated bounds are unavailable.
        """
        if _is_unknown_rated_power_bound(data.active_power_inclusion_upper_bound):
            return

        current_upper = max(0.0, data.active_power_inclusion_upper_bound)
        if current_upper <= 0.0:
            return
        previous_upper = self._max_seen_inclusion_upper.get(data.component_id, 0.0)
        if current_upper > previous_upper:
            self._max_seen_inclusion_upper[data.component_id] = current_upper

    def _redistribute_power(self) -> dict[ComponentId, Power]:
        """Distribute target power across connected EV chargers.

        Each connected charger either receives zero power or an allocation within
        its physical/rated ceiling. Chargers that cannot be given at least their
        lower inclusion bound are excluded from the allocation.

        Returns:
            Updated power allocations for chargers whose target allocation changed.
        """
        connected_states = [
            evc for evc in self._evc_states.values() if evc.last_data.is_ev_connected()
        ]

        if not connected_states:
            return {
                evc.component_id: Power.zero()
                for evc in self._evc_states.values()
                if evc.last_allocation > Power.zero()
            }

        total_target = max(self._target_power, Power.zero()).as_watts()
        upper_bounds = {
            evc.component_id: self._effective_upper_bound(evc)
            for evc in connected_states
        }
        lower_bounds = {
            evc.component_id: min(
                max(0.0, evc.last_data.active_power_inclusion_lower_bound),
                upper_bounds[evc.component_id],
            )
            for evc in connected_states
        }

        included_ids = {
            evc.component_id
            for evc in connected_states
            if upper_bounds[evc.component_id] > 0.0
        }
        while included_ids:
            total_minimum = sum(
                lower_bounds[component_id] for component_id in included_ids
            )
            if total_minimum <= total_target:
                break

            component_id = max(
                included_ids,
                key=lambda candidate: (
                    lower_bounds[candidate],
                    upper_bounds[candidate],
                    candidate,
                ),
            )
            included_ids.remove(component_id)

        allocations = {evc.component_id: 0.0 for evc in connected_states}
        for component_id in included_ids:
            allocations[component_id] = lower_bounds[component_id]

        remaining_power = total_target - sum(allocations.values())
        remaining_ids = {
            component_id
            for component_id in included_ids
            if upper_bounds[component_id] - allocations[component_id] > 0.0
        }

        while remaining_ids and remaining_power > 0.0:
            fair_share = remaining_power / len(remaining_ids)
            progress = False
            for component_id in tuple(remaining_ids):
                headroom = upper_bounds[component_id] - allocations[component_id]
                allocatable = min(fair_share, headroom)
                allocations[component_id] += allocatable
                remaining_power -= allocatable
                progress = progress or allocatable > 0.0
                if is_close_to_zero(
                    upper_bounds[component_id] - allocations[component_id]
                ):
                    remaining_ids.remove(component_id)
            if not progress:
                break

        target_power_changes: dict[ComponentId, Power] = {}
        connected_ids = {evc.component_id for evc in connected_states}
        for evc in self._evc_states.values():
            target_power = Power.from_watts(allocations.get(evc.component_id, 0.0))
            if evc.component_id not in connected_ids:
                target_power = Power.zero()
            if target_power != evc.last_allocation:
                target_power_changes[evc.component_id] = target_power

        return target_power_changes

    def _has_significant_bound_change(
        self, previous: EVChargerData, current: EVChargerData
    ) -> bool:
        """Check whether EV charger bounds changed enough to trigger redistribution."""
        previous_upper = previous.active_power_inclusion_upper_bound
        current_upper = current.active_power_inclusion_upper_bound
        delta = abs(current_upper - previous_upper)
        threshold = max(abs(previous_upper), abs(current_upper)) * 0.05
        return delta > max(100.0, threshold)

    async def _run(self) -> None:  # pylint: disable=too-many-locals
        """Run the main event loop of the EV charger manager."""
        api = connection_manager.get().api_client
        ev_charger_data_rx = merge(
            *(EVChargerData.subscribe(api, evc_id) for evc_id in self._ev_charger_ids)
        )
        target_power_rx = self._target_power_channel.new_receiver()
        renewal_timer = Timer(_BOUNDS_RENEWAL_INTERVAL, SkipMissedAndDrift())
        async for selected in select(
            ev_charger_data_rx, target_power_rx, renewal_timer
        ):
            target_power_changes = {}
            is_target_power_event = False

            if selected_from(selected, renewal_timer):
                # Re-assert the currently-desired caps so the bounds do not
                # lapse (which would uncap the chargers). This is not a response
                # to a `distribute_power()` call, so no Result is emitted.
                if self._last_sent_allocations:
                    _logger.debug(
                        "Renewing EV charger bounds: %s", self._last_sent_allocations
                    )
                    await self._send_api_bounds(
                        api,
                        dict(self._last_sent_allocations),
                        self._api_power_request_timeout,
                    )
                continue

            if selected_from(selected, ev_charger_data_rx):
                evc_data = selected.message
                self._remember_inclusion_upper_bound(evc_data)
                if evc_data.component_id not in self._evc_states:
                    # First time we see this charger: record its state but do
                    # not push any setpoint. The microgrid API may reject a 0
                    # write if the charger's inclusion lower bound is > 0 (EV
                    # already plugged in), and there is no user request driving
                    # this event. Any subsequent legitimate trigger (target
                    # power, connection change, significant bounds change) will
                    # invoke `_redistribute_power()` and set the correct value.
                    self._evc_states.add_evc(
                        EvcState(
                            component_id=evc_data.component_id,
                            last_data=evc_data,
                            last_allocation=Power.zero(),
                        )
                    )
                    self._evc_states.get(evc_data.component_id).update_state(evc_data)
                    target_power_changes = self._redistribute_power()
                else:
                    evc_state = self._evc_states.get(evc_data.component_id)
                    previous_data = evc_state.last_data
                    was_connected = previous_data.is_ev_connected()
                    evc_state.update_state(evc_data)
                    is_connected = evc_data.is_ev_connected()
                    connection_changed = was_connected != is_connected
                    bounds_changed = self._has_significant_bound_change(
                        previous_data, evc_data
                    )

                    if connection_changed:
                        if is_connected:
                            _logger.info(
                                "New EV connected to EV charger %s",
                                evc_data.component_id,
                            )
                        else:
                            _logger.info(
                                "EV disconnected from EV charger %s",
                                evc_data.component_id,
                            )
                    if connection_changed or bounds_changed:
                        target_power_changes = self._redistribute_power()

            elif selected_from(selected, target_power_rx):
                is_target_power_event = True
                self._latest_request = selected.message
                self._target_power = selected.message.power
                _logger.debug("New target power: %s", self._target_power)
                target_power_changes = self._redistribute_power()

            if target_power_changes:
                _logger.debug("Setting power to EV chargers: %s", target_power_changes)
                for component_id, power in target_power_changes.items():
                    self._evc_states.get(component_id).update_last_allocation(power)
                result = await self._set_api_power(
                    api, target_power_changes, self._api_power_request_timeout
                )
                # Only emit a Result when this loop iteration was triggered by
                # a target_power request. Data-driven reallocations (new
                # charger observed, connection change, bounds change) are not
                # responses to a `distribute_power()` call, and emitting a
                # Result for them confuses `PowerManagingActor`, which
                # correlates results to its own outstanding requests.
                if is_target_power_event:
                    await self._results_sender.send(result)
            elif is_target_power_event:
                # Target power request produced no allocation changes — send a
                # result immediately so callers don't hang.
                allocated = self._evc_states.get_total_allocated_power()
                excess = max(self._target_power - allocated, Power.zero())
                await self._results_sender.send(
                    Success(
                        succeeded_components=set(),
                        succeeded_power=allocated,
                        excess_power=excess,
                        request=self._latest_request,
                    )
                )

    async def _send_api_bounds(
        self,
        api: MicrogridApiClient,
        allocations: dict[ComponentId, Power],
        api_request_timeout: timedelta,
    ) -> tuple[set[ComponentId], set[ComponentId]]:
        """Send EV charger active-power upper bounds to the microgrid API.

        Each charger is capped by adding an ``AC_POWER_ACTIVE`` inclusion bound
        of ``[0, allocation]`` watts. The microgrid service relays this upper
        bound to the asset; a direct power setpoint would instead be overwritten
        by that relay, which is why bounds are used here.

        Args:
            api: The microgrid API client to use for setting the bounds.
            allocations: Desired per-charger active-power caps (watts, ``>= 0``).
            api_request_timeout: The timeout for the API request.

        Returns:
            A tuple ``(succeeded_components, failed_components)`` of the
                component IDs whose bounds were set successfully and those that
                failed, respectively.
        """
        tasks: dict[ComponentId, asyncio.Task[datetime | None]] = {}
        for component_id, power in allocations.items():
            tasks[component_id] = asyncio.create_task(
                api.add_component_bounds(
                    component_id,
                    Metric.AC_POWER_ACTIVE,
                    [Bounds(lower=0.0, upper=max(0.0, power.as_watts()))],
                    validity=_BOUNDS_VALIDITY,
                )
            )
        _, pending = await asyncio.wait(
            tasks.values(),
            timeout=api_request_timeout.total_seconds(),
            return_when=asyncio.ALL_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        failed_components: set[ComponentId] = set()
        succeeded_components: set[ComponentId] = set()
        for component_id, task in tasks.items():
            try:
                task.result()
            except asyncio.CancelledError:
                _logger.warning(
                    "Timeout while setting bounds for EV charger %s", component_id
                )
            except ApiClientError as exc:
                _logger.warning(
                    "Got a client error while setting bounds for EV charger %s: %s",
                    component_id,
                    exc,
                )
            except Exception:  # pylint: disable=broad-except
                _logger.exception(
                    "Unknown error while setting bounds for EV charger: %s",
                    component_id,
                )
            else:
                succeeded_components.add(component_id)
                continue

            failed_components.add(component_id)

        return succeeded_components, failed_components

    async def _set_api_power(
        self,
        api: MicrogridApiClient,
        target_power_changes: dict[ComponentId, Power],
        api_request_timeout: timedelta,
    ) -> Result:
        """Send the EV charger power changes to the microgrid API.

        Args:
            api: The microgrid API client to use for setting the power.
            target_power_changes: A dictionary containing the new power allocations for
                the EV chargers.
            api_request_timeout: The timeout for the API request.

        Returns:
            Power distribution result, corresponding to the result of the API
                request.
        """
        succeeded_components, failed_components = await self._send_api_bounds(
            api, target_power_changes, api_request_timeout
        )

        # Remember the caps that were applied so they can be re-asserted before
        # the bounds expire (see ``_BOUNDS_RENEWAL_INTERVAL``).
        for component_id in succeeded_components:
            self._last_sent_allocations[component_id] = target_power_changes[
                component_id
            ]

        failed_power = Power.zero()
        for component_id in failed_components:
            failed_power += target_power_changes[component_id]

        allocated = self._evc_states.get_total_allocated_power()
        excess = max(self._target_power - allocated, Power.zero())

        if failed_components:
            return PartialFailure(
                failed_components=failed_components,
                succeeded_components=succeeded_components,
                failed_power=failed_power,
                succeeded_power=allocated - failed_power,
                excess_power=excess,
                request=self._latest_request,
            )
        return Success(
            succeeded_components=succeeded_components,
            succeeded_power=allocated,
            excess_power=excess,
            request=self._latest_request,
        )
