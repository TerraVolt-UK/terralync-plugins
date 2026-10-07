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
STATE_FILE = "axle_state.json"


def _event_is_export(event: Optional[Dict]) -> bool:
    """Direction gate — Axle documents ``import_export`` as the string
    ``"import" | "export"``.  Missing/malformed values keep the legacy
    export behaviour so an absent field can't silently skip events."""
    v = (event or {}).get("import_export")
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() == "export"
    return bool(v)


class AxlePlugin:
    """Main plugin class for Axle Energy VPP integration using Quick Settings."""
    
    def __init__(self):
        self.plugin_dir = os.environ.get("TERRALYNC_PLUGIN_DIR", ".")
        self.data_dir = os.environ.get("TERRALYNC_PLUGIN_DATA_DIR", ".")
        self.api_base = os.environ.get("TERRALYNC_PLUGIN_API", "http://127.0.0.1:8080")
        
        self.settings: Dict[str, Any] = {}
        self.current_event: Optional[Dict] = None
        self.event_active = False
        self._import_logged: Optional[Tuple] = None
        self.last_poll_time: Optional[datetime] = None
        self.next_poll_interval = 900  # Default 15 minutes (900 seconds)
        self.running = False
        
        # Track auto-resume tasks per inverter to cancel if needed
        self._pending_resumes: Dict[str, asyncio.Task] = {}
        
        # Ensure data directory exists
        os.makedirs(self.data_dir, exist_ok=True)
        
        self._load_state()
    
    def _load_settings(self):
        """Load plugin settings from settings.json (called before every poll)."""
        settings_path = os.path.join(self.data_dir, "settings.json")
        try:
            with open(settings_path, "r") as f:
                self.settings = json.load(f)
            logger.debug("Settings reloaded")
        except FileNotFoundError:
            logger.warning("No settings.json found — configure the plugin via the TerraLync dashboard")
            self.settings = {}
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
        """Save persistent plugin state (also used by frontend for status display)."""
        state_path = os.path.join(self.data_dir, STATE_FILE)
        try:
            now_iso = datetime.utcnow().isoformat() + "Z"
            next_poll_iso = None
            if self.last_poll_time:
                next_dt = self.last_poll_time + timedelta(seconds=self.next_poll_interval)
                next_poll_iso = next_dt.isoformat() + "Z"
            state = {
                "current_event": self.current_event,
                "event_active": self.event_active,
                "last_poll_time": self.last_poll_time.isoformat() + "Z" if self.last_poll_time else None,
                "next_poll_time": next_poll_iso,
                "next_poll_interval_seconds": self.next_poll_interval,
                "last_saved": now_iso,
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
                "import_export": data.get("import_export"),
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
        """Calculate total duration for auto-resume including buffer on both sides.
        
        The inverter is set to discharge buffer minutes BEFORE the event starts
        and continues until buffer minutes AFTER the event ends.
        """
        start, end = self._parse_event_times(event)
        if not start or not end:
            # Default to 4 hours if parsing fails
            return 240
        
        buffer_minutes = self.settings.get("event_buffer_minutes", 3)
        duration = (end - start).total_seconds() / 60
        # Add buffer on both sides (before start and after end)
        return int(duration) + (buffer_minutes * 2)
    
    def _calculate_poll_interval(self, event: Optional[Dict]) -> int:
        """Calculate appropriate polling interval based on event timing."""
        # Convert to int in case settings are stored as strings
        normal_interval = int(self.settings.get("poll_interval_normal", 15)) * 60
        fast_interval = int(self.settings.get("poll_interval_fast", 90))

        if not event:
            # No event - use normal interval
            return normal_interval

        start, end = self._parse_event_times(event)
        if not start or not end:
            return normal_interval
        
        now = datetime.utcnow()
        fast_window_hours = self.settings.get("fast_poll_window", 1)
        buffer_minutes = self.settings.get("event_buffer_minutes", 3)
        
        # Start fast polling before the event start minus buffer and fast window
        fast_before_start = start - timedelta(hours=fast_window_hours, minutes=buffer_minutes)
        # Continue fast polling until event end plus buffer
        buffered_end = end + timedelta(minutes=buffer_minutes)
        
        # Fast polling: within fast window of buffered start or during buffered event period
        if fast_before_start <= now <= buffered_end:
            return fast_interval

        return normal_interval
    
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
        else:
            logger.error(f"Failed to trigger export on {len(serials) - success_count}/{len(serials)} inverters — all must succeed for grid event")
            return False  # Partial success is NOT acceptable during grid events
    
    async def _check_and_handle_event_extension(self, new_event: Dict):
        """Check if event end time was extended and re-trigger with updated duration.
        
        Axle may extend events while they're active. We need to detect this and
        re-trigger discharge_now with the new auto_resume_minutes so the inverter
        doesn't resume early.
        """
        if not self.current_event:
            return
        
        old_start, old_end = self._parse_event_times(self.current_event)
        new_start, new_end = self._parse_event_times(new_event)
        
        if not old_end or not new_end:
            return
        
        # Check if end time was extended
        if new_end > old_end:
            old_duration = self._calculate_event_duration_minutes(self.current_event)
            new_duration = self._calculate_event_duration_minutes(new_event)
            
            logger.info(f"Event extended: end time moved from {old_end} to {new_end}")
            logger.info(f"Duration changed from {old_duration} to {new_duration} minutes - re-triggering export")
            
            # Re-trigger export with updated duration
            # This cancels the old auto-resume timer and sets a new one
            success = await self._trigger_export_on_all_inverters(new_event)
            
            if success:
                logger.info("Export re-triggered successfully with extended duration")
                self.current_event = new_event  # Update stored event
                self._save_state()
            else:
                logger.error("Failed to re-trigger export for extended event - will retry on next poll")
    
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
        # Reload settings on every poll so live changes take effect without restart
        self._load_settings()

        if not self.settings.get("enabled"):
            logger.warning("Plugin is disabled — enable it via TerraLync Dashboard → Plugins → Axle Energy VPP → Settings")
            return
        
        if not self.settings.get("api_key"):
            logger.warning("No API key configured — add your Axle API key via TerraLync Dashboard → Plugins → Axle Energy VPP → Settings")
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
                buffer_minutes = self.settings.get("event_buffer_minutes", 3)
                buffered_start = start - timedelta(minutes=buffer_minutes)
                buffered_end = end + timedelta(minutes=buffer_minutes)
                
                logger.info(f"Event found: {start} to {end} (buffered: {buffered_start} to {buffered_end})")
                
                # Check if we're within the buffered event window
                # (starts buffer minutes before event, ends buffer minutes after)
                if buffered_start <= now <= buffered_end:
                    if not self.event_active:
                        if _event_is_export(event):
                            logger.info(f"Event BUFFER period active - triggering export ({buffer_minutes} min buffer)")
                            self.event_active = True
                            self.current_event = event

                            # Trigger discharge_now on all inverters
                            # Auto-resume covers full buffered duration
                            success = await self._trigger_export_on_all_inverters(event)

                            if success:
                                self._log_event(event, "started")
                            else:
                                logger.error("Failed to start export - will retry on next poll")
                                # Don't mark as active if we couldn't trigger
                                self.event_active = False

                            self._save_state()
                        elif self._import_logged != (start, end):
                            # Import-direction event — record it once,
                            # never discharge into it.
                            self._import_logged = (start, end)
                            logger.info("Import-direction event — standing down")
                            self._log_event(event, "import_skipped")
                            self._save_state()
                    else:
                        # Event already active - check if end time was extended
                        await self._check_and_handle_event_extension(event)
                else:
                    # Event is upcoming (before buffer period)
                    if now < buffered_start:
                        minutes_until = (buffered_start - now).total_seconds() / 60
                        logger.debug(f"Event upcoming in {minutes_until:.0f} minutes (includes {buffer_minutes} min buffer)")
            
            # Check if current event has passed its buffered end time
            if self.event_active and self.current_event:
                _, current_end = self._parse_event_times(self.current_event)
                if current_end:
                    buffer_minutes = self.settings.get("event_buffer_minutes", 3)
                    buffered_current_end = current_end + timedelta(minutes=buffer_minutes)
                    if now > buffered_current_end:
                        logger.info("Buffered event period has ended - resuming normal operation")
                        self.event_active = False
                        await self._resume_all_inverters()
                        self._log_event(self.current_event, "ended")
                        self.current_event = None
                        self._save_state()
        else:
            logger.info("Axle API polled — no active or upcoming events")
            
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

        # Always save state after a successful poll so the frontend shows
        # current last_poll_time / next_poll_time regardless of event activity
        self._save_state()
    
    async def run(self):
        """Main plugin loop."""
        self.running = True
        logger.info("Axle Energy VPP plugin started (Quick Settings mode)")
        logger.info(f"Axle API endpoint: {AXLE_API_BASE}")
        logger.info(f"TerraLync API base: {self.api_base}")
        logger.info(f"Data directory: {self.data_dir}")
        
        while self.running:
            try:
                await self._check_and_handle_event()
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
            
            # Wait for next poll
            logger.info(f"Next poll in {self.next_poll_interval} seconds")
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
