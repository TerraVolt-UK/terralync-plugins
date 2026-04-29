# Axle Energy VPP Plugin for TerraLync

This plugin integrates TerraLync with Axle Energy's Virtual Power Plant (VPP) program using **Quick Settings** for immediate, non-disruptive battery export during grid events.

## Key Design: Quick Settings Only

Unlike scheduler-based approaches, this plugin:
- **Does NOT modify your schedules** - your existing schedule remains untouched
- **Uses Quick Settings** - immediate response via `discharge_now` quick action
- **Auto-pauses scheduler** - TerraLync automatically pauses scheduler during quick actions
- **Auto-resumes** - Inverters resume normal operation after event ends
- **Zero schedule conflicts** - No backup/restore needed

## Features

- **Automatic Event Detection**: Polls Axle Energy API for grid events
- **Adaptive Polling**: Fast polling (1 min) near events, normal (10 min) otherwise
- **Multi-Inverter Support**: Triggers exports on ALL connected inverters simultaneously
- **Quick Settings Integration**: Uses TerraLync's `discharge_now` and `resume` actions
- **Automatic Recovery**: Auto-resume after event duration expires
- **Event History**: Tracks all VPP events with web UI
- **API Backoff**: Exponential backoff on rate limits

## Installation

1. Copy the `axle-energy` folder to your TerraLync plugins directory:
   ```
   /opt/terralync/data/plugins/axle-energy/
   ```

2. Restart TerraLync or reload plugins via the dashboard

3. Enable the plugin in TerraLync dashboard → Plugins

4. Configure your Axle Energy API key in the plugin settings

## Configuration

Access settings via TerraLync Dashboard → Plugins → Axle Energy VPP → Settings.

### Required Settings

- **Axle API Key**: Your Axle Energy authentication token

### Optional Settings

- **Enable Axle Energy VPP**: Master on/off switch
- **Normal Polling Interval**: Minutes between checks when no event (default: 10)
- **Fast Polling Interval**: Minutes between checks near events (default: 1)
- **Fast Poll Window**: Hours before event to start fast polling (default: 2)
- **Export Power Level**: Max discharge power 0-50 (default: 50 = max)
- **Discharge Target SOC**: Minimum battery level during events (default: 4%)

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│  POLLING PHASE (every X minutes)                              │
│  • Normal: 10 min when no event                              │
│  • Fast: 1 min when event within 2 hours or active          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  EVENT DETECTED                                              │
│  • Calculate event duration                                  │
│  • Compute auto_resume_minutes = duration + 15min buffer      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  TRIGGER EXPORT (on ALL inverters)                           │
│  POST /api/quick/discharge_now                                │
│  Body: {                                                      │
│    "serial": "CE1234...",                                    │
│    "auto_resume_minutes": 255                                │
│  }                                                           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  QUICK SETTINGS EFFECT                                       │
│  1. Captures current inverter state                         │
│  2. Sets discharge_now configuration                        │
│  3. Pauses scheduler (prevents schedule conflicts)          │
│  4. Schedules auto-resume after X minutes                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  EVENT ENDS (or auto-resume triggers)                        │
│  • Quick Settings auto-resumes all inverters                │
│  • Scheduler unpauses and continues normal operation        │
│  • Your original schedules are never modified               │
└─────────────────────────────────────────────────────────────┘
```

## Event History

View event history by visiting:
```
http://<terralync-ip>:8080/plugins/axle-energy/index.html
```

Shows:
- Current/next upcoming event
- Past events with duration and direction (export/import)
- Plugin status and polling information

## Quick Settings vs Scheduler

| Feature | Quick Settings (This Plugin) | Scheduler Integration |
|--------|------------------------------|----------------------|
| **Schedule Impact** | None - schedules unchanged | Modifies schedule blocks |
| **Response Time** | Immediate | On next scheduler tick (30s) |
| **State Backup** | Automatic via QS | Manual backup/restore needed |
| **Multi-day Events** | Auto-resume calculated | Complex multi-day scheduling |
| **Conflict Risk** | Zero (scheduler paused) | Possible schedule conflicts |
| **Recovery** | Automatic on resume | Manual restore required |

## API Key Security

Your API key is stored in `settings.json` within the plugin directory. Ensure:
- File permissions are restricted (chmod 600)
- Key is not committed to version control (see `.gitignore`)
- Regular key rotation per Axle Energy guidelines

## Troubleshooting

**Plugin not detecting events**
- Verify API key is correct in settings
- Check plugin logs: `/opt/terralync/data/plugins/axle-energy/logs/`
- Confirm network access is allowed (permission in plugin.json)

**Events not triggering exports**
- Ensure inverters are connected and responsive
- Check quick settings are working via dashboard
- Verify export power setting is > 0
- Review logs for API errors

**Scheduler not resuming after event**
- Check quick settings status in dashboard
- Manually trigger "Resume" if needed
- Plugin will attempt resume on next poll if event ended

**Auto-resume didn't fire**
- This can happen if TerraLync restarted during event
- Plugin detects this on next poll and triggers manual resume
- Check logs for "resuming normal operation" messages

## File Structure

```
axle-energy/
├── plugin.json           # Plugin manifest (permissions: write_inverter)
├── settings_schema.json  # UI configuration schema
├── main.py              # Core plugin logic (Quick Settings approach)
├── requirements.txt     # Python dependencies (none required)
├── README.md           # This file
├── frontend/
│   └── index.html     # Event history UI
└── data/              # Runtime data (created automatically)
    ├── events.json    # Event history
    └── plugin_state.json
```

## Technical Details

### Why Quick Settings?

The Quick Settings approach was chosen because:

1. **Immediate Response**: Events often need response within minutes
2. **No Schedule Pollution**: Avoids cluttering schedules with temporary blocks
3. **Built-in Safety**: Quick Settings already has state backup/restore logic
4. **Scheduler Coordination**: QS automatically pauses scheduler during actions
5. **Simpler Code**: No need for complex schedule manipulation

### Event Duration Calculation

```python
auto_resume_minutes = (event_end - event_start).total_seconds() / 60 + 15
```

The 15-minute buffer ensures the inverter doesn't resume mid-event if there are clock sync issues.

### Multi-Inverter Handling

The plugin:
1. Fetches all connected inverters via `/api/inverters`
2. Triggers `discharge_now` on each serial number
3. Each inverter gets its own auto-resume timer
4. On event end, sends `resume` to all inverters

### Rate Limiting

If Axle API returns HTTP 429 (rate limit):
- Polling interval doubles (exponential backoff)
- Max backoff: 60 minutes
- Resets to normal after successful API call

## Permissions Required

From `plugin.json`:
```json
{
  "read_inverter_data": true,   // To get inverter list
  "write_inverter": true,       // For quick settings actions
  "write_scheduler": false,     // Not used (QS pauses scheduler)
  "network_access": true,       // For Axle API
  "filesystem": "own_dir"       // For event history storage
}
```

## License

MIT License - See LICENSE file for details

## Support

For issues or feature requests, contact TerraLync support or submit via GitHub.
