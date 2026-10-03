# Octopus Energy Lite — Saving Sessions → quick-action export.

import time

from helpers import _SS_REGION_PREFIX, _MAX_RESUME_MIN, _REARM_LEAD_S, _parse_iso, _iso
from api import _oe_gql

_GQL_SESSIONS = (
    "query OctoplusAccountInfo($accountNumber: String!) {"
    "  octoplusAccountInfo(accountNumber: $accountNumber) {"
    "    availableEvents { id startAt endAt incentiveRate targetRegions }"
    "    joinedEvents { id startAt endAt incentiveRate targetRegions }"
    "  } }")


def _region_match(ev, region):
    tr = ev.get("targetRegions") or []
    return not tr or (_SS_REGION_PREFIX + region) in tr


async def _saving_sessions(ctx, st, settings):
    """Poll joined sessions; export while one is active."""
    data = await _oe_gql(ctx, settings, _GQL_SESSIONS,
                         {"accountNumber": settings["account_number"]})
    info = data.get("octoplusAccountInfo") or {}
    now = time.time()
    region = st.get("region_code") or "C"

    avail = [e for e in info.get("availableEvents") or []
             if _region_match(e, region)
             and (_parse_iso(e.get("startAt", "")) or 0) > now]
    if avail:
        ctx.log("{} saving session(s) available — join via Octopus "
                "app".format(len(avail)), "info")

    active = None
    for ev in info.get("joinedEvents") or []:
        if not _region_match(ev, region):
            continue
        s = _parse_iso(ev.get("startAt", ""))
        e = _parse_iso(ev.get("endAt", ""))
        if s and e and s <= now <= e:
            active = (ev, e)
            break

    if active and not st.get("ss_active"):
        ev, end_ts = active
        remain_min = int((end_ts - now) / 60) + 1
        mins = min(_MAX_RESUME_MIN, max(30, remain_min))
        res = await ctx.quick_action(
            "discharge_now", auto_resume_minutes=mins,
            until_soc=int(settings.get("saving_session_min_soc", 10)))
        if res.get("success"):
            st["ss_active"] = True
            st["ss_event"] = ev
            st["armed_until"] = now + mins * 60
            _persist_ss(ctx, st)
            _log_ss(ctx, ev, "started")
            ctx.log("saving session export armed {} min".format(mins))
    elif active and st.get("ss_active"):
        # Sessions >240min: re-arm before the armed resume fires
        ev, end_ts = active
        if end_ts > st.get("armed_until", 0) and \
                now >= st["armed_until"] - _REARM_LEAD_S:
            remain_min = int((end_ts - now) / 60) + 1
            mins = min(_MAX_RESUME_MIN, remain_min)
            res = await ctx.quick_action(
                "discharge_now", auto_resume_minutes=mins,
                until_soc=int(settings.get(
                    "saving_session_min_soc", 10)))
            if res.get("success"):
                st["armed_until"] = now + mins * 60
                st["ss_event"] = ev
    elif not active and st.get("ss_active"):
        try:
            await ctx.quick_resume()
        except Exception as exc:
            ctx.log("session resume failed: {}".format(exc), "error")
        st["ss_active"] = False
        st["armed_until"] = 0
        if st.get("ss_event"):
            _log_ss(ctx, st["ss_event"], "ended")
        st["ss_event"] = None
        _persist_ss(ctx, st)
        ctx.log("saving session ended — resumed")


def _persist_ss(ctx, st):
    """Persist ss_active so stop() can decide whether to resume."""
    try:
        s = ctx.load_json("octopus_state", {}) or {}
        s["ss_active"] = bool(st.get("ss_active"))
        ctx.save_json("octopus_state", s)
    except Exception:
        pass


def _log_ss(ctx, ev, action):
    try:
        data = ctx.load_json("saving_sessions", {}) or {}
        events = data.get("events") or []
        events.append({
            "timestamp": _iso(time.time()), "action": action,
            "event_start": ev.get("startAt"), "event_end": ev.get("endAt"),
            "incentive_rate": ev.get("incentiveRate")})
        ctx.save_json("saving_sessions", {"events": events[-100:]})
    except Exception:
        pass
