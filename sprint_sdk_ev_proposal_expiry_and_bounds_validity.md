# Sprint Plan: Complete Proposal Expiry Semantics for EV Charger Bounds

## Context

Commit `dcabc282ed39bb0311c6eb685eb87f092921b98e` introduced configurable
power-manager proposal lifetimes. Callers can now set a pool-level or per-call
`max_proposal_age`, and the Matryoshka algorithms use that value when dropping stale
proposals.

Live truck-charging testing showed this is not enough for EV chargers:

1. The app commanded about **25 kW per charger**.
2. nitrogend telemetry showed `AC_ACTIVE_POWER` bounds of `[0, 25000]` per charger.
3. After stopping the app and waiting, those bounds eventually returned to `[0, 350000]`.
4. Therefore latest nitrogend and the 350 kW config are working; the 25 kW bound was an
   SDK/app operator cap still active in nitrogend.

The SDK currently writes EV charger power by adding active-power inclusion bounds, not
by direct setpoints:

```python
api.add_component_bounds(
    component_id,
    Metric.AC_POWER_ACTIVE,
    [Bounds(lower=0.0, upper=allocation_w)],
    validity=_BOUNDS_VALIDITY,
)
```

In `EVChargerManager`, this validity is still hard-coded to one minute:

```python
_BOUNDS_VALIDITY = Validity.ONE_MINUTE
_BOUNDS_RENEWAL_INTERVAL = timedelta(seconds=_BOUNDS_VALIDITY.value / 2)
```

So a short proposal lifetime does **not** imply a short-lived external EV charger cap.
If the process restarts while an old one-minute bound is still active, the restarted SDK
can initially see only the echoed/intersected `[0, 25000]` telemetry bound. If rated
bounds are unavailable or not used in that path, the in-memory high-water anti-ratchet
state starts from 25 kW and can keep renewing that ceiling.

## Findings to Fix

### Finding 1: Proposal expiry only mutates internal algorithm state

`PowerManagingActor` calls:

```python
self._algorithm.drop_old_proposals(asyncio.get_event_loop().time())
```

on a 1 s timer, but it does not recalculate target power or send an updated distribution
request for component buckets whose proposals expired.

The algorithms also remove `_target_power` when the last proposal in a bucket is
dropped. That makes it hard for a later recalculation to notice that a previous non-zero
target must be reset to the default power.

Impact: a stale proposal can disappear from the power manager while the distributor / API
still holds the last target.

### Finding 2: EV charger API bounds ignore proposal lifetime

EV charger allocations are sent as `add_component_bounds()` calls with
`Validity.ONE_MINUTE`, regardless of the proposal's `max_proposal_age`.

Impact: even with `max_proposal_age=1s`, nitrogend can keep the last EV cap for up to 60
s after the last proposal or app crash.

### Finding 3: EV charger renewal keeps the last allocation alive independently

`EVChargerManager` remembers successful allocations in `_last_sent_allocations` and
renews them every 30 s. If proposal expiry does not send a zero/default target, the old
allocation remains in `_last_sent_allocations` and continues to be renewed.

Impact: stale EV charger caps can be actively kept alive by the SDK.

### Finding 4: The microgrid client validity API is coarse (but nitrogend is not)

The current Python client exposes:

```text
Validity.FIVE_SECONDS = 5
Validity.ONE_MINUTE = 60
Validity.FIVE_MINUTES = 300
Validity.FIFTEEN_MINUTES = 900
```

and `MicrogridApiClient.add_component_bounds(..., validity: Validity | None = None)`.

There is no 1 s validity enum, but this is a **purely SDK-side limitation**. The deployed
nitrogend (`/home/matthias/workspace/frequenz/nitrogend`,
`src/proto/common_impl/bounds.rs`) accepts `request_lifetime` as an arbitrary integer
number of seconds in the range **[1, 900]**:

```rust
const MIN_DURATION: u64 = 1;          // 0 -> InvalidArgument
const MAX_DURATION: u64 = 60 * 15;    // >900 -> InvalidArgument
```

When `request_lifetime` is omitted, nitrogend falls back to its configured default
(`default_fallback_bounds_duration_ms = 5000`, i.e. 5 s). The installed Python client
sends `request_lifetime` directly (the alpha proto field is `optional uint64`), so true
1 s validity is achievable today by passing the seconds value; the only granularity limit
is whole seconds (no sub-second). The coarse `ComponentBoundsValidityDuration` enum exists
only in the unused `v1` (non-alpha) proto and does not constrain the deployed path.

## Sprint Goal

Make proposal lifetimes have end-to-end effect for EV charger control:

- when a proposal expires, the power manager must recalculate and distribute the new
  target immediately;
- when the last proposal for EV chargers expires, EV charger caps must be reset to the
  default safe target, normally zero;
