# Frequenz Python SDK Release Notes

## Summary

<!-- Here goes a general summary of what this release is about -->

## Upgrading

* The default fallback power for EV chargers (used when all proposals are cleared) changed from `DefaultPower.MAX` to `DefaultPower.ZERO`. Calling `EVChargerPool.propose_power(None)` (or letting all proposals expire) now drives the chargers to 0 W instead of the system upper bound. Callers that relied on the previous behaviour must explicitly propose the maximum power.

## New Features

* A new `tick_delay` option was added to `ResamplerConfig` and `ResamplerConfig2` to delay resampling execution after each timer tick. The delay was designed to postpone processing while keeping window boundaries aligned to the original tick times, which can be used for cascaded resampling pipelines. This option is experimental and may be changed or deprecated in a future release.

## Bug Fixes

* Expiring power-manager proposals now trigger an immediate target recalculation and distribution request. When the last EV charger proposal expires, the SDK sends the default 0 W target instead of leaving the last non-zero cap active in the distributor/API.
* EV charger operator bounds now use a request-aware validity derived from the active proposal lifetime, sent to the microgrid API as a whole-second `timedelta` clamped to the supported 1 to 900 second range (down to a 1 s floor). The EV charger manager renews the currently valid allocation, including zero resets, and no longer renews stale non-zero caps after a reset.
* Clearing the last EV charger proposal no longer makes the pool jump to the maximum system power; it now releases the chargers to 0 W, matching the documented "release control" semantics of `propose_power(None)`.
