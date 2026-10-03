"""Axle Energy VPP (Lite) — poll the Axle API, drive quick settings.

Polls ``api.axle.energy`` for grid events.  Inside a buffered event
window it fires the host's ``discharge_now`` quick action (which pauses
the scheduler, sets max export, and journals the original registers for
a delta-resume); when the event ends it calls ``quick_resume``.

Differences vs the full version (same semantics, Lite surfaces):

- Inverter access goes through ``ctx.quick_action``/``ctx.quick_resume``
  — Lite is single-inverter so no per-serial loop is needed.
- ``auto_resume_minutes`` is clamped to 240 by quick_settings; long
  events are covered by re-arming the action before the armed deadline
  expires (re-firing is safe: the journal keeps the first captured
  "original" values).
- State/history persist via ``ctx.save_json`` and are read by the
  plugin frontend at ``/api/plugins/axle-energy/data/<file>.json``.
"""

import time

AXLE_API_URL = "https://api.axle.energy/vpp/home-assistant/event"

_MAX_RESUME_MIN = 240        # quick_settings auto_resume clamp
_REARM_LEAD_S = 150          # re-fire this long before the arm expires
_MAX_EVENTS = 100            # history file cap
_DEFAULT_INTERVAL_S = 900    # 15 min


# ---------------------------------------------------------------------------
#  Time helpers (ESP32 RTC is UTC — mktime/localtime are inverse there)
# ---------------------------------------------------------------------------

def _days_from_civil(y, m, d):
    """Days since 1970-01-01 (Howard Hinnant's algorithm).

    Pure arithmetic — identical on CPython and MicroPython, unlike
    ``time.mktime`` which is TZ-sensitive on host and wants a different
    tuple length on each platform.
    """
    y -= m <= 2
    era = y // 400 if y >= 0 else (y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


# MicroPython time.time() counts from 2000-01-01, not the Unix epoch —
# parsed ISO timestamps must be shifted into the same base or event
# windows never match time.time() on device.
_EPOCH_OFFSET = 946684800 if time.gmtime(0)[0] == 2000 else 0


