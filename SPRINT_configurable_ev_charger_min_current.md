# Sprint: Make EV Charger Minimum/Initial Current Configurable in SDK

## Problem

The SDK EV charger power distributor uses hard-coded/default current thresholds:

- `min_current = 6 A`
- `initial_current = 10 A`
- `increase_power_interval = 60 s`

These values live in:

`src/frequenz/sdk/microgrid/_power_distributing/_component_managers/_ev_charger_manager/_config.py`

They are physically sensible defaults, but currently they are not exposed through the public EV charger pool setup API. This makes it impossible for applications/tests/simulators to tune minimum EV charging behavior without patching SDK internals.

Observed behavior with a 5 kW pool target:

```text
voltage ~= 229.8 V
min_current = 6 A
min_power = 229.8 V * 6 A * 3 ~= 4136 W
```

The manager allocates ~4.136 kW to the first EV charger. The remaining ~864 W is below the same minimum threshold, so a second charger cannot be started. This behavior is correct for the configured minimum current, but the minimum should be configurable.

## Goal

Expose EV charger distribution parameters via a public SDK API while keeping the current defaults unchanged.

## Scope

Make the following EV charger distribution settings publicly configurable:

- `min_current: Current` — default `6 A`
- `initial_current: Current` — default `10 A`
- `increase_power_interval: timedelta` — default `60 s`

## Proposed API

Add a public configuration object, for example:

```python
@dataclass(frozen=True)
class EVChargerDistributionConfig:
    """Configuration for EV charger power distribution.

    Args:
        min_current: Minimum current required to allocate power to a charger.
        initial_current: Preferred initial current when starting a charger.
        increase_power_interval: Minimum interval between power increases.

    Attributes:
        min_current: Minimum current required to allocate power to a charger.
        initial_current: Preferred initial current when starting a charger.
        increase_power_interval: Minimum interval between power increases.
    """

    min_current: Current = field(default_factory=lambda: Current.from_amperes(6.0))
    initial_current: Current = field(default_factory=lambda: Current.from_amperes(10.0))
    increase_power_interval: timedelta = timedelta(seconds=60)
```

Expose it where EV charger pools are created, for example:

```python
microgrid.new_ev_charger_pool(
    component_ids={...},
    distribution_config=EVChargerDistributionConfig(
        min_current=Current.from_amperes(4.0),
        initial_current=Current.from_amperes(6.0),
        increase_power_interval=timedelta(seconds=10),
    ),
)
```

Exact naming/location can be adapted to existing SDK API conventions.

## Implementation Plan

1. Locate the public EV charger pool creation path:
   - `src/frequenz/sdk/microgrid/_data_pipeline.py`
   - `src/frequenz/sdk/timeseries/ev_charger_pool/`
   - `src/frequenz/sdk/microgrid/_power_wrapper.py`

2. Add a public EV charger distribution config type.
   - Keep the existing internal `EVDistributionConfig` if useful.
   - Convert public config into internal config when constructing the power distributing actor / EV charger manager.

3. Thread the config through the SDK pipeline:
   - public `new_ev_charger_pool(...)` or equivalent
   - data pipeline / power wrapper
   - `PowerDistributingActor`
   - `EVChargerManager`
   - internal `EVDistributionConfig`

4. Preserve existing defaults exactly:
   - `min_current = 6 A`
   - `initial_current = 10 A`
   - `increase_power_interval = 60 s`

5. Add validation:
   - currents must be positive
   - `initial_current >= min_current`
   - interval must be positive or non-negative, depending on existing SDK style

6. Add tests:
   - default config preserves current behavior
   - custom `min_current` affects computed minimum start power
   - custom `initial_current` affects first allocation when enough power is available
   - invalid configs are rejected
   - config is actually propagated from public EV charger pool API to `EVChargerManager`

7. Update docs/examples:
   - Document EV charger distribution config.
   - Mention that the minimum charging power is calculated as:
     `voltage * min_current * 3`.

## Acceptance Criteria

- Applications can configure EV charger `min_current` without patching SDK internals.
- Current behavior remains unchanged when no config is supplied.
- Tests cover config propagation and allocation behavior.
- Documentation explains the distinction between:
  - proposal bounds, which constrain pool-level target power selection, and
  - EV charger distribution config, which controls per-charger minimum/start allocation.

## Notes from Debugging

The proposal bounds seen in the debug logs were correctly propagated:

```text
ComponentPool.propose_power:
  power=5 kW
  bounds=Bounds(lower=Power(value=1000.0, exponent=0), upper=Power(value=7000.0, exponent=0))

PowerManagingActor:
  power=5 kW
  bounds=Bounds(lower=Power(value=1000.0, exponent=0), upper=Power(value=7000.0, exponent=0))
```

The 4.136 kW charger allocation did not come from the proposal lower bound. It came from the EV charger manager's configured/default minimum current:

```python
min_power = voltage * self._config.min_current * 3.0
```
