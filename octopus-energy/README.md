# Octopus Energy Tariff Optimiser

Links your Octopus Energy account to TerraLync for tariff-aware battery scheduling. Automatically optimises charging based on your electricity tariff and participates in Saving Sessions.

## Supported Tariffs

| Tariff Type | Behaviour |
|-------------|-----------|
| **Intelligent Octopus Go** | Creates a fixed charge slot during your cheap rate window (default 23:30–05:30). Polls for planned dispatches from your EV charger. |
| **Agile Octopus** | Fetches next-day half-hourly rates around 16:15, selects the cheapest slots (configurable), and creates charge blocks for tomorrow. Includes retry logic for when rates are published late. |
| **Fixed Off-Peak** (Go/Economy 7) | Configurable fixed-time charge slot applied to all days. |
| **Standard Variable** | No schedule changes; only Saving Session participation active. |

## Features

- **Tariff Auto-Detection**: Discovers your active tariff from your Octopus account
- **Region Auto-Detection**: Determines DNO region from MPAN for accurate Agile pricing
- **Agile Rate Retry**: Exponential backoff covering up to 6 hours for late Agile rate publication
- **Saving Session Participation**: Automatically exports battery power during OctoPlus events
- **Quick Settings Integration**: Uses TerraLync Quick Settings for immediate response to events (pauses scheduler automatically)
- **State Persistence**: Remembers plugin state across restarts

## Configuration

### Required Settings

| Setting | Description |
|---------|-------------|
| **API Key** | Your Octopus Energy API key (found in your online dashboard) |
| **Account Number** | Your Octopus account number (e.g., A-1234ABCD) |

### Optional Settings

| Setting | Default | Description |
|---------|---------|-------------|
| MPAN | Auto-detect | Specific meter point if you have multiple meters |
| Region Code | Auto-detect | DNO region letter (A-P) for Agile tariffs |
| Tariff Mode | Auto | Override auto-detection: Intelligent Go, Agile, Fixed Off-Peak, or Standard Variable |
| Intelligent Go Start | 23:30 | Cheap rate window start time |
| Intelligent Go End | 05:30 | Cheap rate window end time |
| Agile Cheap Slots | 8 | Number of cheapest half-hours to charge (default 8 = 4 hours) |
| Agile Max Price | 15.0 p/kWh | Only charge when price is below this (0 = disabled) |
| Charge Power | 50 | Maximum charge power (0-50, where 50 = maximum) |
| Charge Target SOC | 100% | Target battery level to reach during cheap rates |
| Saving Sessions | Enabled | Participate in OctoPlus saving sessions |
| Export Power | 50 | Discharge power during saving sessions |
| Min SOC | 10% | Stop exporting when battery reaches this level |
| Agile Retry Delay | 60s | Initial retry delay for late Agile rates (doubles each retry) |
| Max Agile Retry Hours | 6 | Maximum total time to keep retrying for late rates |
| Session Poll Interval | 15 min | How often to check for saving sessions |
| Dispatch Poll Interval | 5 min | How often to check Intelligent dispatches |
| Dispatch Mode | Planned+Started | Whether to include planned or only started dispatches |

## How It Works

### Intelligent Octopus Go Mode

1. On plugin start, writes a `charge_slot` block to all days covering your cheap rate window
2. Polls the GraphQL API every 5 minutes for planned dispatches (smart-charge windows)
3. Stores dispatches locally for reference

### Agile Octopus Mode

1. Waits until approximately 16:15 UTC each day
2. Fetches tomorrow's half-hourly rates from the REST API
3. If rates aren't ready, retries with exponential backoff (30s → 1h over 8 attempts)
4. Sorts rates by price, selects cheapest N slots (respecting max price threshold if set)
5. Merges adjacent slots into continuous charge blocks
6. Writes schedule to tomorrow's day in TerraLync scheduler

### Saving Sessions

1. Polls OctoPlus events every 15 minutes
2. When an active session is detected:
   - Triggers `discharge_now` quick action on all inverters
   - Sets auto-resume for the session duration
   - Logs the event
3. When session ends, resumes normal operation

## API Permissions Required

| Permission | Reason |
|------------|--------|
| `write_scheduler` | Write charge_slot blocks based on tariff analysis |
| `write_inverter` | Quick Settings actions for Saving Sessions |
| `network_access` | Connect to Octopus Energy APIs |
| `read_inverter_data` | Optional: could be used for SOC-aware charging decisions |

## Files Created

The plugin stores the following files in its data directory:

- `octopus_plugin_state.json` — Account info, detected tariff, region code
- `agile_rates_cache.json` — Last fetched Agile rates
- `intelligent_dispatches.json` — Planned/started dispatches from Intelligent tariff
- `saving_sessions.json` — Event history log

## Troubleshooting

**"Account discovery failed"**
- Verify your API key and account number are correct
- Check the account number format (e.g., A-1234ABCD)

**"No tariff code available"**
- Ensure you have an active electricity agreement with Octopus
- Check your account has a valid meter point assigned

**"Agile rates not fetched"**
- Octopus sometimes publishes rates late (after 16:30)
- The plugin will retry up to 8 times with increasing delays
- Check the logs for retry status

**"Saving sessions not detected"**
- Ensure you have OctoPlus enabled on your Octopus account
- Verify saving_sessions_enabled is set to true

## Technical Notes

- All Octopus API calls use Python stdlib only (urllib) — no external dependencies
- REST API used for account info and Agile rates
- GraphQL API used for Intelligent dispatches and Saving Sessions
- All blocking IO runs in thread pool to avoid blocking the async event loop
- Plugin follows the TerraLync Plugin SDK patterns for API interaction

## Version History

- **1.0.0** — Initial release with Intelligent Go, Agile, and Saving Session support
