# Frequenz Python SDK Release Notes

## Summary

<!-- Here goes a general summary of what this release is about -->

## Upgrading

* The default fallback power for EV chargers (used when all proposals are cleared) changed from `DefaultPower.MAX` to `DefaultPower.ZERO`. Calling `EVChargerPool.propose_power(None)` (or letting all proposals expire) now drives the chargers to 0 W instead of the system upper bound. Callers that relied on the previous behaviour must explicitly propose the maximum power.

## New Features

* A new `tick_delay` option was added to `ResamplerConfig` and `ResamplerConfig2` to delay resampling execution after each timer tick. The delay was designed to postpone processing while keeping window boundaries aligned to the original tick times, which can be used for cascaded resampling pipelines. This option is experimental and may be changed or deprecated in a future release.

## Bug Fixes

* Clearing the last EV charger proposal no longer makes the pool jump to the maximum system power; it now releases the chargers to 0 W, matching the documented "release control" semantics of `propose_power(None)`.
