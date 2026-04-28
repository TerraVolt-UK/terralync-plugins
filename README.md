# TerraLync Plugins

Official plugin repository for TerraLync - a comprehensive solar energy management system.

## Overview

This repository contains TerraLync plugins that extend the functionality of the TerraLync dashboard. Each plugin is self-contained in its own subdirectory.

## Available Plugins

### In-Home Display
- **Location**: `in-home-display/`
- **Description**: A beautiful, modern in-home display showing real-time energy flow with animated visualizations
- **Documentation**: See [in-home-display/README.md](in-home-display/README.md) for details

## Installation

### Manual Installation

1. Clone this repository
2. Copy the desired plugin folder to your TerraLync plugins directory:
   ```bash
   cp -r terralync-plugins/in-home-display /path/to/terralync/data/plugins/
   ```
3. Restart the TerraLync server or trigger a plugin scan via the dashboard
4. Approve the plugin permissions in the Plugins tab
5. Access the plugin via the View button in the plugin manager

### Plugin Registry (Future)

Once the plugin registry system is implemented, you'll be able to add this repository as a source and install plugins directly from the TerraLync dashboard.

## Plugin Development

To add a new plugin to this repository:

1. Create a new subdirectory following the naming convention
2. Include the required files:
   - `plugin.json` - Plugin manifest with metadata and permissions
   - `main.py` - Entry point
   - `frontend/` - Optional UI files (HTML/JS/CSS)
   - `README.md` - Plugin documentation
3. Follow the plugin development guidelines in the TerraLync documentation
4. Submit a pull request for review

## Requirements

- TerraLync 1.0.0 or higher
- Each plugin may have additional requirements - see individual plugin READMEs

## License

This repository is licensed under MIT License. Individual plugins may have their own licenses - see each plugin's LICENSE file for details.

## Support

For issues with specific plugins, please open an issue in this repository and tag with the plugin name.

For general TerraLync support, visit the main TerraLync repository.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request with new plugins or improvements to existing ones.
