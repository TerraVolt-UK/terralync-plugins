#!/usr/bin/env python3
"""Axle Energy VPP Plugin for TerraLync - Quick Settings Edition.

Polls the Axle Energy API for grid events and uses TerraLync Quick Settings
to export battery power during event periods. This approach:

- Does NOT modify scheduler schedules
- Pauses scheduler automatically during events
- Resumes normal operation after events
- Supports multiple inverters

Features:
- Adaptive polling intervals (normal vs fast when events approaching)
- Multi-inverter support via quick settings
- Automatic state backup and restore via quick settings
- Event history logging
- API backoff on rate limits
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from urllib import request, error

logger = logging.getLogger(__name__)

# Axle Energy API configuration
AXLE_API_BASE = "https://api.axle.energy/vpp/home-assistant/event"

# Local file paths (relative to plugin data dir)
EVENTS_FILE = "events.json"
STATE_FILE = "plugin_state.json"


class AxlePlugin:
    """Main plugin class for Axle Energy VPP integration using Quick Settings."""
    
    def __init__(self):
        self.plugin_dir = os.environ.get("TERRALYNC_PLUGIN_DIR", ".")
        self.data_dir = os.environ.get("TERRALYNC_PLUGIN_DATA_DIR", ".")
        self.api_base = os.environ.get("TERRALYNC_PLUGIN_API", "http://127.0.0.1:8080")
        
        self.settings: Dict[str, Any] = {}
        self.current_event: Optional[Dict] = None
        self.event_active = False
        self.last_poll_time: Optional[datetime] = None
        self.next_poll_interval = 600  # Default 10 minutes
        self.running = False
        
        # Track auto-resume tasks per inverter to cancel if needed
        self._pending_resumes: Dict[str, asyncio.Task] = {}
        
        # Ensure data directory exists
        os.makedirs(self.data_dir, exist_ok=True)
        
        self._load_settings()
        self._load_state()
    
    def _load_settings(self):
        """Load plugin settings from settings.json."""
        settings_path = os.path.join(self.plugin_dir, "settings.json")
        try:
            with open(settings_path, "r") as f:
                self.settings = json.load(f)
            logger.info("Settings loaded successfully")
        except Exception as e:
            logger.warning(f"Could not load settings: {e}")
            self.settings = {}
    
    def _load_state(self):
        """Load persistent plugin state."""
        state_path = os.path.join(self.data_dir, STATE_FILE)
        try:
            if os.path.exists(state_path):
                with open(state_path, "r") as f:
                    state = json.load(f)
                    self.current_event = state.get("current_event")
                    self.event_active = state.get("event_active", False)
        except Exception as e:
            logger.warning(f"Could not load state: {e}")
    
    def _save_state(self):
        """Save persistent plugin state."""
        state_path = os.path.join(self.data_dir, STATE_FILE)
        try:
            state = {
                "current_event": self.current_event,
                "event_active": self.event_active,
                "last_saved": datetime.utcnow().isoformat() + "Z"
            }
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
    
    def _api_request(self, method: str, path: str, body: Any = None, timeout: int = 30) -> Any:
        """Make HTTP request to TerraLync API."""
        url = self.api_base.rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API {exc.code} {method} {path}: {error_body}")
    
    async def _async_api_request(self, method: str, path: str, body: Any = None) -> Any:
        """Async wrapper for API requests."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._api_request, method, path, body)
    
    async def _fetch_axle_event(self) -> Optional[Dict]:
        """Fetch current event from Axle Energy API."""
        api_key = self.settings.get("api_key")
        if not api_key:
            logger.warning("No API key configured")
            return None
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json"
        }
        
        req = request.Request(AXLE_API_BASE, headers=headers, method="GET")
        
        try:
            loop = asyncio.get_event_loop()
            def do_request():
                with request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            
            data = await loop.run_in_executor(None, do_request)
            
            # Check if there's an active or upcoming event
            if not data or "start_time" not in data:
                return None
            
            return {
                "start_time": data.get("start_time"),
                "end_time": data.get("end_time"),
                "import_export": data.get("import_export", 0),
                "updated_at": data.get("updated_at", datetime.utcnow().isoformat() + "Z")
            }
            
        except error.HTTPError as e:
            if e.code == 429:
                logger.warning("Axle API rate limit hit, backing off")
                self.next_poll_interval = min(self.next_poll_interval * 2, 3600)
            else:
                logger.error(f"Axle API error: {e.code}")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch Axle event: {e}")
            return None
    
    def _parse_event_times(self, event: Dict) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Parse ISO 8601 event times to datetime objects."""
        try:
            start_str = event["start_time"].replace("Z", "+00:00")
            end_str = event["end_time"].replace("Z", "+00:00")
            start = datetime.fromisoformat(start_str)
            end = datetime.fromisoformat(end_str)
            # Make timezone-naive for comparison
            start = start.replace(tzinfo=None)
            end = end.replace(tzinfo=None)
            return start, end
        except Exception as e:
            logger.error(f"Failed to parse event times: {e}")
            return None, None
    
    def _calculate_event_duration_minutes(self, event: Dict) -> int:
        """Calculate event duration in minutes for auto-resume."""
        start, end = self._parse_event_times(event)
        if not start or not end:
            # Default to 4 hours if parsing fails
            return 240
        duration = (end - start).total_seconds() / 60
        # Add 15 minute buffer for safety
        return int(duration) + 15
    
    def _calculate_poll_interval(self, event: Optional[Dict]) -> int:
        """Calculate appropriate polling interval based on event timing."""
        if not event:
            # No event - use normal interval
            return self.settings.get("poll_interval_normal", 10) * 60
        
        start, end = self._parse_event_times(event)
        if not start or not end:
            return self.settings.get("poll_interval_normal", 10) * 60
        
        now = datetime.utcnow()
        fast_window_hours = self.settings.get("fast_poll_window", 2)
        fast_before_start = start - timedelta(hours=fast_window_hours)
        
        # Fast polling: within fast window of start or during event
        if fast_before_start <= now <= end:
            return self.settings.get("poll_interval_fast", 1) * 60
        
        return self.settings.get("poll_interval_normal", 10) * 60
    
    async def _get_all_inverters(self) -> List[str]:
        """Get list of all connected inverter serial numbers."""
        try:
            result = await self._async_api_request("GET", "/api/inverters")
            if result.get("success") and "inverters" in result:
                # Extract serials from connected inverters
                serials = []
                for inv in result["inverters"].get("connected", []):
                    serial = inv.get("serial_number") or inv.get("key")
                    if serial:
                        serials.append(serial)
                return serials
        except Exception as e:
            logger.error(f"Failed to get inverter list: {e}")
        return []
    
    async def _trigger_export_on_all_inverters(self, event: Dict) -> bool:
        """Trigger discharge_now quick action on all inverters.
        
        Uses auto_resume_minutes calculated from event duration so inverters
        automatically resume normal operation after the event ends.
        """
        serials = await self._get_all_inverters()
        if not serials:
            logger.warning("No inverters found to trigger export")
            return False
        
        # Calculate auto-resume time from event duration
        auto_resume_minutes = self._calculate_event_duration_minutes(event)
        export_power = self.settings.get("export_power", 50)
        target_soc = self.settings.get("discharge_target_soc", 4)
        
        logger.info(f"Triggering export on {len(serials)} inverter(s) for {auto_resume_minutes} minutes")
        
        success_count = 0
        for serial in serials:
            try:
                # Use discharge_now quick action with auto-resume
                # This pauses the scheduler and sets max discharge
                body = {
                    "serial": serial,
                    "auto_resume_minutes": auto_resume_minutes,
                    # Additional parameters for power level would go here
                    # if the API supports them for discharge_now
                }
                
                result = await self._async_api_request("POST", "/api/quick/discharge_now", body)
                
                if result.get("success"):
                    logger.info(f"Export triggered for inverter {serial}")
                    success_count += 1
                else:
                    logger.error(f"Failed to trigger export for {serial}: {result.get('message')}")
                    
            except Exception as e:
                logger.error(f"Failed to trigger export for {serial}: {e}")
        
        if success_count == len(serials):
            logger.info(f"Export triggered successfully on all {len(serials)} inverter(s)")
            return True
        elif success_count > 0:
            logger.warning(f"Export triggered on {success_count}/{len(serials)} inverters")
            return True  # Partial success still means we're exporting
        else:
            logger.error("Failed to trigger export on any inverter")
            return False
    
    async def _resume_all_inverters(self) -> bool:
        """Resume normal operation on all inverters immediately."""
        serials = await self._get_all_inverters()
        if not serials:
            logger.info("No inverters to resume")
            return True
        
        logger.info(f"Resuming normal operation on {len(serials)} inverter(s)")
        
        success_count = 0
        for serial in serials:
            try:
                result = await self._async_api_request(
                    "POST",
                    "/api/quick/resume",
                    {"serial": serial}
                )
                
                if result.get("success"):
                    logger.info(f"Resume triggered for inverter {serial}")
                    success_count += 1
                else:
                    logger.error(f"Failed to resume {serial}: {result.get('message')}")
                    
            except Exception as e:
                logger.error(f"Failed to resume {serial}: {e}")
        
        return success_count > 0
    
    def _log_event(self, event: Dict, action: str):
        """Log event to local history file."""
        try:
            events_path = os.path.join(self.data_dir, EVENTS_FILE)
            events = []
            if os.path.exists(events_path):
                with open(events_path, "r") as f:
                    events = json.load(f)
            
            event_record = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "action": action,
                "event_start": event.get("start_time"),
                "event_end": event.get("end_time"),
                "import_export": event.get("import_export"),
                "duration_minutes": self._calculate_event_duration_minutes(event) if action == "started" else None
            }
            events.append(event_record)
            
            # Keep only last 100 events
            events = events[-100:]
            
            with open(events_path, "w") as f:
                json.dump(events, f, indent=2)
                
            logger.info(f"Event logged: {action} at {event_record['timestamp']}")
        except Exception as e:
            logger.error(f"Failed to log event: {e}")
    
    async def _check_and_handle_event(self):
        """Main polling logic - check for events and handle with quick settings."""
        if not self.settings.get("enabled"):
            logger.debug("Plugin disabled, skipping poll")
            return
        
        if not self.settings.get("api_key"):
            logger.warning("No API key configured, skipping poll")
            return
        
        logger.debug("Polling Axle API for events...")
        event = await self._fetch_axle_event()
        
        now = datetime.utcnow()
        self.last_poll_time = now
        
        # Calculate next poll interval
        self.next_poll_interval = self._calculate_poll_interval(event)
        
        if event:
            start, end = self._parse_event_times(event)
            if start and end:
                logger.info(f"Event found: {start} to {end}")
                
                # Check if event is currently active
                if start <= now <= end:
                    if not self.event_active:
                        logger.info("Event is now ACTIVE - triggering export")
                        self.event_active = True
                        self.current_event = event
                        
                        # Trigger discharge_now on all inverters
                        # This will auto-resume after event duration
                        success = await self._trigger_export_on_all_inverters(event)
                        
                        if success:
                            self._log_event(event, "started")
                        else:
                            logger.error("Failed to start export - will retry on next poll")
                            # Don't mark as active if we couldn't trigger
                            self.event_active = False
                        
                        self._save_state()
                    else:
                        # Event still active, check if we need to extend
                        # (in case event end time was extended)
                        pass
                else:
                    # Event is upcoming
                    if now < start:
                        minutes_until = (start - now).total_seconds() / 60
                        logger.debug(f"Event upcoming in {minutes_until:.0f} minutes")
        else:
            logger.debug("No active or upcoming events")
            
            # Check if we need to clean up from a previous event
            if self.event_active:
                logger.info("Event has ended - resuming normal operation")
                self.event_active = False
                
                # Trigger immediate resume on all inverters
                # (auto-resume should have already fired, but this ensures cleanup)
                await self._resume_all_inverters()
                
                if self.current_event:
                    self._log_event(self.current_event, "ended")
                
                self.current_event = None
                self._save_state()
    
    async def run(self):
        """Main plugin loop."""
        self.running = True
        logger.info("Axle Energy VPP plugin started (Quick Settings mode)")
        
        while self.running:
            try:
                await self._check_and_handle_event()
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
            
            # Wait for next poll
            logger.debug(f"Next poll in {self.next_poll_interval} seconds")
            await asyncio.sleep(self.next_poll_interval)
    
    async def stop(self):
        """Clean shutdown."""
        logger.info("Axle Energy VPP plugin stopping")
        self.running = False
        
        # If event was active, ensure we resume
        if self.event_active:
            await self._resume_all_inverters()
            if self.current_event:
                self._log_event(self.current_event, "ended_shutdown")


async def main():
    """Plugin entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [AxleVPP] %(levelname)s %(message)s",
        datefmt="%H:%M:%S"
    )
    
    plugin = AxlePlugin()
    
    try:
        await plugin.run()
    except asyncio.CancelledError:
        await plugin.stop()


if __name__ == "__main__":
    asyncio.run(main())
