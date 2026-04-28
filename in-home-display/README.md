# In Home Display Plugin

A beautiful, modern in-home display showing real-time energy flow with animated visualizations for solar, house load, battery, and grid status.

## Features

- **Real-time data**: Pulls live data from your TerraLync inverter via the public API
- **Animated visualizations**: Smooth transitions and animated particle effects
- **Responsive design**: Works on desktop, tablet, and mobile devices
- **Beautiful UI**: Modern bento-grid layout with spotlight hover effects
- **Energy flow tracking**: Visual indicators for charging, discharging, importing, and exporting

## Installation

### Method 1: Manual Installation

1. Copy the `in-home-display` folder to your TerraLync plugins directory:
   ```
   /path/to/terralync/data/plugins/in-home-display/
   ```

2. Restart the TerraLync server or trigger a plugin scan via the dashboard

3. Approve the plugin permissions in the Plugins tab of the dashboard

4. Access the display at: `http://your-terralync-host:8080/plugins/in-home-display/index.html`

### Method 2: Dashboard Installation (Future)

Once the plugin registry is implemented, you'll be able to install this plugin directly from the TerraLync dashboard.

## Usage

The display updates every 2.5 seconds with fresh data from your inverter. It shows:

- **Solar Array**: Current solar generation in watts with historical sparkline
- **House Load**: Current household consumption in watts with historical sparkline
- **Storage Cell**: Battery state of charge (%) and current power flow (charging/discharging/idle)
- **External Grid**: Grid power flow (importing/exporting/idle) with animated directional indicator

## Permissions Required

This plugin requires:
- `read_inverter_data`: To fetch real-time inverter telemetry

No write permissions are required - this is a read-only display.

## Configuration

No configuration is required. The plugin automatically connects to the TerraLync API at `/api/inverter/data` to fetch live data.

## Development

To modify the plugin:

1. Edit `frontend/index.html` to change the UI
2. The JavaScript in `index.html` calls the TerraLync API directly
3. No Python backend changes are needed for this frontend-only plugin

## License

MIT License - see LICENSE file for details

## Support

For issues or feature requests, please open an issue on the GitHub repository.
