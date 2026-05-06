#!/usr/bin/env python3
"""Octopus Energy Tariff Optimiser for TerraLync.

Fetches tariff and rate data from Octopus Energy public APIs and
automatically adjusts battery schedules to charge when electricity is
cheapest (Intelligent Go, Agile Octopus) or to export during Saving
Sessions. Uses the TerraLync scheduler for routine charge slots and
Quick Settings for exceptional events.
"""

import asyncio
import json
import logging
import os
import random
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("octopus_energy")

_OE_REST_BASE = "https://api.octopus.energy/v1"
_OE_GQL_BASE = "https://api.octopus.energy/v1/graphql/"

_TARIFF_INTELLIGENT = ("INTELLI",)
_TARIFF_AGILE = ("AGILE",)
_TARIFF_GO = ("GO", "E-1R-GO")

_REGION_MAP = {
    "A": "_A", "B": "_B", "C": "_C", "D": "_D", "E": "_E",
    "F": "_F", "G": "_G", "H": "_H", "J": "_J", "K": "_K",
    "L": "_L", "M": "_M", "N": "_N", "P": "_P",
}

_STATE_FILE = "octopus_plugin_state.json"
_AGILE_CACHE_FILE = "agile_rates_cache.json"
_AGILE_EXPORT_CACHE_FILE = "agile_export_rates_cache.json"
_INTELLIGENT_CACHE_FILE = "intelligent_dispatches.json"
_SAVING_SESSION_FILE = "saving_sessions.json"
_RATES_DISPLAY_FILE = "rates_display.json"

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _load_json(path: str) -> Dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: Dict):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("Failed to write %s: %s", path, exc)


