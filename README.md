# TerraLync Plugins

Official plugin repository for TerraLync - a comprehensive solar energy management system.

## Overview

This repository contains TerraLync plugins that extend the functionality of the TerraLync dashboard. Each plugin is self-contained in its own subdirectory.

## Available Plugins

### In-Home Display
- **Location**: `in-home-display/`
- **Version**: 1.1.0
- **Description**: A beautiful, modern in-home display showing real-time energy flow with animated visualizations. Features dual display modes (Bento Grid and NEXUS Fluid) selectable via plugin settings.
- **Documentation**: See [in-home-display/README.md](in-home-display/README.md) for details

### Axle Energy VPP
- **Location**: `axle-energy/`
- **Version**: 1.0.0
- **Description**: Virtual Power Plant integration for Axle Energy - automatic battery export during grid events
- **Documentation**: See [axle-energy/README.md](axle-energy/README.md) for details

## Installation

### Manual Installation

1. Clone this repository
2. Copy the desired plugin folder to your TerraLync plugins directory:
   ```bash
   cp -r terralync-plugins/in-home-display /path/to/terralync/data/plugins/
   ```
3. Restart the TerraLync server or trigger a plugin scan via the dashboard
4. Review the plugin's declared permissions in the Plugins tab
5. Enable the plugin via the Enable button in the plugin manager
6. Access the plugin via the View button in the plugin manager

### Plugin Registry Installation

The plugin registry system is implemented in TerraLync. You can install plugins directly from the dashboard:

1. Navigate to the Plugins tab in the TerraLync dashboard
2. Click "Browse Plugins" to view available plugins from the registry
3. Click "Install" on the desired plugin
4. The plugin will be downloaded and installed automatically

The registry is defined in `registry.json` and points to GitHub release archives for each version.

## Plugin Development

To add a new plugin to this repository:

1. Create a new subdirectory following the naming convention (lowercase, hyphens)
2. Include the required files:
   - `plugin.json` - Plugin manifest with metadata, version, and permissions
   - `main.py` - Entry point (can be a simple keep-alive process for frontend-only plugins)
   - `settings_schema.json` - Optional settings schema for configurable plugins
   - `frontend/` - Optional UI files (HTML/JS/CSS)
   - `README.md` - Plugin documentation
   - `requirements.txt` - Optional Python dependencies
3. Update `registry.json` to include the new plugin with version and download URL
4. Follow the plugin development guidelines in the TerraLync documentation
5. Submit a pull request for review

## Release Process

This repository uses git tags for releases. When releasing a plugin:

1. Update the plugin's `plugin.json` with the new version
2. Update `registry.json` with the new version and download URL for **ALL** plugins
3. Commit and push changes to GitHub
4. Create and push a git tag (e.g., `v1.1.0`)
5. The tag automatically generates a `.tar.gz` archive for distribution

For detailed release instructions, see the [Plugin Release Documentation](https://github.com/TerraVolt-UK/terralync/blob/main/docs/PLUGIN_RELEASES.md) in the main TerraLync repository.

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
