# Sprint Plan: Configurable Proposal Lifetime for EV Charger Pools

## Context

The power manager drops proposals that are older than a hardcoded **60 seconds**
(`timedelta(seconds=60.0)` in `_power_managing_actor.py`). This lifetime is the same
for batteries, EV chargers, and PV inverters — there is no way for a caller to
influence it.

For the truck-charging app this is mostly fine because the control loop re-proposes on
every grid-power tick (≈1 s resampling). But there are legitimate reasons to want a
different lifetime:

- **Longer lifetime**: a slow-polling actor (e.g. one that only reacts to dispatch
  events every few minutes) may want its proposals to stay alive between updates
  without having to run a keep-alive timer.
- **Shorter lifetime**: a safety-critical actor may want stale proposals to expire
  quickly so that a crash or hang does not leave a phantom allocation in the power
  manager for up to 60 s.

The lifetime should be surfaced as a parameter the caller can set when creating a pool.

## Goal

Allow callers of `microgrid.new_ev_charger_pool()` (and, for consistency,
`new_battery_pool()` and `new_pv_pool()`) to **optionally** specify a
`max_proposal_age` that overrides the hardcoded 60 s default.

## Current Call Chain (EV charger path)

```
microgrid.new_ev_charger_pool(priority=…)
  └─ _DataPipeline.new_ev_charger_pool(priority=…)
       └─ EVChargerPool(pool_ref_store=…, priority=…)
            └─ .propose_power(power)
                 └─ sends Proposal(creation_time=now) to PowerWrapper.proposal_channel
                      └─ PowerManagingActor receives it
                           └─ Matryoshka / ShiftingMatryoshka drops proposals
                              older than self._max_proposal_age_sec  (hardcoded 60 s)
```

The 60 s constant lives in `PowerManagingActor.__init__` (lines 85, 90).

## Proposed Changes

### 1. `Proposal` dataclass — add `max_age` field

**File:** `src/frequenz/sdk/microgrid/_power_managing/_base_classes.py`

Add an optional `max_age: float | None` field (seconds) to `Proposal`. When `None`
the algorithm falls back to its own default.

```python
@dataclasses.dataclass(frozen=True, kw_only=True)
class Proposal:
    ...
    creation_time: float
    max_age: float | None = None   # <-- NEW: per-proposal lifetime in seconds
```

### 2. Matryoshka / ShiftingMatryoshka — honour per-proposal `max_age`

**Files:**
- `src/frequenz/sdk/microgrid/_power_managing/_matryoshka.py`
- `src/frequenz/sdk/microgrid/_power_managing/_shifting_matryoshka.py`

In the stale-proposal eviction loop, use `proposal.max_age` when set, falling back
to the algorithm-wide `self._max_proposal_age_sec`:

```python
effective_age = proposal.max_age if proposal.max_age is not None else self._max_proposal_age_sec
if (loop_time - proposal.creation_time) > effective_age:
    ...  # drop
```

### 3. `ComponentPool.propose_power()` — accept `max_proposal_age`

**File:** `src/frequenz/sdk/timeseries/component_pool/_component_pool.py`

```python
async def propose_power(
    self,
    power: Power | None,
    bounds: Bounds[Power | None] = Bounds(None, None),
    *,
    max_proposal_age: timedelta | None = None,    # <-- NEW
) -> None:
    await self._pool_ref_store.power_manager_requests_sender.send(
        Proposal(
            ...
            creation_time=asyncio.get_running_loop().time(),
            max_age=max_proposal_age.total_seconds() if max_proposal_age is not None else None,
        )
    )
```

### 4. `EVChargerPool.propose_power()` — forward the new parameter

**File:** `src/frequenz/sdk/timeseries/ev_charger_pool/_ev_charger_pool.py`

```python
async def propose_power(
    self,
    power: Power | None,
    bounds: Bounds[Power | None] = Bounds(None, None),
    *,
    max_proposal_age: timedelta | None = None,    # <-- NEW
) -> None:
    ...
    await super().propose_power(power, bounds=bounds, max_proposal_age=max_proposal_age)
```

(Same for `BatteryPool` and `PVPool` for consistency.)

### 5. Tests

**Files:**
- `tests/timeseries/_ev_charger_pool/test_ev_charger_pool_control_methods.py`
- New or extended tests for the Matryoshka stale-proposal eviction logic

Add tests that:
1. A proposal with a short `max_proposal_age` (e.g. 2 s) expires before the
   default 60 s window.
2. A proposal with a long `max_proposal_age` (e.g. 300 s) survives past the default
   60 s window.
3. A proposal with `max_proposal_age=None` still uses the algorithm default (60 s).
4. Two proposals from different actors with different lifetimes expire independently.

## Out of Scope

- Changing the *algorithm-wide* default (60 s) via `initialize()` /
  `_DataPipeline` / `PowerWrapper`. This is a separate concern and can be done later
  if needed; the per-proposal knob is sufficient for the truck-charging use case.
- Changing the 1 s stale-proposal-check timer interval. It already provides
  second-granularity eviction which is adequate.

## Commits (suggested split)

1. **feat(power-manager): add `max_age` field to `Proposal`**
   — `_base_classes.py` change only, no behaviour change yet.

2. **feat(power-manager): honour per-proposal `max_age` in Matryoshka algorithms**
   — `_matryoshka.py`, `_shifting_matryoshka.py`, plus unit tests for the eviction
   logic.

3. **feat(ev-charger-pool): accept `max_proposal_age` in `propose_power()`**
   — `_component_pool.py`, `_ev_charger_pool.py`, `_battery_pool.py`, `_pv_pool.py`,
   plus integration-style tests.
