# TerraLync Plugins Registry

Official plugin registry for TerraLync - a comprehensive solar energy management system.

## Overview

This repository contains official TerraLync plugins that extend the functionality of the TerraLync dashboard. Each plugin is self-contained in its own subdirectory.

## Available Plugins

### In-Home Display
- **Location**: `in-home-display/`
- **Description**: A beautiful, modern in-home display showing real-time energy flow with animated visualizations for solar, house load, battery, and grid status.
- **Features**: Real-time data, animated visualizations, responsive design, energy flow tracking
- **Permissions**: `read_inverter_data`

### Engineering Mode
- **Location**: `engineering-mode/`
- **Description**: Advanced engineering tools for inverter diagnostics and configuration.
- **Features**: Register read/write, advanced diagnostics, configuration tools
- **Permissions**: `read_inverter_data`, `write_inverter`

## Installation

### Adding as a Plugin Source

1. Open your TerraLync dashboard
2. Navigate to Settings → Plugins
3. Click "Add Source" and enter this repository URL
4. Click "Browse" to see available plugins
5. Click "Install" on any plugin you want to add

### Manual Installation

1. Clone this repository
2. Copy the desired plugin folder to your TerraLync plugins directory:
   ```bash
   cp -r terralync-plugins/in-home-display /path/to/terralync/data/plugins/
   ```
3. Restart TerraLync or trigger a plugin scan
4. Approve permissions in the Plugins tab

## Plugin Development

To add a new plugin to this registry:

1. Create a new subdirectory following the naming convention
2. Include the required files:
   - `plugin.json` - Plugin manifest
   - `main.py` - Entry point
   - `frontend/` - Optional UI files
   - `README.md` - Plugin documentation
3. Follow the plugin development guidelines in the TerraLync documentation
4. Submit a pull request for review

## Requirements

- TerraLync 1.0.0 or higher
- Each plugin may have additional requirements - see individual plugin READMEs

## License

This registry is licensed under MIT License. Individual plugins may have their own licenses - see each plugin's LICENSE file for details.

## Support

For issues with specific plugins, please open an issue in this repository and tag with the plugin name.

For general TerraLync support, visit the main TerraLync repository.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request with new plugins or improvements to existing ones.