- EV charger `add_component_bounds()` validity must be configurable and must not remain
  hard-coded to one minute for short-lived proposals;
- renewal must only renew the currently valid target and must stop renewing stale
  allocations.

## Non-Goals

- Do not change nitrogend semantics. nitrogend correctly intersects operator bounds into
  public telemetry and removes them after their validity duration.
- Do not remove EV charger control via bounds. EV chargers must continue to be controlled
  with active-power inclusion bounds because nitrogend relays the effective upper bound
  to the asset.
- Do not rely on SDK process shutdown hooks for safety. Crashes/restarts must be handled
  by short external validity, not only by graceful cleanup.

---

## Implementation Plan

### Task 1: Make stale-proposal eviction report changed buckets

**Files:**

- `src/frequenz/sdk/microgrid/_power_managing/_base_classes.py`
- `src/frequenz/sdk/microgrid/_power_managing/_matryoshka.py`
- `src/frequenz/sdk/microgrid/_power_managing/_shifting_matryoshka.py`

Change `BaseAlgorithm.drop_old_proposals()` to return the component buckets that changed:

```python
def drop_old_proposals(self, loop_time: float) -> set[frozenset[ComponentId]]:
    """Drop old proposals and return buckets whose proposal set changed."""
```

Implementation details:

- Track a bucket as changed if at least one proposal was removed.
- If the bucket becomes empty, remove it from `_component_buckets`.
- **Do not eagerly remove `_target_power` in `drop_old_proposals()`.** Preserve it until
  the next recalculation so `calculate_target_power(..., proposal=None, ...)` can detect
  that a previously active target must be reset to the algorithm default.
- Keep per-proposal `max_age` behavior from `dcabc282`:

  ```python
  max_proposal_age_sec = (
      proposal.max_age
      if proposal.max_age is not None
      else self._max_proposal_age_sec
  )
  ```

Acceptance criteria:

- Unit tests show `drop_old_proposals()` returns changed buckets.
- Unit tests show `_target_power` is retained after eviction until recalculation.
- Existing proposal-lifetime tests continue to pass after adapting expectations.

### Task 2: Recalculate and distribute targets after expiry

**File:**

- `src/frequenz/sdk/microgrid/_power_managing/_power_managing_actor.py`

Update the drop timer branch:

```python
elif selected_from(selected, drop_old_proposals_timer):
    changed_component_ids = self._algorithm.drop_old_proposals(
        asyncio.get_event_loop().time()
    )
    for component_ids in changed_component_ids:
        if component_ids in self._system_bounds:
            await self._send_updated_target_power(component_ids, None)
            await self._send_reports(component_ids)
```

Expected behavior:

- If a lower-priority proposal expires but others remain, recalculate the target from
  remaining proposals and send it to the distributor.
- If the last proposal expires and a target was previously set, send the component
  category's default target. For EV chargers this should be `DefaultPower.ZERO`.
- If no target was ever sent, do not emit a spurious distribution request.

Acceptance criteria:

- Actor-level test with a short-lived EV charger proposal receives a second distributor
  request resetting power to zero after expiry.
- Actor-level test with two proposals of different ages receives a recalculated target
  when the short-lived proposal expires.
- Reports update after expiry.

### Task 3: Carry target expiry / requested external validity to power distribution

**Files:**

- `src/frequenz/sdk/microgrid/_power_distributing/request.py`
- `src/frequenz/sdk/microgrid/_power_managing/_power_managing_actor.py`
- `src/frequenz/sdk/microgrid/_power_managing/_base_classes.py`
- `src/frequenz/sdk/microgrid/_power_managing/_matryoshka.py`
- `src/frequenz/sdk/microgrid/_power_managing/_shifting_matryoshka.py`

Add enough information to distribution requests so component managers can choose an API
validity aligned with the proposals that currently determine the target.

Recommended design:

1. Add an optional field to `_power_distributing.Request`:

   ```python
   bounds_validity: timedelta | None = None
   """Maximum validity for external operator bounds produced for this request."""
   ```

2. Add an algorithm helper that returns the next expiry deadline / remaining lifetime for
   a component bucket:

   ```python
   def next_proposal_expiry(self, component_ids: frozenset[ComponentId], loop_time: float) -> float | None:
       """Return seconds until the next proposal in the bucket expires, or None."""
   ```

3. In `_send_updated_target_power()`, compute a distribution validity from the currently
   active proposals:

   - If proposals have finite expiry, use the remaining time until the next proposal
     expiry, plus a small grace margin if needed for timer jitter.
   - Clamp to the minimum validity supported by the API client until arbitrary durations
     are available (see Task 4).
   - For default/reset requests caused by expiry, use the shortest supported validity.

Alternative design:

- Make `calculate_target_power()` return a richer result:

  ```python
  TargetCalculation(target_power: Power | None, bounds_validity: timedelta | None)
  ```

  This is cleaner long-term but touches more code.

