#!/usr/bin/env python3
"""In Home Display Plugin.

A beautiful, modern in-home display showing real-time energy flow
with animated visualizations for solar, house load, battery, and grid status.

This is a frontend-only plugin - the main.py just keeps the plugin process alive.
All the actual functionality is in the HTML/JS frontend which calls the TerraLync API directly.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def main():
    """Plugin entry point - keeps the process alive."""
    logger.info("In Home Display plugin started")
    
    # This is a frontend-only plugin, so we just keep the process alive
    # The actual display is served via /plugins/in-home-display/index.html
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("In Home Display plugin stopping")


if __name__ == "__main__":
    asyncio.run(main())
