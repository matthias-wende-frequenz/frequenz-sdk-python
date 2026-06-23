# Sprint Plan: Surface EV Charger Rated (Physical) Power Bounds in the Frequenz SDK

## Context

The truck-charging control loop needs the **physical maximum charging power** of the
EV charger pool as the anti-windup ceiling for the EV cap. Today the app cannot get
it, because the only bound the SDK exposes already encodes our own control command.

### Root cause (traced end-to-end)

The SDK's `EVChargerManager` controls chargers by writing
`add_component_bounds([0, allocation])` on `AC_POWER_ACTIVE` — it does **not** write a
power setpoint (a setpoint would be overwritten by nitrogen relaying the effective
upper bound to the asset). nitrogend then **intersects** that operator cap into the
*reported* `AC_POWER_ACTIVE` inclusion bound of the streamed telemetry:

```
reported active_power_inclusion_upper_bound = min(physical_max, our_applied_cap)
```

So whenever we cap below the physical maximum (i.e. normal operation),
`ev_charger_pool.power_status.bounds.upper` reads back our own command. Using it as
the EV physical maximum closes a feedback loop that ratchets the cap to 0 (observed
live: "EV max power: 0 W", no EV charging despite chargers WORKING).

### The physical max *is* available in the API — just not plumbed through the SDK

nitrogend exposes a separate, **non-echoed** channel for the physical/rated limit:

- **Rated bounds**: static `metric_config_bounds` are written via
  `insert_rated_bounds()` (v0.15 compat: `api_v0_17_wrapper.rs:126-130`) into a field
  **distinct** from `system_inclusion_bounds`. Rated bounds come from component config
  and are never touched by `add_component_bounds`, so they never echo our cap.
- (Secondary) device-delivered `AcMaxActivePowerChargeW` becomes the `AC_POWER_ACTIVE`
  inclusion upper *before* the operator-cap intersection.

The SDK drops both:

- `EVChargerData.from_samples` (`_old_component_data.py`) parses only `sample.bounds`
  (the intersected/echoed `system_inclusion_bounds`); it ignores `rated_bounds`.
- `EVCSystemBoundsTracker` (`ev_charger_pool/_system_bounds_tracker.py`) aggregates only
  those same `active_power_inclusion_*` fields into `power_status`.

### App-side stopgap currently in place

Commit `a59a167` added a configured `ev_max_power` (`ev_max_power_w`, default 1 MW) and
drives `_ev_cap_ceiling` / `ev_nominal_room` from it instead of the echoed bound. This
is a pragmatic fallback (and the only option on a HiL that reports a sentinel rated
bound), but the *correct* fix is to surface the rated bound from the SDK and use it
when available.

### Why this is a general SDK fix (not a HiL workaround)

The defect is in the SDK and affects **every** deployment: `EVChargerData` /
`EVCSystemBoundsTracker` expose only `min(physical_max, our_applied_cap)`, so any
consumer wanting the EV pool's physical capacity gets a value contaminated by the
controller's own command. In production systems with sane rated bounds the symptom is
subtle (an unnecessarily collapsing/limited ceiling); on a HiL reporting a sentinel it
was dramatic (0 W collapse, no EV charging). The rated-bounds channel that fixes it is
already produced by nitrogend and has existed for years — the SDK simply drops it.

The HiL sentinel is **not** a go/no-go gate for this work; it is just one test case. Where
a deployment genuinely reports no usable rated bound, the consumer falls back (for us,
to `ev_max_power`) — ordinary defensive design, not the justification for the effort.

### Goal of this sprint

Make the SDK parse and expose the EV charger **rated active-power bounds**, so consumers
can use the true physical maximum (uncontaminated by the active control cap), with a
local fallback when no rated bound is available.

### Key references

| What | Where |
|------|-------|
| SDK repo | `/home/matthias/workspace/frequenz_python/frequenz-sdk-python/` |
| EV system bounds tracker | `src/frequenz/sdk/timeseries/ev_charger_pool/_system_bounds_tracker.py` |
| EV charger data parsing | `src/frequenz/sdk/microgrid/_old_component_data.py` (`EVChargerData`) |
| Base bounds types | `src/frequenz/sdk/timeseries/_base_types.py` (`Bounds`, `SystemBounds`) |
| nitrogend rated bounds (compat) | `nitrogend/src/proto/compatibility_v0_15/component_data/api_v0_17_wrapper.rs` |
| nitrogend echo/intersect | `nitrogend/src/proto/common_impl/component_telemetry_sample.rs`, `metric_value.rs` (`add_bounds`) |
| App consumer | `se-app-truck-charging/src/app/ev_charging_main.py` (`_ev_cap_ceiling`) |

---

