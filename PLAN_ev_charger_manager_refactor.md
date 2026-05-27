# Refactor Plan: EVChargerManager Power Distribution

## Design Critique

The current `EVChargerManager` has several fundamental design issues that result in
unnecessarily high latency, lost user intent, and confused responsibilities.

### Problem 1: The manager ignores the user's requested power

When `propose_power(10kW)` arrives via the `target_power_rx` channel, the manager
stores `self._target_power = 10kW` but **does not immediately allocate it to
chargers**. It only acts on the target power in two narrow cases:

- `target_power < used_power` → throttle
- `target_power < allocated_power` → deallocate unused

If the target power **increases** (the common case for `propose_power`), **nothing
happens**. The manager waits for the next `ev_charger_data_rx` message to trigger
`_act_on_new_data()`, which then applies the 60-second `increase_power_interval`
throttle. This means:

1. Power increase is driven by **data arrival**, not by the **user's request**.
2. Response latency = data stream interval + up to 60s throttle timer.
3. The user calls `propose_power(10kW)` and gets... nothing for a minute.

### Problem 2: The 60-second throttle is applied unconditionally, conflates concerns, and lives at the wrong layer

`increase_power_interval` (60s) is meant to protect chargers from rapid current
changes. But it's applied as a single coarse gate on **all** power changes triggered
by data arrival, regardless of:

- Whether the user just sent a new power request (should be acted on promptly)
- Whether the charger actually needs ramping protection (many modern chargers handle
  instant changes fine)
- Whether the change is small (e.g., 100W increase) or large (0→11kW)

The throttle is also reset on **every** allocation including the initial one
(`last_reallocation_time=now` in the `_run` new-charger branch), so even after an EV
connects, the first real power adjustment is delayed by 60 seconds.

