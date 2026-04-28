# TerraLync IHD Plugin

A beautiful, modern in-home display showing real-time energy flow with animated visualizations for solar, house load, battery, and grid status.

![TerraLync IHD](https://img.shields.io/badge/TerraLync-Plugin-blue)
![Version](https://img.shields.io/badge/version-1.0.0-green)
![License](https://img.shields.io/badge/license-MIT-orange)

## Features

- **Real-time data**: Pulls live data from your TerraLync inverter via the public API
- **Animated visualizations**: Smooth transitions and animated particle effects
- **Responsive design**: Works on desktop, tablet, and mobile devices
- **Beautiful UI**: Modern bento-grid layout with spotlight hover effects
- **Energy flow tracking**: Visual indicators for charging, discharging, importing, and exporting

## Screenshots

Coming soon...

## Requirements

- TerraLync 1.0.0 or higher
- Working inverter connection in TerraLync

## Installation

### Method 1: Manual Installation

1. Clone or download this repository
2. Copy the plugin files to your TerraLync plugins directory:
   ```bash
   cp -r terralync-plugins/in-home-display /path/to/terralync/data/plugins/
   ```
3. Restart the TerraLync server or trigger a plugin scan via the dashboard
4. Approve the plugin permissions in the Plugins tab of the dashboard
5. Click the View button to access the display

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

MIT License - see [LICENSE](LICENSE) file for details

## Support

For issues or feature requests, please open an issue on the GitHub repository.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