## Sprint Backlog

### Task 1: Confirm the wire-level rated-bounds field (0.5h)

Verify what the SDK actually receives over gRPC for an EV charger's rated bounds, so we
parse the right field.

- Inspect the protobuf message the SDK consumes (`frequenz.api.common` / microgrid
  component data) for a rated-bounds / config-bounds field on metric samples.
- Capture a live sample from the running service (`http://[::1]:8800`) for an EV charger
  and confirm whether `AC_ACTIVE_POWER` carries a rated bound separate from
  `system_inclusion_bounds`, and whether it is a real value or the ±99.99 GW sentinel.

**Acceptance:** Documented field name + a captured example sample showing rated vs.
system bounds for an EV charger.

---

### Task 2: Parse rated bounds in `EVChargerData` (1h)

- Add `active_power_rated_lower_bound` / `active_power_rated_upper_bound` (watts) fields
  to `EVChargerData` in `_old_component_data.py`.
- Populate them in `from_samples` from the `AC_ACTIVE_POWER` sample's rated-bounds field
  (the non-echoed channel), independent of `system_inclusion_bounds`.
- Document the fields: meaning, sign convention, and that they are *not* affected by
  `add_component_bounds`.
- Normalise sentinel/absent rated bounds (e.g. ±99.99 GW) to "unknown" so downstream can
  fall back deterministically; define and document the sentinel threshold.

**Acceptance:** Unit tests: (a) a sample with distinct rated and system bounds parses
both correctly and independently; (b) a sentinel rated bound is normalised to unknown.

---

### Task 3: Aggregate rated bounds in `EVCSystemBoundsTracker` (1h)

- Extend the aggregation in `_system_bounds_tracker.py` to compute an aggregate rated
  bound across working chargers (upper = sum of per-charger rated upper bounds, matching
  the existing inclusion-upper semantics).
- Decide how to surface it: extend `SystemBounds` with a `rated_bounds: Bounds | None`
  field (preferred — keeps `power_status` the single channel) or add a parallel
  receiver. Update `_base_types.py` accordingly.
- Preserve backward compatibility: existing `inclusion_bounds` / `exclusion_bounds`
  semantics unchanged; `rated_bounds` is additive and `None` when unavailable.

**Acceptance:** Unit test on the tracker asserts the aggregate `rated_bounds` is emitted
and equals the sum of per-charger rated uppers, while `inclusion_bounds` still reflects
the (capped) system bounds.

---

### Task 4: Consume rated bounds in the app, with fallback (1h)

In `se-app-truck-charging`:

- Read the aggregate EV rated upper bound from `ev_charger_pool.power_status`
  (`rated_bounds`).
- Treat the ±99.99 GW sentinel (and absent bounds) as "unknown" — the SDK should already
  surface these as `None` (Task 2/3); the consumer simply falls back when it is `None`.
- In `_ev_cap_ceiling` / `ev_nominal_room`, prefer the rated bound when present; fall
  back to the configured `ev_max_power` when it is `None` (sentinel/unavailable).
- Keep `ev_max_power` config as the documented fallback; update its docstring to note it
  is only used when the API provides no usable rated bound.
- Update the affected unit tests (`tests/test_ev_charging_main.py`) to cover both the
  rated-bound-present and fallback paths.

**Acceptance:** Tests cover both paths; the EV cap ceiling is driven by the rated bound
when available and never by the echoed `inclusion` bound.

---

### Task 5: Validate live on HiL (0.5h)

- Deploy against the running microgrid service and confirm:
  - If the HiL provides a real rated bound: the ceiling tracks it and the grid converges
    to target without the cap collapsing.
  - If the HiL reports the sentinel: the app cleanly falls back to `ev_max_power` and
    behaves identically to the current stopgap.

**Acceptance:** Live run shows EV cap holding, EV charging active, grid converging to the
25 kW target — no collapse to 0 — under both rated-bound and fallback conditions.

---

## Open Questions

1. Where should sentinel/absent rated bounds be normalised to `None` — in `EVChargerData`
   (Task 2) or in the aggregate tracker (Task 3)? (Recommendation: normalise per-charger
   in Task 2 so the aggregate in Task 3 is clean.) Define the sentinel threshold there.
2. Preferred surface for rated bounds: extend `SystemBounds` vs. a new receiver. —
   resolve in Task 3 (recommendation: extend `SystemBounds`).
3. Should the same rated-bounds plumbing be added for batteries/inverters for symmetry,
   or kept EV-only for this sprint? (Recommend EV-only here; track the rest separately.)

## Out of Scope

- Changing nitrogend (the rated-bounds data is already exposed correctly).
- Reverting the `ev_max_power` config; it remains the documented fallback.