Beyond the design issues above, this protection is at the **wrong layer**. Whether a
specific charger model needs ramping (and how fast) is a property of the **hardware**,
not of the SDK's power-distribution policy. The correct home for it is the device
controller in `matrix-controller`, which already has `device_controller/utils/ramp.rs`
used by inverter controllers for exactly this reason (see the module docstring:
*"When increasing the power of an inverter we want to do it in small steps to reduce
wear on various components. We can always drop power levels quickly."*). The EV
charger device controllers (`alfen_ng9xx`, `keba_p30`, `huawei`, `se_hil`) currently
have no ramp logic — that's the gap. Putting ramping in the SDK manager forces one
global policy across heterogeneous chargers and conflates *"what should each charger
get"* with *"how fast can this charger change"*.

### Problem 3: Power allocation is per-charger, triggered by per-charger data events

The `_act_on_new_data()` method makes allocation decisions **per charger** as data
arrives. This means:

- With N chargers, each charger independently tries to grab available power.
- The first charger to send data after the throttle window gets the power.
- There's no holistic "distribute X watts across N chargers" step when a new target
  arrives.

The `target_power_rx` branch handles **decreases** holistically (`_throttle_ev_chargers`,
`_deallocate_unused_power`) but **increases** are left to the per-charger data path.
This asymmetry is confusing and buggy.

### Problem 4: `initial_current` is a poor substitute for target-based allocation

When a new EV connects, `_allocate_new_ev()` assigns a hardcoded `initial_current`
(10A × voltage × 3 phases) regardless of what the user actually requested. If the
user requested 11kW and only one charger is connected, it should get 11kW, not ~7kW.

### Problem 5: No feedback loop for `propose_power` result

The `Result` sent via `results_sender` only reports API call success/failure, not
whether the requested power was actually allocated. If the throttle timer suppresses
allocation, the caller gets no feedback — `propose_power` silently does nothing.

---

## Proposed Refactor

### Core principle: **React to power requests, not just data events**

The target_power_rx branch should be the **primary driver** of power allocation.
Data events should update charger state (bounds, connection status, actual power)
but not independently trigger allocation decisions.

### New architecture

```
target_power_rx received:
  1. Store new target power
  2. Call _redistribute_power() → holistic allocation across all connected chargers
  3. Apply allocations via API
  4. Send result

ev_charger_data_rx received:
  1. Update charger state (bounds, connection, actual power)
  2. If EV newly connected/disconnected → call _redistribute_power()
  3. If bounds changed significantly → call _redistribute_power()
  4. Otherwise → no allocation action (state update only)
```

### Changes

#### A. Add `_redistribute_power()` — holistic allocation

A single method that takes the current `_target_power` and distributes it across
all connected chargers proportionally (or equally, or by priority — configurable).

```python
def _redistribute_power(self) -> dict[ComponentId, Power]:
    """Distribute target power across all connected EV chargers.

    Considers each charger's bounds (min/max power) and allocates
    proportionally within those constraints.
    """
```

This replaces the scattered allocation logic in `_act_on_new_data()` and
`_allocate_new_ev()`.

#### B. React to target power increases immediately

In the `target_power_rx` branch, **always** call `_redistribute_power()`, not just
on decreases:

```python
elif selected_from(selected, target_power_rx):
    self._target_power = selected.message.power
    target_power_changes = self._redistribute_power()
```

#### C. Remove `increase_power_interval` — push ramp protection down to the device controller

Drop the throttle from the SDK entirely. The `EVChargerManager` becomes
ramp-agnostic: it computes and emits setpoints; smoothing/ramping (if needed) is
the responsibility of the device controller in `matrix-controller`, alongside the
existing inverter ramp logic in `device_controller/utils/ramp.rs`.

No replacement field is added to `EVDistributionConfig`. This keeps the SDK config
clean and ensures protection policy is co-located with the hardware that needs it,
so heterogeneous chargers can each apply their own rules.

**Follow-up (separate task, not in this refactor):** extend `ramp.rs` (or add an EV
variant) for the EV charger controllers in matrix-controller that need it (likely
Alfen and Keba — to be decided per hardware).

**Caveat to verify before landing:** if any deployment talks to chargers without
going through matrix-controller (e.g., directly via the microgrid API to a charger
that lacks server-side ramping), removing the SDK-level gate would leave it
unprotected. The right fix in that case is a ramp wrapper at the data-source
boundary, not in the allocation manager — but we should confirm no such path is in
production use first.

#### D. Remove `initial_current` concept

Replace with: allocate based on target power and charger bounds. No special "initial"
value — if 11kW is requested and available, allocate 11kW.

#### E. Trigger redistribution on connection state changes

When `_act_on_new_data()` detects a new EV connection or disconnection, call
`_redistribute_power()` instead of the ad-hoc `_allocate_new_ev()` / zero-out logic.
This ensures all chargers are rebalanced when the set of connected EVs changes.

#### F. Data-driven reallocation only on significant bound changes

Instead of trying to allocate power on every data tick, only trigger
`_redistribute_power()` when a charger's bounds change by more than a threshold
(e.g., >5% or >100W). This eliminates the need for the throttle timer entirely.

---

## Implementation Steps

### Step 1: Add `_redistribute_power()`

Create the holistic allocation method. Simple initial strategy: divide target power
equally among connected chargers, clamped to each charger's [min, max] bounds.
Excess from chargers at their limit gets redistributed to others.

**Files:** `_ev_charger_manager.py`

### Step 2: Refactor `target_power_rx` branch

Call `_redistribute_power()` for all target power changes (increases AND decreases).
Remove `_throttle_ev_chargers()` and `_deallocate_unused_power()` — they become
special cases of `_redistribute_power()`.

**Files:** `_ev_charger_manager.py`

### Step 3: Refactor `ev_charger_data_rx` branch

- Update state unconditionally.
- Only call `_redistribute_power()` on connection change or significant bound change.
- Remove `_act_on_new_data()` and `_allocate_new_ev()`.

**Files:** `_ev_charger_manager.py`

### Step 4: Remove `increase_power_interval` from config and manager

Delete the field from `EVDistributionConfig` and remove all throttle bookkeeping
(`last_reallocation_time`, the time-gate checks, and the associated branches) from
`_ev_charger_manager.py`. No replacement field is added.

File a follow-up issue in `matrix-controller` to add ramp-up handling to the EV
charger device controllers that need it, reusing/extending
`src/device_controller/utils/ramp.rs`.

**Files:** `_config.py`, `_ev_charger_manager.py` (+ follow-up issue in
`matrix-controller`)

### Step 5: Remove `initial_current` from config

No longer needed — initial allocation uses the same `_redistribute_power()` path.
Any initial soft-start behavior is likewise a device-controller concern.

**Files:** `_config.py`, `_ev_charger_manager.py`

### Step 6: Update tests

Adapt existing tests to the new behavior. Key behavioral changes to test:

- Power increase request → immediate allocation (no 60s delay)
- New EV connection → redistribution across all chargers
- EV disconnection → redistribution of freed power
- Optional ramp rate limiting
- Multiple chargers: fair distribution within bounds

**Files:** tests for `_ev_charger_manager`

### Step 7: Clean up

Remove dead code (`_act_on_new_data`, `_allocate_new_ev`, `_throttle_ev_chargers`,
`_deallocate_unused_power`). Update docstrings.

---

## Risk Assessment

- **Low risk:** The current design is already broken for the primary use case (power
  increases are delayed by 60s). The refactor makes the happy path work correctly.
- **Medium risk — charger protection moves layers:** Ramp/throttle responsibility is
  removed from the SDK and handed to `matrix-controller`'s device controllers (where
  inverter ramping already lives in `device_controller/utils/ramp.rs`). Until the
  follow-up issue lands, EV chargers behind matrix-controller will receive setpoints
  without ramping. Mitigation: open the matrix-controller follow-up before/with this
  refactor, and verify no deployments bypass matrix-controller for chargers that
  require ramp protection.
- **Testing:** Existing tests likely encode the current broken behavior. They need
  updating, not preserving.