Acceptance criteria:

- Distribution requests caused by finite-age proposals carry a non-`None` validity.
- Existing battery/PV behavior remains unchanged if component managers ignore the field.

### Task 4: Make EV charger API bounds validity configurable and short-lived

**File:**

- `src/frequenz/sdk/microgrid/_power_distributing/_component_managers/_ev_charger_manager/_ev_charger_manager.py`

Replace the global hard-coded `_BOUNDS_VALIDITY = Validity.ONE_MINUTE` with a request-aware
validity selection.

Short-term implementation with the current client API:

```python
_DEFAULT_BOUNDS_VALIDITY = Validity.ONE_MINUTE
_MIN_BOUNDS_VALIDITY = Validity.FIVE_SECONDS

def _validity_for_request(request: Request) -> Validity:
    if request.bounds_validity is None:
        return _DEFAULT_BOUNDS_VALIDITY
    if request.bounds_validity <= timedelta(seconds=Validity.FIVE_SECONDS.value):
        return Validity.FIVE_SECONDS
    if request.bounds_validity <= timedelta(seconds=Validity.ONE_MINUTE.value):
        return Validity.ONE_MINUTE
    ...
```

Renewal changes:

- Track validity per last-sent allocation, not as one global constant:

  ```python
  @dataclass
  class LastSentAllocation:
      power: Power
      validity: Validity
      sent_at: datetime
  ```

- Renew each allocation before its own validity expires.
- If all allocations are zero, either:
  - renew zero while the SDK is alive and the zero/default target is still active, or
  - intentionally do not renew zero after one short validity interval, if fail-safe-open
    behavior is desired. For truck charging we probably want fail-safe-zero while the app
    is alive, and external expiry after crash.

- On a reset/default request, overwrite `_last_sent_allocations` with zero allocations so
  stale non-zero caps stop being renewed.

Acceptance criteria:

- A proposal with `max_proposal_age=1s` results in EV charger API bounds with the shortest
  supported validity, currently 5 s, not 60 s.
- Repeated proposals keep the cap alive.
- If proposals stop, power manager sends zero and the EV manager renews zero, not the old
  non-zero cap.
- If the SDK process crashes, the last non-zero external cap expires after at most the
  selected API validity, not one minute.

### Task 5: Decide whether to extend the microgrid client for arbitrary validity durations

**Files / repos:**

- SDK: `src/frequenz/sdk/...`
- Client: `/home/matthias/workspace/frequenz/api_clients_python/frequenz-client-microgrid-python`

The SDK can only request enum validities today, but this is a pure SDK-side wrapper limit:
the deployed nitrogend already accepts arbitrary `request_lifetime` in `[1, 900]` seconds
(see Finding 4 and Open Question 2). No server change is needed. If the application
requires true 1 s external bounds, extend the client API:

```python
async def add_component_bounds(
    ...,
    validity: Validity | timedelta | None = None,
) -> datetime | None:
    ...
```

Map `timedelta` to the underlying protobuf `validity_duration` if supported.

Map `timedelta` to the protobuf `request_lifetime` field as `int(td.total_seconds())`,
rejecting/clamping values outside nitrogend's accepted `[1, 900]` second range. Note the
field is whole-second granularity, so sub-second validity is not possible.

Acceptance criteria:

- Client unit tests prove a `timedelta(seconds=1)` validity is serialized to
  `request_lifetime=1` correctly.
- Client rejects/clamps `timedelta(0)` and `timedelta > 900s` to stay within nitrogend's
  accepted range.
- SDK EV charger tests can request 1 s external validity instead of clamping to 5 s.
- If the client change is deferred, the SDK clamps to `Validity.FIVE_SECONDS` (a real 5 s
  nitrogend bound, not just a fallback); document this 5 s interim floor in the SDK API
  docs and in the truck-charging app config.

### Task 6: Add end-to-end regression tests for EV charger stale caps

**Files:**

- `tests/actor/_power_managing/test_proposal_lifetime.py`
- New tests under `tests/microgrid/power_distributing/_component_managers/` or similar

Test scenarios:

1. **Expiry sends reset**
   - Submit EV charger proposal with short lifetime.
   - Verify first distributor request is non-zero.
   - Advance/drop proposals.
   - Verify next distributor request is zero.

2. **EV manager stops renewing stale non-zero allocation**
   - Seed an EV charger manager with last sent allocation 25 kW.
   - Send target zero.
   - Verify `_last_sent_allocations` becomes zero and renewal sends zero, not 25 kW.

3. **Short API validity selection**
   - Send request with `bounds_validity=1s` or `5s`.
   - Verify `api.add_component_bounds()` is called with the shortest available validity.