class OctopusClient:
    """Low-level wrapper around Octopus REST and GraphQL APIs."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._auth_header = {"Authorization": f"Basic {api_key}"}

    def _rest(self, path: str, params: Optional[Dict] = None, timeout: int = 30) -> Dict:
        url = _OE_REST_BASE + path
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url += "?" + qs
        req = urllib.request.Request(url, headers=self._auth_header, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Octopus REST {exc.code}: {body}") from exc

    def _gql(self, query: str, variables: Dict, timeout: int = 30) -> Dict:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        headers = {**self._auth_header, "Content-Type": "application/json", "Accept": "application/json"}
        req = urllib.request.Request(_OE_GQL_BASE, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Octopus GraphQL {exc.code}: {body}") from exc

    def fetch_account(self, account_number: str) -> Dict:
        return self._rest(f"/accounts/{account_number}/")

    def fetch_rates(self, product_code: str, tariff_code: str, period_from: str, period_to: str) -> List[Dict]:
        all_results: List[Dict] = []
        page = 1
        while True:
            data = self._rest(
                f"/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/",
                {"period_from": period_from, "period_to": period_to, "page": str(page)},
            )
            results = data.get("results", [])
            if not results:
                break
            all_results.extend(results)
            if data.get("next") is None:
                break
            page += 1
        return all_results

    def fetch_export_rates(self, product_code: str, tariff_code: str, period_from: str, period_to: str) -> List[Dict]:
        """Fetch export tariff rates (e.g., Agile Outgoing)."""
        all_results: List[Dict] = []
        page = 1
        while True:
            data = self._rest(
                f"/products/{product_code}/electricity-tariffs/{tariff_code}/export-payment-rates/",
                {"period_from": period_from, "period_to": period_to, "page": str(page)},
            )
            results = data.get("results", [])
            if not results:
                break
            all_results.extend(results)
            if data.get("next") is None:
                break
            page += 1
        return all_results

    def fetch_planned_dispatches(self, account_number: str) -> List[Dict]:
        query = (
            "query PlannedDispatches($accountNumber: String!) {"
            "  plannedDispatches(accountNumber: $accountNumber) {"
            "    start end source location meta { totalCostAdded totalCarbonSaved }"
            "  }"
            "}"
        )
        resp = self._gql(query, {"accountNumber": account_number})
        return resp.get("data", {}).get("plannedDispatches") or []

    def fetch_saving_sessions(self, account_number: str) -> Dict[str, List[Dict]]:
        """Fetch saving sessions - returns dict with 'available' and 'joined' events."""
        query = (
            "query OctoplusAccountInfo($accountNumber: String!) {"
            "  octoplusAccountInfo(accountNumber: $accountNumber) {"
            "    availableEvents { id eventCode startAt endAt incentiveRate targetRegions }"
            "    joinedEvents { id eventCode startAt endAt incentiveRate targetRegions }"
            "  }"
            "}"
        )
        resp = self._gql(query, {"accountNumber": account_number})
        info = resp.get("data", {}).get("octoplusAccountInfo") or {}
        return {
            "available": info.get("availableEvents") or [],
            "joined": info.get("joinedEvents") or []
        }


class TerraLyncAPI:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _call(self, method: str, path: str, body: Optional[Dict] = None, timeout: int = 30) -> Dict:
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"TerraLync API {exc.code}: {body}") from exc

    def get_schedule(self, day: str) -> Dict:
        return self._call("GET", f"/api/scheduler/schedule/{day}")

    def save_schedule(self, day: str, schedule: Dict) -> Dict:
        return self._call("POST", f"/api/scheduler/schedule/{day}", schedule)

    def get_inverters(self) -> List[str]:
        result = self._call("GET", "/api/inverters")
        if result.get("success") and "inverters" in result:
            return [inv.get("serial_number") or inv.get("key") for inv in result["inverters"].get("connected", []) if inv.get("serial_number") or inv.get("key")]
        return []

    def quick_action(self, action: str, serial: Optional[str] = None, auto_resume_minutes: int = 240, **kwargs) -> Dict:
        body = {"auto_resume_minutes": auto_resume_minutes, **kwargs}
        if serial:
            body["serial"] = serial
        return self._call("POST", f"/api/quick/{action}", body)


class OctopusPlugin:
    def __init__(self):
        self.plugin_dir = os.environ.get("TERRALYNC_PLUGIN_DIR", ".")
        self.data_dir = os.environ.get("TERRALYNC_PLUGIN_DATA_DIR", ".")
        self.api_base = os.environ.get("TERRALYNC_PLUGIN_API", "http://127.0.0.1:8080")
        self.settings: Dict[str, Any] = {}
        self.running = False
        self._state: Dict = {}
        self._oe: Optional[OctopusClient] = None
        self._tl: Optional[TerraLyncAPI] = None
        self._account_info: Optional[Dict] = None
        self._detected_tariff: Optional[str] = None
        self._current_event: Optional[Dict] = None
        self._event_active = False
        self._pending_resumes: Dict[str, asyncio.Task] = {}
        self._load_settings()
        self._state = _load_json(os.path.join(self.data_dir, _STATE_FILE))
        self._tl = TerraLyncAPI(self.api_base)

    def _load_settings(self):
        settings_path = os.path.join(self.data_dir, "settings.json")
        try:
            with open(settings_path, "r") as f:
                self.settings = json.load(f)
            logger.info("Settings loaded")
        except Exception as e:
            logger.warning("Could not load settings: %s", e)
            self.settings = {}

    def _save_state(self):
        _save_json(os.path.join(self.data_dir, _STATE_FILE), self._state)

    def _parse_iso(self, ts: str) -> Optional[datetime]:
        try:
            ts = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(ts).replace(tzinfo=None)
        except Exception:
            return None

    def _format_iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _async_api_call(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        # run_in_executor doesn't support kwargs, so we use a closure
        if kwargs:
            return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        return await loop.run_in_executor(None, fn, *args)

    async def _discover_account(self) -> bool:
        """Fetch account info and identify the active electricity meter point."""
        if not self.settings.get("api_key") or not self.settings.get("account_number"):
            logger.error("API key and account number required")
            return False
        self._oe = OctopusClient(self.settings["api_key"])
        try:
            acc = await self._async_api_call(self._oe.fetch_account, self.settings["account_number"])
        except Exception as exc:
            logger.error("Failed to fetch account: %s", exc)
            return False
        self._account_info = acc
        props = acc.get("properties", [])
        if not props:
            logger.error("No properties found in account")
            return False
        elec_points = props[0].get("electricity_meter_points", [])
        if not elec_points:
            logger.error("No electricity meter points found")
            return False
        target_mpan = self.settings.get("mpan")
        chosen = None
        for mp in elec_points:
            if target_mpan and mp.get("mpan") == target_mpan:
                chosen = mp
                break
        if not chosen:
            for mp in elec_points:
                if not mp.get("is_export", False):
                    chosen = mp
                    break
        if not chosen:
            chosen = elec_points[0]
        self._state["mpan"] = chosen.get("mpan")
        self._state["region_code"] = self._extract_region(chosen.get("mpan", ""))
        if self.settings.get("region_code"):
            self._state["region_code"] = self.settings["region_code"]
        agreements = chosen.get("agreements", [])
        active_agreement = None
        now = datetime.utcnow()
        for ag in agreements:
            valid_from = self._parse_iso(ag.get("valid_from", ""))
            valid_to = ag.get("valid_to")
            if valid_from and valid_from <= now:
                if valid_to is None or self._parse_iso(valid_to) is None or self._parse_iso(valid_to) > now:
                    active_agreement = ag
                    break
        if active_agreement:
            tariff_code = active_agreement.get("tariff_code", "")
            self._state["tariff_code"] = tariff_code
            self._detected_tariff = self._classify_tariff(tariff_code)
            self._state["detected_tariff_mode"] = self._detected_tariff
            logger.info("Detected tariff: %s (mode: %s)", tariff_code, self._detected_tariff)
        else:
            logger.warning("No active tariff agreement found")
        self._save_state()
        return True

    def _extract_region(self, mpan: str) -> str:
        """Extract region letter from MPAN (13-21 digits, region is first 2 of last 10)."""
        if len(mpan) >= 13:
            region_id = mpan[-10:-8]
            region_map = {"10": "A", "11": "B", "12": "C", "13": "D", "14": "E", "15": "F", "16": "G", "17": "H", "18": "J", "19": "K", "20": "L", "21": "M", "22": "N", "23": "P"}
            return region_map.get(region_id, "C")
        return "C"

    def _classify_tariff(self, code: str) -> str:
        """Classify tariff code into mode."""
        code_upper = code.upper()
        for prefix in _TARIFF_INTELLIGENT:
            if prefix in code_upper:
                return "intelligent_go"
        for prefix in _TARIFF_AGILE:
            if prefix in code_upper:
                return "agile"
        for prefix in _TARIFF_GO:
            if prefix in code_upper:
                return "fixed_offpeak"
        return "standard_variable"

    def _get_effective_mode(self) -> str:
        """Return effective tariff mode considering user override."""
        mode = self.settings.get("tariff_mode", "auto")
        if mode == "auto":
            return self._detected_tariff or "standard_variable"
        return mode

    def _load_cached_rates(self, cache_file: str) -> Tuple[List[Dict], Optional[datetime]]:
        """Load cached rates and return (rates_list, latest_end_time)."""
        cache_path = os.path.join(self.data_dir, cache_file)
        data = _load_json(cache_path)
        rates = data.get("rates", [])
        if not rates:
            return [], None
        latest_end = None
        for rate in rates:
            end_str = rate.get("valid_to") or rate.get("end", "")
            end_dt = self._parse_iso(end_str)
            if end_dt and (latest_end is None or end_dt > latest_end):
                latest_end = end_dt
        return rates, latest_end

    def _merge_rates(self, existing: List[Dict], new_rates: List[Dict]) -> List[Dict]:
        """Merge new rates into existing cache, avoiding duplicates by valid_from."""
        seen = {}
        for rate in existing + new_rates:
            key = rate.get("valid_from") or rate.get("start", "")
            if key:
                seen[key] = rate
        sorted_rates = sorted(seen.values(), key=lambda x: x.get("valid_from") or x.get("start", ""))
        return sorted_rates

    async def _fetch_rates_with_caching(
        self,
        product_code: str,
        tariff_code: str,
        cache_file: str,
        fetch_fn,
        tomorrow_only: bool = True
    ) -> Optional[List[Dict]]:
        """Fetch rates with intelligent caching - only fetches gaps."""
        cached_rates, latest_end = self._load_cached_rates(cache_file)
        now = datetime.utcnow()
        if tomorrow_only:
            tomorrow = now.date() + timedelta(days=1)
            period_from = datetime.combine(tomorrow, datetime.min.time())
            period_to = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time())
        else:
            period_from = datetime.combine(now.date(), datetime.min.time())
            period_to = datetime.combine(now.date() + timedelta(days=2), datetime.min.time())
        
        # If we have cached rates covering up to or past our target end, use them
        if latest_end and latest_end >= period_to:
            logger.debug("Using cached rates (cached until %s, need until %s)", latest_end, period_to)
            return cached_rates
        
        # Determine fetch window - only fetch what's missing
        fetch_from = latest_end if (latest_end and latest_end > period_from) else period_from
        
        try:
            new_rates = await self._async_api_call(
                fetch_fn, product_code, tariff_code,
                self._format_iso(fetch_from), self._format_iso(period_to)
            )
            if new_rates:
                merged = self._merge_rates(cached_rates, new_rates)
                _save_json(os.path.join(self.data_dir, cache_file), {
                    "date": str(now.date()),
                    "rates": merged,
                    "last_updated": now.isoformat() + "Z"
                })
                logger.info("Fetched %d new rates, merged to %d total", len(new_rates), len(merged))
                return merged
            return cached_rates if cached_rates else None
        except Exception as exc:
            logger.warning("Rate fetch failed: %s", exc)
            return cached_rates if cached_rates else None

    async def _fetch_agile_rates_with_retry(self) -> Optional[List[Dict]]:
        """Fetch Agile import rates with caching and retry for late publication."""
        mode = self._get_effective_mode()
        if mode != "agile":
            return None
        tariff_code = self._state.get("tariff_code", "")
        if not tariff_code:
            logger.error("No tariff code available for Agile fetch")
            return None
        product_code = tariff_code.split("-")[2] if len(tariff_code.split("-")) >= 3 else "AGILE-FLEX-22-11-25"
        region = self._state.get("region_code", "C")
        full_tariff = f"E-1R-{product_code}-{region}"
        
        # First try cached rates
        rates, latest_end = self._load_cached_rates(_AGILE_CACHE_FILE)
        tomorrow = datetime.utcnow().date() + timedelta(days=1)
        period_to = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time())
        
        if rates and latest_end and latest_end >= period_to:
            logger.info("Using %d cached Agile rates", len(rates))
            return rates
        
        # Cache miss or incomplete - fetch with retry
        base_delay = self.settings.get("agile_poll_retry_delay", 60)
        max_retry_hours = self.settings.get("max_agile_retry_hours", 6)
        max_total_delay = max_retry_hours * 3600
        total_delay = 0
        attempt = 0
        period_from = self._format_iso(datetime.combine(tomorrow, datetime.min.time()))
        period_to_str = self._format_iso(period_to)
        
        while total_delay < max_total_delay:
            try:
                new_rates = await self._async_api_call(
                    self._oe.fetch_rates, product_code, full_tariff, period_from, period_to_str
                )
                if new_rates and len(new_rates) >= 40:
                    merged = self._merge_rates(rates, new_rates)
                    _save_json(os.path.join(self.data_dir, _AGILE_CACHE_FILE), {
                        "date": str(tomorrow), 
                        "rates": merged,
                        "last_updated": datetime.utcnow().isoformat() + "Z"
                    })
                    logger.info("Fetched %d Agile rates for %s", len(merged), tomorrow)
                    # Also fetch export rates for completeness
                    await self._fetch_agile_export_rates()
                    return merged
                else:
                    logger.debug("Agile rates not ready yet (attempt %d)", attempt + 1)
            except Exception as exc:
                logger.warning("Agile fetch error (attempt %d): %s", attempt + 1, exc)
            attempt += 1
            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 30), 3600)
            total_delay += delay
            if total_delay >= max_total_delay:
                logger.warning("Agile retry window exceeded %d hours, giving up", max_retry_hours)
                break
            logger.info("Waiting %.0f seconds before retry (total delay: %.0f min)...", delay, total_delay / 60)
            await asyncio.sleep(delay)
        
        # Return cached rates even if stale, rather than nothing
        if rates:
            logger.warning("Using stale cached rates (%.0f hours old)", total_delay / 3600)
            return rates
        logger.error("Failed to fetch Agile rates (%.0f hours elapsed)", total_delay / 3600)
        return None

    async def _fetch_agile_export_rates(self) -> Optional[List[Dict]]:
        """Fetch Agile export (outgoing) rates for export optimization."""
        if not self.settings.get("enable_export_tariff", False):
            return None
        export_product = self.settings.get("export_tariff_product", "AGILE-OUTGOING-19-05-13")
        region = self._state.get("region_code", "C")
        full_tariff = f"E-1R-{export_product}-{region}"
        tomorrow = datetime.utcnow().date() + timedelta(days=1)
        period_from = self._format_iso(datetime.combine(tomorrow, datetime.min.time()))
        period_to = self._format_iso(datetime.combine(tomorrow + timedelta(days=1), datetime.min.time()))
        
        try:
            export_rates = await self._async_api_call(
                self._oe.fetch_export_rates, export_product, full_tariff, 
                period_from, period_to
            )
            if export_rates:
                _save_json(os.path.join(self.data_dir, _AGILE_EXPORT_CACHE_FILE), {
                    "date": str(tomorrow),
                    "rates": export_rates,
                    "last_updated": datetime.utcnow().isoformat() + "Z"
                })
                logger.info("Fetched %d Agile export rates", len(export_rates))
                return export_rates
        except Exception as exc:
            logger.debug("Failed to fetch export rates (may not be on Agile Outgoing): %s", exc)
        return None

    def _rates_to_charge_slots(self, rates: List[Dict], num_slots: int, max_price: float) -> List[Tuple[str, str]]:
        """Convert half-hourly rates to charge slot time ranges."""
        sorted_rates = sorted(rates, key=lambda x: x.get("value_inc_vat", 999))
        selected = sorted_rates[:num_slots] if max_price <= 0 else [r for r in sorted_rates if r.get("value_inc_vat", 999) <= max_price][:num_slots]
        if not selected:
            return []
        sorted_by_time = sorted(selected, key=lambda x: x.get("valid_from", ""))
        slots = []
        current_start = None
        current_end = None
        for rate in sorted_by_time:
            start = self._parse_iso(rate.get("valid_from", ""))
            end = self._parse_iso(rate.get("valid_to", ""))
            if not start or not end:
                continue
            if current_start is None:
                current_start = start
                current_end = end
            elif start == current_end:
                current_end = end
            else:
                slots.append((current_start.strftime("%H:%M"), current_end.strftime("%H:%M")))
                current_start = start
                current_end = end
        if current_start and current_end:
            slots.append((current_start.strftime("%H:%M"), current_end.strftime("%H:%M")))
        return slots

    async def _write_agile_schedule(self, rates: List[Dict]):
        """Write Agile charge slots to tomorrow's scheduler."""
        num_slots = self.settings.get("agile_cheap_slots", 8)
        max_price = self.settings.get("agile_max_price_pence", 15.0)
        charge_slots = self._rates_to_charge_slots(rates, num_slots, max_price)
        if not charge_slots:
            logger.warning("No cheap slots identified from Agile rates")
            return
        tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%A").lower()
        blocks = []
        for idx, (start, end) in enumerate(charge_slots):
            block = {
                "id": f"agile_charge_{idx}_{start.replace(':', '')}",
                "type": "charge_slot",
                "start_time": start,
                "end_time": end,
                "settings": {
                    "charge_target": self.settings.get("charge_target_soc", 100),
                    "charge_power": self.settings.get("charge_power_steps", 50)
                }
            }
            blocks.append(block)
        schedule = {"day": tomorrow, "active": True, "blocks": blocks}
        try:
            result = await self._async_api_call(self._tl.save_schedule, tomorrow, schedule)
            if result.get("success"):
                logger.info("Saved Agile schedule for %s with %d charge blocks", tomorrow, len(blocks))
            else:
                logger.error("Failed to save schedule: %s", result.get("message"))
        except Exception as exc:
            logger.error("Exception saving schedule: %s", exc)

    async def _write_intelligent_schedule(self):
        """Write fixed Intelligent Go charge slot to all days."""
        start = self.settings.get("intelligent_go_start", "23:30")
        end = self.settings.get("intelligent_go_end", "05:30")
        for day in DAYS:
            schedule = await self._async_api_call(self._tl.get_schedule, day)
            existing = [b for b in schedule.get("blocks", []) if not b.get("id", "").startswith("intelli")]
            block = {
                "id": f"intelli_charge_{start.replace(':', '')}",
                "type": "charge_slot",
                "start_time": start,
                "end_time": end,
                "settings": {
                    "charge_target": self.settings.get("charge_target_soc", 100),
                    "charge_power": self.settings.get("charge_power_steps", 50)
                }
            }
            new_schedule = {"day": day, "active": True, "blocks": existing + [block]}
            try:
                await self._async_api_call(self._tl.save_schedule, day, new_schedule)
            except Exception as exc:
                logger.error("Failed to save %s schedule: %s", day, exc)
        logger.info("Applied Intelligent Go schedule to all days")

    async def _check_saving_sessions(self):
        """Poll for saving sessions and trigger export if a JOINED event is active."""
        if not self.settings.get("saving_sessions_enabled", True):
            return
        try:
            result = await self._async_api_call(self._oe.fetch_saving_sessions, self.settings["account_number"])
        except Exception as exc:
            logger.warning("Failed to fetch saving sessions: %s", exc)
            return
        now = datetime.utcnow()
        region = self._state.get("region_code", "C")
        # Check available events (not yet joined)
        available_events = result.get("available", [])
        unjoined_in_region = []
        for ev in available_events:
            target_regions = ev.get("targetRegions") or []
            if target_regions and f"_{region}" not in target_regions:
                continue
            start = self._parse_iso(ev.get("startAt", ""))
            if start and start > now:
                unjoined_in_region.append(ev)
        if unjoined_in_region:
            logger.info("Found %d available saving session(s) not yet joined. Join via Octopus app to participate.", len(unjoined_in_region))
            # Could add auto-join logic here if we had a mutation
        # Only act on JOINED events - not just available ones!
        joined_events = result.get("joined", [])
        active_event = None
        for ev in joined_events:
            # Filter by region - events may be region-specific
            target_regions = ev.get("targetRegions") or []
            if target_regions and f"_{region}" not in target_regions:
                continue
            start = self._parse_iso(ev.get("startAt", ""))
            end = self._parse_iso(ev.get("endAt", ""))
            if start and end and start <= now <= end:
                active_event = ev
                break
        if active_event and not self._event_active:
            logger.info("Saving session active (joined event) - triggering export")
            await self._trigger_export(active_event)
            self._event_active = True
            self._current_event = active_event
            self._log_event(active_event, "started")
        elif not active_event and self._event_active:
            logger.info("Saving session ended - resuming normal operation")
            await self._resume_all()
            self._event_active = False
            if self._current_event:
                self._log_event(self._current_event, "ended")
            self._current_event = None

    async def _trigger_export(self, event: Dict):
        """Trigger discharge_now on all inverters during saving session."""
        serials = await self._async_api_call(self._tl.get_inverters)
        if not serials:
            logger.warning("No inverters found for export")
            return
        duration = self._calculate_event_duration(event)
        for serial in serials:
            try:
                await self._async_api_call(self._tl.quick_action, "discharge_now", serial, duration, target_soc=self.settings.get("saving_session_min_soc", 10))
                logger.info("Export triggered for %s", serial)
            except Exception as exc:
                logger.error("Failed to trigger export for %s: %s", serial, exc)

    async def _resume_all(self):
        """Resume normal operation on all inverters."""
        serials = await self._async_api_call(self._tl.get_inverters)
        for serial in serials:
            try:
                await self._async_api_call(self._tl.quick_action, "resume", serial)
            except Exception as exc:
                logger.warning("Resume failed for %s: %s", serial, exc)

    def _calculate_event_duration(self, event: Dict) -> int:
        start = self._parse_iso(event.get("startAt", ""))
        end = self._parse_iso(event.get("endAt", ""))
        if start and end:
            return max(int((end - start).total_seconds() / 60), 30)
        return 60

    def _log_event(self, event: Dict, action: str):
        try:
            path = os.path.join(self.data_dir, _SAVING_SESSION_FILE)
            events = _load_json(path).get("events", [])
            record = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "action": action,
                "event_start": event.get("startAt"),
                "event_end": event.get("endAt"),
                "incentive_rate": event.get("incentiveRate")
            }
            events.append(record)
            _save_json(path, {"events": events[-100:]})
        except Exception as exc:
            logger.warning("Failed to log event: %s", exc)

    async def _run_agile_daily_update(self):
        """Daily task to fetch Agile rates, update schedules, and export chart data."""
        rates = await self._fetch_agile_rates_with_retry()
        if rates:
            await self._write_agile_schedule(rates)
            await self._export_chart_data(rates)

    async def _run_intelligent_dispatch_check(self):
        """Poll for Intelligent planned dispatches and write active/started dispatches as charge slots."""
        if self._get_effective_mode() != "intelligent_go":
            return
        try:
            dispatches = await self._async_api_call(self._oe.fetch_planned_dispatches, self.settings["account_number"])
        except Exception as exc:
            logger.warning("Failed to fetch planned dispatches: %s", exc)
            return
        now = datetime.utcnow()
        mode = self.settings.get("intelligent_dispatch_mode", "planned_and_started")
        # Filter dispatches based on mode
        if mode == "started_only":
            relevant_dispatches = [d for d in dispatches if self._parse_iso(d.get("start", "")) and self._parse_iso(d.get("end", "")) and self._parse_iso(d.get("start", "")) <= now <= self._parse_iso(d.get("end", ""))]
        else:
            relevant_dispatches = [d for d in dispatches if self._parse_iso(d.get("start", "")) and self._parse_iso(d.get("end", "")) and self._parse_iso(d.get("end", "")) >= now]
        _save_json(os.path.join(self.data_dir, _INTELLIGENT_CACHE_FILE), {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "dispatches": dispatches,
            "relevant": [d for d in relevant_dispatches]
        })
        # Export chart data
        self._export_intelligent_chart_data(dispatches)
        # Write active dispatches as additional charge slots for today
        if relevant_dispatches:
            today = now.strftime("%A").lower()
            try:
                schedule = await self._async_api_call(self._tl.get_schedule, today)
                existing = [b for b in schedule.get("blocks", []) if not b.get("id", "").startswith("intelli_dispatch_")]
                new_blocks = []
                for idx, d in enumerate(relevant_dispatches):
                    start_dt = self._parse_iso(d.get("start", ""))
                    end_dt = self._parse_iso(d.get("end", ""))
                    if not start_dt or not end_dt:
                        continue
                    block = {
                        "id": f"intelli_dispatch_{idx}_{start_dt.strftime('%H%M')}",
                        "type": "charge_slot",
                        "start_time": start_dt.strftime("%H:%M"),
                        "end_time": end_dt.strftime("%H:%M"),
                        "settings": {
                            "charge_target": self.settings.get("charge_target_soc", 100),
                            "charge_power": self.settings.get("charge_power_steps", 50)
                        }
                    }
                    new_blocks.append(block)
                new_schedule = {"day": today, "active": True, "blocks": existing + new_blocks}
                await self._async_api_call(self._tl.save_schedule, today, new_schedule)
                logger.info("Applied %d active Intelligent dispatch charge slot(s) for %s", len(new_blocks), today)
            except Exception as exc:
                logger.error("Failed to apply intelligent dispatch schedule: %s", exc)

    def _export_chart_data(self, rates: List[Dict]):
        """Export rate data for chart display."""
        try:
            display_data = {
                "tariff_type": "agile",
                "region": self._state.get("region_code", "C"),
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "import_rates": [],
                "export_rates": []
            }
            # Format import rates for chart
            for rate in rates:
                display_data["import_rates"].append({
                    "start": rate.get("valid_from", ""),
                    "end": rate.get("valid_to", ""),
                    "price_pence": rate.get("value_inc_vat", 0)
                })
            # Add export rates if available
            export_cache = _load_json(os.path.join(self.data_dir, _AGILE_EXPORT_CACHE_FILE))
            for rate in export_cache.get("rates", []):
                display_data["export_rates"].append({
                    "start": rate.get("valid_from", ""),
                    "end": rate.get("valid_to", ""),
                    "price_pence": rate.get("value_inc_vat", 0)
                })
            # Add selected charge slots
            num_slots = self.settings.get("agile_cheap_slots", 8)
            max_price = self.settings.get("agile_max_price_pence", 15.0)
            selected = self._rates_to_charge_slots(rates, num_slots, max_price)
            display_data["selected_slots"] = [
                {"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M")} 
                for s, e in selected
            ]
            _save_json(os.path.join(self.data_dir, _RATES_DISPLAY_FILE), display_data)
            logger.debug("Exported chart data with %d import rates", len(display_data["import_rates"]))
        except Exception as exc:
            logger.warning("Failed to export chart data: %s", exc)

    def _export_intelligent_chart_data(self, dispatches: List[Dict]):
        """Export Intelligent dispatch data for display."""
        try:
            display_data = {
                "tariff_type": "intelligent_go",
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "fixed_window": {
                    "start": self.settings.get("intelligent_go_start", "23:30"),
                    "end": self.settings.get("intelligent_go_end", "05:30")
                },
                "planned_dispatches": [
                    {
                        "start": d.get("start", ""),
                        "end": d.get("end", ""),
                        "source": d.get("source", ""),
                        "energy_added": d.get("meta", {}).get("totalCostAdded", 0)
                    }
                    for d in dispatches
                ]
            }
            _save_json(os.path.join(self.data_dir, _RATES_DISPLAY_FILE), display_data)
            logger.debug("Exported Intelligent chart data with %d dispatches", len(dispatches))
        except Exception as exc:
            logger.warning("Failed to export Intelligent chart data: %s", exc)

    async def run(self):
        self.running = True
        logger.info("Octopus Energy plugin started")
        if not await self._discover_account():
            logger.error("Account discovery failed - plugin cannot continue")
            return
        mode = self._get_effective_mode()
        logger.info("Operating in %s mode", mode)
        
        # Export initial chart data
        self._export_chart_data([])
        
        if mode == "intelligent_go":
            await self._write_intelligent_schedule()
            # Export initial intelligent data
            self._export_intelligent_chart_data([])
        elif mode == "agile":
            asyncio.create_task(self._agile_scheduler_loop())
        elif mode == "fixed_offpeak":
            await self._write_intelligent_schedule()
        asyncio.create_task(self._saving_session_loop())
        if mode == "intelligent_go":
            asyncio.create_task(self._intelligent_dispatch_loop())
        while self.running:
            await asyncio.sleep(60)

    async def _agile_scheduler_loop(self):
        """Run daily around 16:15 to fetch next day's Agile rates."""
        while self.running:
            now = datetime.utcnow()
            target = now.replace(hour=16, minute=15, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            logger.info("Next Agile update at %s (in %.0f seconds)", target.isoformat(), wait)
            await asyncio.sleep(wait)
            if self.running:
                await self._run_agile_daily_update()
                await asyncio.sleep(3600)

    async def _saving_session_loop(self):
        """Poll for saving sessions periodically."""
        interval = self.settings.get("saving_session_poll_interval", 15) * 60
        while self.running:
            await self._check_saving_sessions()
            await asyncio.sleep(interval)

    async def _intelligent_dispatch_loop(self):
        """Poll for Intelligent planned dispatches."""
        interval = self.settings.get("intelligent_poll_interval", 5) * 60
        while self.running:
            await self._run_intelligent_dispatch_check()
            await asyncio.sleep(interval)

    async def stop(self):
        self.running = False
        logger.info("Octopus Energy plugin stopping")
        if self._event_active:
            await self._resume_all()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [Octopus] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    plugin = OctopusPlugin()
    try:
        await plugin.run()
    except asyncio.CancelledError:
        await plugin.stop()


if __name__ == "__main__":
    asyncio.run(main())

