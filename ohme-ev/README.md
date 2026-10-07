# Ohme EV Charger

Live telemetry from Ohme EV chargers (Home Pro, Home, Go, ePod — any
model on the Ohme cloud API) for TerraLync Lite.

## What it does

- **Discharge hold** — while the charger reports real draw (or a
  claimed charge window is open and the car is plugged in), the plugin
  declares a `hold_discharge` intent so the house battery never feeds
  the car. Works through the platform's intent arbiter, so VPP exports
  (Axle) and planner charging don't fight it — an export event
  declared during a charge hold fires when the hold lifts.
- **Planner feed** — planned charge windows publish as cheap import
  slots (`tariff.rates`) so the Energy Planner can co-charge the
  battery alongside the car, gated by a configurable mains-fuse check.
- **Load hygiene** — delivered EV energy is integrated from power
  samples and published as `load.adjust` corrections, so charge
  sessions don't pollute the household load forecast.
- **Approval flow** — chargers that require approval for
  out-of-schedule charging surface an amber banner with an Approve
  button.

## Sign-in

Uses the same cloud API as the Ohme app (Firebase auth, email +
password). The password is stored masked on-device only; the session
JWT lives in a private `_session.json` that is never served over the
plugin data route.

## Notes

- `ev_inside_ct` (default on) controls whether discharge-hold and load
  corrections apply — turn it off only if the charger is wired
  grid-side of the inverter's CT clamp, where the battery physically
  cannot feed the car.
- The API is unofficial (though stable for years); the plugin treats
  claimed charge windows as hints and actual `power.watt` as truth.