4. **Restart window regression**
   - Simulate active old `[0, 25000]` operator bounds and physical rated bound 350 kW.
   - Verify after proposal expiry/reset the SDK does not keep renewing 25 kW.
   - If rated bounds are available, verify allocation uses the rated 350 kW ceiling rather
     than the echoed operator bound.

### Task 7: Documentation and release notes

**Files:**

- EV charger pool docs / docstrings
- `RELEASE_NOTES.md`

Document:

- `max_proposal_age` controls the lifetime of proposals in the power manager.
- For EV chargers, the SDK also constrains external operator-bound validity so stale caps
  do not live for the old fixed one-minute interval.
- Current minimum external bound validity is 5 s unless the microgrid client is extended
  to accept arbitrary `timedelta` validity.
- Short proposal lifetimes are not a replacement for a safe default target; when proposals
  expire, EV chargers are reset to zero by default.

---

## Suggested Commit Split

1. `fix(power-manager): recalculate targets after proposal expiry`
   - Change `drop_old_proposals()` to return changed buckets.
   - Preserve `_target_power` until recalculation.
   - Actor sends updated target/report after expiry.
   - Add algorithm and actor tests.

2. `feat(power-distributor): carry external bounds validity in requests`
   - Add `bounds_validity` to `_power_distributing.Request`.
   - Compute validity from active proposal expiry in `PowerManagingActor`.
   - Add tests that requests carry the expected validity.

3. `fix(ev-charger): align operator-bound validity with proposal lifetime`
   - Replace hard-coded one-minute EV bounds validity with request-aware validity.
   - Track renewal per allocation validity.
   - Stop renewing stale non-zero caps after reset.
   - Add EV manager tests.

4. Optional client commit, if required:
   - `feat(client): allow timedelta validity for component bounds`
   - Enable true 1 s bounds instead of clamping to `Validity.FIVE_SECONDS`.

5. `docs(ev-charger): document proposal and operator-bound expiry semantics`
   - Update docstrings and release notes.

## Validation Checklist

Run at least:

```bash
pytest tests/actor/_power_managing/test_proposal_lifetime.py
pytest tests/microgrid/power_distributing
pytest tests/microgrid/test_ev_charger_data.py
```

Live validation on the truck-charging HIL:

1. Start app with EV proposal lifetime configured short.
2. Confirm EV charger telemetry bounds become the commanded caps while app is running.
3. Stop app without cleanup.
4. Confirm bounds return to `[0, 350000]` after the selected external validity, not after
   one minute.
5. Restart app while old caps are still active and verify it does not permanently seed the
   EV ceiling from `[0, 25000]`.
6. Verify repeated proposals still keep intentional caps alive while the app is healthy.

## Open Questions (Resolved)

1. **Should EV chargers renew zero bounds indefinitely while the SDK is alive, or should
   zero bounds also expire after a short interval?**

   **Resolved: fail-safe-zero while alive, external expiry after crash.** When an
   `AC_POWER_ACTIVE` inclusion bound expires, nitrogend *removes* the cap and the charger
   is uncapped to its physical/rated ceiling. Therefore, while the SDK process is alive
   and the active target is zero (or any non-default cap), it MUST keep renewing that
   bound before its validity lapses. Renewal must stop only when the process dies, at
   which point the last bound expires after its (now short) validity. This is the safe
   truck-charging behavior: caps are held while we are in control, and they fall away
   safely after a crash rather than being silently dropped while we are still running.

2. **Is a 5 s minimum external stale-cap window acceptable, or do we need to extend the
   client to support true 1 s validity?**

   **Resolved: the deployed nitrogend already supports 1 s validity** (accepts
   `request_lifetime` in `[1, 900]` seconds; see Finding 4). The 5 s figure is only
   nitrogend's *fallback default* (used when no lifetime is sent) and the smallest member
   of the SDK's `Validity` enum. There is no need to change nitrogend. To get sub-minute
   external validity:
   - Short term: there is no `FIVE_SECONDS`-and-below problem — `Validity.FIVE_SECONDS`
     already maps to a real 5 s nitrogend bound, so Task 4's enum-based clamp to 5 s is a
     valid, safe first step.
   - Preferred: implement Task 5 by extending the SDK client's `add_component_bounds` to
     accept `validity: Validity | timedelta | None`, mapping a `timedelta` to
     `int(td.total_seconds())` clamped to `[1, 900]`. This unlocks true 1 s external
     validity end-to-end with no server change. Sub-second validity is **not** possible
     (whole-second granularity), so the practical floor is 1 s.

3. **Should `bounds_validity` be a general `Request` field, or should the EV charger
   manager read proposal expiry through a separate control channel?**

   **Resolved: use the general `Request` field.** It is simpler and keeps component
   managers decoupled from power-manager internals. Battery/PV managers ignore the field;
   the EV charger manager consumes it.
