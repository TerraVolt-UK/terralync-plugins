"""
Engineering Mode Plugin — keep-alive entry point.

This plugin's presence (installed + enabled) gates access to the
engineering API endpoints.  The actual command execution runs inside the
main dashboard process; this subprocess exists only so the plugin
manager can track its lifecycle.
"""

import asyncio
import logging
import os
import signal
import sys

# Optionally use the SDK if available, but we only need the keep-alive loop
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from terralync_plugin_sdk import Plugin
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [engineering-mode] %(levelname)s %(message)s",
)
logger = logging.getLogger("engineering-mode")

_running = True


def _handle_signal(sig, _frame):
    global _running
    logger.info("Received signal %s — shutting down", sig)
    _running = False


async def main():
    global _running
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Engineering Mode plugin started")
    logger.info("  Plugin dir : %s", os.environ.get("TERRALYNC_PLUGIN_DIR", "?"))
    logger.info("  API base   : %s", os.environ.get("TERRALYNC_PLUGIN_API", "?"))

    # Just stay alive so the plugin manager sees us as RUNNING
    while _running:
        await asyncio.sleep(5)

    logger.info("Engineering Mode plugin stopped")


if __name__ == "__main__":
    asyncio.run(main())