def _parse_iso(s):
    """ISO 8601 ``YYYY-MM-DDTHH:MM:SS[Z|+HH:MM|-HH:MM]`` → epoch (UTC).

    Returns None on any parse failure.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        s = s.strip()
        tz_off = 0
        if s.endswith("Z"):
            s = s[:-1]
        else:
            # trailing +HH:MM / -HH:MM (or +HHMM)
            for i in range(len(s) - 1, 9, -1):
                c = s[i]
                if c == "+" or c == "-":
                    off = s[i + 1:].replace(":", "")
                    if len(off) >= 4:
                        tz_off = (int(off[:2]) * 3600 +
                                  int(off[2:4]) * 60)
                        if c == "-":
                            tz_off = -tz_off
                    s = s[:i]
                    break
                if c == "T":
                    break
        date, tpart = s.split("T")
        y, mo, d = date.split("-")
        tparts = tpart.split(":")
        sec = int(float(tparts[2])) if len(tparts) > 2 else 0
        return (_days_from_civil(int(y), int(mo), int(d)) * 86400 +
                int(tparts[0]) * 3600 + int(tparts[1]) * 60 +
                sec - tz_off - _EPOCH_OFFSET)
    except Exception:
        return None


def _iso(ts):
    t = time.localtime(int(ts))
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
        t[0], t[1], t[2], t[3], t[4], t[5])


def _event_times(event):
    """(start_epoch, end_epoch) or (None, None)."""
    if not event:
        return None, None
    return _parse_iso(event.get("start_time")), \
        _parse_iso(event.get("end_time"))


# ---------------------------------------------------------------------------
#  Persistence — shapes match the full version's axle_state.json /
#  events.json so the shared frontend renders both identically.
# ---------------------------------------------------------------------------

def _save_state(ctx, st, last_poll, interval):
    st["saved"]["event_active"] = st["event_active"]
    st["saved"]["current_event"] = st["current_event"]
    st["saved"]["armed_until"] = st["armed_until"]
    st["saved"]["last_poll_time"] = _iso(last_poll) if last_poll else None
    st["saved"]["next_poll_time"] = \
        _iso(last_poll + interval) if last_poll else None
    st["saved"]["next_poll_interval_seconds"] = interval
    st["saved"]["last_saved"] = _iso(time.time())
    try:
        ctx.save_json("axle_state", st["saved"])
    except Exception:
        pass


def _log_event(ctx, event, action, duration_min=None):
    try:
        events = ctx.load_json("events", []) or []
        events.append({
            "timestamp": _iso(time.time()),
            "action": action,
            "event_start": event.get("start_time"),
            "event_end": event.get("end_time"),
            "import_export": event.get("import_export"),
            "duration_minutes": duration_min,
        })
        ctx.save_json("events", events[-_MAX_EVENTS:])
    except Exception as exc:
        ctx.log("history write failed: {}".format(exc), "warning")


# ---------------------------------------------------------------------------
#  Inverter control
# ---------------------------------------------------------------------------

async def _fire_export(ctx, st, settings, buffered_end):
    """Start/extend discharge_now; arms auto-resume (<=240 min) and an
    SOC floor.  Records the armed deadline so long events re-arm."""
    now = time.time()
    remaining_min = int((buffered_end - now) / 60) + 1
    if remaining_min < 1:
        return False
    minutes = min(_MAX_RESUME_MIN, remaining_min)
    until_soc = settings.get("discharge_target_soc", 4)
    try:
        res = await ctx.quick_action(
            "discharge_now",
            auto_resume_minutes=minutes,
            until_soc=until_soc)
    except Exception as exc:
        ctx.log("discharge_now failed: {}".format(exc), "error")
        return False
    if not res.get("success"):
        ctx.log("discharge_now rejected: {}".format(
            res.get("message", "?")), "error")
        return False
    st["armed_until"] = now + minutes * 60
    ctx.log("export armed for {} min (SOC floor {}%)".format(
        minutes, until_soc))
    return True


async def _resume(ctx, st):
    try:
        res = await ctx.quick_resume()
        ok = not res or res.get("success", True)
    except Exception as exc:
        ctx.log("resume failed: {}".format(exc), "error")
        ok = False
    st["armed_until"] = 0
    return ok


# ---------------------------------------------------------------------------
#  Poll
# ---------------------------------------------------------------------------

async def _fetch_event(ctx, settings):
    """Axle API → (event dict | None, rate_limited bool)."""
    key = settings.get("api_key")
    resp = await ctx.http_get(
        AXLE_API_URL,
        headers={"Authorization": "Bearer {}".format(key),
                 "Accept": "application/json"},
        timeout=25)
    status = resp.get("status", -1)
    if status == 429:
        return None, True
    if status != 200:
        ctx.log("Axle API status {} ({})".format(
            status, resp.get("error", "")), "warning")
        return None, False
    data = resp.get("json")
    if not data or "start_time" not in data:
        return None, False
    return {
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "import_export": data.get("import_export", 0),
        "updated_at": data.get("updated_at") or _iso(time.time()),
    }, False


async def _poll(ctx, st, settings):
    """One poll cycle.  Returns seconds until the next poll."""
    now = time.time()
    normal_s = int(settings.get("poll_interval_normal", 15)) * 60
    fast_s = int(settings.get("poll_interval_fast", 90))
    buffer_min = int(settings.get("event_buffer_minutes", 3))
    fast_window_h = settings.get("fast_poll_window", 1)

    try:
        fast_window_s = int(float(fast_window_h) * 3600)
    except (TypeError, ValueError):
        fast_window_s = 3600

    event, limited = await _fetch_event(ctx, settings)
    st["last_poll"] = now
    # 429 → cumulative exponential backoff (cap 1h), reset on success
    if limited:
        st["backoff"] = min(st.get("backoff", 0) * 2 or 2, 4)
        ctx.log("Axle rate-limited — backing off ×{}".format(
            st["backoff"]), "warning")
    else:
        st["backoff"] = 0
    interval = normal_s * (1 << st["backoff"]) if st["backoff"] \
        else normal_s
    interval = min(interval, 3600)

    start, end = _event_times(event)
    if event and start and end:
        buffered_start = start - buffer_min * 60
        buffered_end = end + buffer_min * 60

        # Fast polling near/inside the buffered window
        if now >= buffered_start - fast_window_s and \
                now <= buffered_end:
            interval = fast_s

        if buffered_start <= now <= buffered_end:
            if not st["event_active"]:
                ctx.log("event active {} → {} — exporting".format(
                    event["start_time"], event["end_time"]))
                if await _fire_export(ctx, st, settings, buffered_end):
                    st["event_active"] = True
                    st["current_event"] = event
                    dur = int((end - start) / 60) + buffer_min * 2
                    _log_event(ctx, event, "started", dur)
                    ctx.set_status("running",
                                   "exporting until " + _iso(end))
            else:
                # Event extension: Axle moved the end later
                _, cur_end = _event_times(st["current_event"])
                if cur_end and end > cur_end:
                    ctx.log("event extended → re-arming export")
                    if await _fire_export(ctx, st, settings,
                                          buffered_end):
                        st["current_event"] = event

        # Auto-resume covers <=240 min; re-arm while still buffered.
        if st["event_active"] and st["armed_until"]:
            if buffered_end > st["armed_until"] and \
                    now >= st["armed_until"] - _REARM_LEAD_S:
                ctx.log("event outlives 240-min resume — re-arming")
                await _fire_export(ctx, st, settings, buffered_end)

    # Event finished (API cleared it or buffered end passed)
    if st["event_active"]:
        _, cur_end = _event_times(st["current_event"])
        expired = cur_end is None or \
            now > cur_end + buffer_min * 60
        if not event or expired:
            ctx.log("event ended — resuming normal operation")
            await _resume(ctx, st)
            if st["current_event"]:
                _log_event(ctx, st["current_event"], "ended")
            st["event_active"] = False
            st["current_event"] = None
            ctx.set_status("running", "idle — polling")

    return max(15, interval)


# ---------------------------------------------------------------------------
#  Lifecycle
# ---------------------------------------------------------------------------

async def run(ctx):
    st = {"event_active": False, "current_event": None,
          "armed_until": 0, "last_poll": None, "backoff": 0,
          "saved": {}}

    # Restore across restarts: if an event was active when the plugin
    # stopped, the next poll inside the window re-arms export.
    saved = ctx.load_json("axle_state", {}) or {}
    st["saved"] = dict(saved)
    st["event_active"] = bool(saved.get("event_active"))
    st["current_event"] = saved.get("current_event")
    st["armed_until"] = saved.get("armed_until") or 0

    ctx.log("Axle Energy VPP started", "info")
    ctx.set_status("running", "polling")

    while True:
        interval = _DEFAULT_INTERVAL_S
        try:
            settings = ctx.get_settings() or {}
            if not settings.get("enabled"):
                ctx.set_status("waiting",
                               "disabled in settings")
            elif not settings.get("api_key"):
                ctx.set_status("waiting", "no API key configured")
            else:
                interval = await _poll(ctx, st, settings)
                _save_state(ctx, st, st["last_poll"], interval)
        except Exception as exc:
            ctx.log("poll error: {}".format(exc), "error")
        await ctx.sleep_ms(interval * 1000)


async def stop(ctx):
    """On shutdown mid-event, release the inverter back to normal."""
    saved = ctx.load_json("axle_state", {}) or {}
    if saved.get("event_active"):
        ctx.log("stopping mid-event — resuming inverter", "warning")
        try:
            await ctx.quick_resume()
        except Exception as exc:
            ctx.log("shutdown resume failed: {}".format(exc), "error")
