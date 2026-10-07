"""Ohme EV Charger (Lite) — live charge telemetry for planner + arbiter.

Polls the Ohme cloud API (the unofficial API the Ohme app uses — same
one dan-r/ohmepy and Home Assistant core build on) and turns what the
charger is actually doing into three universal platform feeds:

* **intent arbitration** — ``hold_discharge`` intents stop the house
  battery discharging into the car.  Reactive (real ``power.watt`` > 0)
  plus planned-window holds for each claimed slot while plugged in —
  PredBat's cheap-window insurance: a claimed slot that starts early,
  telegraphs late or vanishes from telemetry still can't feed the
  battery to the car, and holding costs ~nothing at the cheap rate.
* **``tariff.rates`` provider** — planned charge windows as priced
  import slots at ``ev_slot_rate_pence``, so the planner can co-charge
  the battery alongside the car (gated by the mains-fuse check).
* **``load.adjust`` provider** — delivered-EV energy integrated from
  power samples (Ohme's ``batterySoc.wh`` is the *car's* absolute
  content, not delivered energy — PredBat integrates for the same
  reason), so the historian's load_power can be corrected before the
  planner builds forecasts.

Truth hierarchy (PredBat parity): actual draw > claimed slots.  Slots
are planning hints — the flags ``slot_claimed_no_draw`` /
``draw_no_slot`` in ev_state.json surface mismatches instead of
silently trusting the API.

Secrets: Firebase JWT + refresh token live in ``_session.json``
(private — the data route never serves ``_``-prefixed files).  The
password itself only lives in settings.json, schema-masked.
"""

import time
try:
    import ujson as json
except ImportError:
    import json
try:
    import ubinascii as _binascii
except ImportError:
    import binascii as _binascii

# Embedded public key from dan-r/ohmepy — the same one the official
# Ohme app ships with (Firebase Identity Toolkit is key-by-design).
_GOOGLE_API_KEY = "AIzaSyC8ZeZngm33tpOXLpbXeKfwtyZ1WrkbdBY"
_LOGIN_URL = ("https://www.googleapis.com/identitytoolkit/v3/"
              "relyingparty/verifyPassword?key=" + _GOOGLE_API_KEY)
_REFRESH_URL = ("https://securetoken.googleapis.com/v1/token?key=" +
                _GOOGLE_API_KEY)
_API_BASE = "https://api.ohme.io"

_EPOCH_OFFSET = 946684800 if time.gmtime(0)[0] == 2000 else 0
_TOKEN_REFRESH_S = 2700         # JWT ~1h — refresh after 45 min
_MAX_GAP_S = 600                # refuse energy integration over >10min
_CORR_MAX_AGE_S = 48 * 3600     # load.adjust corrections retention
_EV_FALLBACK_AMPS = 32          # assume a 7kW charger when watt unknown
_DEFAULT_IDLE_S = 300
_DEFAULT_ACTIVE_S = 60
_SESSIONS_MAX = 30              # session history cap for the frontend
_SESSION_GAP_S = 600            # >10min without draw = new session


# ---------------------------------------------------------------------------
#  Pure helpers (host-testable)
# ---------------------------------------------------------------------------

def _status(session):
    """Ohme mode+power → our status enum (mirrors ohmepy ChargerStatus)."""
    mode = (session or {}).get("mode")
    if mode == "PENDING_APPROVAL":
        return "pending_approval"
    if mode == "DISCONNECTED":
        return "unplugged"
    if mode == "STOPPED":
        return "paused"
    if mode == "FINISHED_CHARGE":
        return "finished"
    if ((session or {}).get("power") or {}).get("watt", 0):
        return "charging"
    return "plugged_in"


def _slots(session, offset):
    """allSessionSlots [{startTimeMs,endTimeMs,watts}] → merged
    [{start,end,kw}] in device epochs.  Adjacent slots merge (ohmepy
    does the same for display)."""
    out = []
    for s in (session or {}).get("allSessionSlots") or []:
        try:
            st = int(s["startTimeMs"]) // 1000 - offset
            en = int(s["endTimeMs"]) // 1000 - offset
            kw = float(s.get("watts") or 0) / 1000.0
        except Exception:
            continue
        if en <= st:
            continue
        if out and out[-1]["end"] == st and out[-1]["kw"] == kw:
            out[-1]["end"] = en
        else:
            out.append({"start": st, "end": en, "kw": kw})
    return out


def _iso(ts):
    t = time.localtime(int(ts))
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
        t[0], t[1], t[2], t[3], t[4], t[5])


def _jwt_uid(token):
    """user_id from the Firebase JWT payload (needed for the charge
    summary endpoint; tolerates malformed tokens)."""
    try:
        p = str(token).split(".")[1]
        p += "=" * ((4 - len(p) % 4) % 4)
        dec = getattr(_binascii, "a2b_base64", None)
        payload = dec(p) if dec else _binascii.b64decode(p)
        return (json.loads(payload) or {}).get("user_id")
    except Exception:
        return None


def _integrate(last_ts, watt, now, max_gap=_MAX_GAP_S):
    """Left-Riemann Wh for one sample — refuses long gaps (metering
    dead zones aren't zero-draw; PredBat applies the same rule)."""
    if not last_ts or watt is None:
        return 0.0
    dt = now - last_ts
    if dt <= 0 or dt > max_gap:
        return 0.0
    return float(watt) * dt / 3600.0


# ---------------------------------------------------------------------------
#  Auth — Firebase Identity Toolkit
# ---------------------------------------------------------------------------

def _save_session(ctx, sess):
    try:
        ctx.save_json("_session", sess)
    except Exception:
        pass


async def _login(ctx, settings, sess):
    """Full email+password login → session dict (id/refresh/birth/uid)."""
    resp = await ctx.http_post(_LOGIN_URL, {
        "email": settings.get("email"),
        "password": settings.get("password"),
        "returnSecureToken": True}, timeout=25)
    if resp.get("status") != 200:
        err = (resp.get("json") or {}).get("error", {})
        ctx.log("Ohme login failed ({}): {}".format(
            resp.get("status"), err.get("message", "?")), "warning")
        return False
    j = resp.get("json") or {}
    if not j.get("idToken") or not j.get("refreshToken"):
        ctx.log("Ohme login: malformed token response", "warning")
        return False
    sess["id"] = j["idToken"]
    sess["refresh"] = j["refreshToken"]
    sess["birth"] = time.time()
    sess["uid"] = _jwt_uid(sess["id"])
    _save_session(ctx, sess)
    ctx.log("Ohme login OK")
    return True


async def _ensure_token(ctx, settings, sess):
    """Valid id token guaranteed (or False).  Refresh >45min via the
    stored refresh token; full login when there's nothing to refresh."""
    now = time.time()
    if sess.get("id") and now - sess.get("birth", 0) < _TOKEN_REFRESH_S:
        return True
    if sess.get("refresh"):
        resp = await ctx.http_post(_REFRESH_URL, {
            "grantType": "refresh_token",
            "refreshToken": sess["refresh"]}, timeout=25)
        if resp.get("status") == 200:
            j = resp.get("json") or {}
            if j.get("id_token"):
                sess["id"] = j["id_token"]
                sess["refresh"] = j.get("refresh_token") or sess["refresh"]
                sess["birth"] = now
                if not sess.get("uid"):
                    sess["uid"] = _jwt_uid(sess["id"])
                _save_session(ctx, sess)
                return True
        # Refresh rejected (revoked/expired) → full login path
        sess["id"] = None
        sess["refresh"] = None
    return await _login(ctx, settings, sess)


async def _api(ctx, settings, sess, method, path, obj=None, timeout=25):
    """Authenticated Ohme API call; one re-login on 401.  Returns the
    http_client response dict ({status, json, ...})."""
    for _ in range(2):
        if not await _ensure_token(ctx, settings, sess):
            return {"status": -1, "error": "auth_failed"}
        headers = {
            "Authorization": "Firebase {}".format(sess["id"]),
            "Content-Type": "application/json",
            "User-Agent": "ohmepy/1.9.2"}
        resp = await ctx.http_request(
            method, _API_BASE + path, obj=obj, headers=headers,
            timeout=timeout)
        if resp.get("status") == 401:
            ctx.log("Ohme 401 — re-authenticating", "warning")
            sess["id"] = None            # force re-login next pass
            continue
        return resp
    return {"status": 401, "error": "auth_rejected"}


# ---------------------------------------------------------------------------
#  Poll
# ---------------------------------------------------------------------------

async def _fetch_session(ctx, settings, sess):
    """GET /v1/chargeSessions → session dict (or None on failure).
    Retries CALCULATING/DELIVERING like ohmepy — those modes mean the
    charger is mid-recompute, not a real state."""
    for attempt in range(3):
        resp = await _api(ctx, settings, sess, "GET",
                          "/v1/chargeSessions", timeout=25)
        if resp.get("status") != 200:
            if attempt == 2:
                ctx.log("chargeSessions status {} ({})".format(
                    resp.get("status"), resp.get("error", "")),
                    "warning")
                return None
            continue
        data = resp.get("json")
        if isinstance(data, list) and data:
            session = data[0]
        elif isinstance(data, dict):
            lst = data.get("chargeSessions")
            session = (lst[0] if isinstance(lst, list) and lst
                       else data)
        else:
            return None
        mode = (session or {}).get("mode")
        if mode in ("CALCULATING", "DELIVERING") and attempt < 2:
            await ctx.sleep_ms(1200)
            continue
        return session
    return None


async def _fetch_account(ctx, settings, sess):
    """GET /v1/users/me/account → serial/model/capabilities (cached
    once per plugin start — it only changes on hardware swaps)."""
    resp = await _api(ctx, settings, sess, "GET",
                      "/v1/users/me/account", timeout=25)
    if resp.get("status") != 200:
        return None
    j = resp.get("json") or {}
    devs = j.get("chargeDevices") or []
    if not devs:
        return None
    dev = devs[0]
    return {"serial": dev.get("id"),
            "model": dev.get("modelTypeDisplayName"),
            "firmware": dev.get("firmwareVersionLabel")}


async def _poll(ctx, st, settings):
    """One poll cycle → seconds until the next poll."""
    now = time.time()
    sess = st["sess"]
    session = await _fetch_session(ctx, settings, sess)

    idle_s = int(settings.get("poll_idle_s", _DEFAULT_IDLE_S))
    active_s = int(settings.get("poll_active_s", _DEFAULT_ACTIVE_S))

    if session is None:
        st["last_error"] = "poll failed"
        _save_state(ctx, st, now, idle_s, settings)
        return idle_s

    if not st.get("account"):
        st["account"] = await _fetch_account(ctx, settings, sess) or {}
    acct = st["account"]

    status = _status(session)
    power = session.get("power") or {}
    watt = float(power.get("watt") or 0)
    plugged = status not in ("unplugged",)
    online = bool((session.get("chargerStatus") or {}).get("online"))
    slots = _slots(session, _EPOCH_OFFSET)
    bat = session.get("batterySoc") or {}
    car_soc = bat.get("percent")
    if car_soc is None:
        car_soc = ((session.get("car") or {}).get("batterySoc")
                   or {}).get("percent")

    # --- delivered energy (Riemann; refuses stale gaps) -------------
    # A "session" = continuous draw.  Brief sub-poll dips don't split
    # it — a new session only opens after _SESSION_GAP_S without draw
    # or an unplug→plug transition, so session_wh stays meaningful.
    last_ts = st.get("last_ts")
    gap_s = _SESSION_GAP_S
    was_charging = bool(st.get("charging"))
    if watt > 0:
        if not was_charging and \
                (now - st.get("last_charge_ts", 0) > gap_s or
                 st.get("prev_status") == "unplugged"):
            # genuinely new session — the unmeasured run-up isn't ours
            st["session_wh"] = 0.0
            st["correction_start"] = now
            st["cur_correction_kwh"] = 0.0
        else:
            # continuing session, incl. resuming from a sub-gap dip —
            # the sample's power covers the interval back to the last
            # poll (right-Riemann, same convention as _integrate)
            wh = _integrate(last_ts, watt, now)
            st["session_wh"] += wh
            st["cur_correction_kwh"] += wh / 1000.0
        st["last_charge_ts"] = now
    st["charging"] = watt > 0
    st["last_ts"] = now
    session_open = watt > 0 or \
        (now - st.get("last_charge_ts", 0) <= gap_s and
         st.get("correction_start"))
    _update_correction(ctx, st, now, session_open)
    st["prev_status"] = status

    # --- planned-vs-actual flags (PredBat parity) -------------------
    in_slot = [s for s in slots if s["start"] <= now < s["end"]]
    flags = {
        "slot_claimed_no_draw":
            bool(in_slot) and plugged and watt <= 0,
        "draw_no_slot": watt > 0 and not in_slot,
    }

    # --- intents ----------------------------------------------------
    inside_ct = _bool(settings.get("ev_inside_ct", True))
    hold_on = _bool(settings.get("hold_discharge_while_charging", True))
    plan_hold = _bool(settings.get("hold_during_planned_slots", True))
    soc_floor = int(settings.get("hold_soc_floor", 4))

    if inside_ct and hold_on and watt > 0:
        res = await ctx.declare_intent(
            "hold_discharge", tag="reactive",
            ttl_s=active_s * 2.5, until_soc=soc_floor,
            reason="EV charging {:.1f}kW".format(watt / 1000.0))
        st["hold_reactive"] = True
        st["hold_suppressed"] = res.get("suppressed_by")
    elif st.get("hold_reactive"):
        await ctx.revoke_intent("hold_discharge", tag="reactive")
        st["hold_reactive"] = False
        st["hold_suppressed"] = None

    if inside_ct and plan_hold and plugged:
        wanted = {}
        for s in in_slot:
            wanted[str(int(s["start"]))] = s["end"]
        # declare holds for open windows; drop holds for vanished ones
        for k, end in wanted.items():
            await ctx.declare_intent(
                "hold_discharge", tag="slot:" + k,
                until_epoch=end, until_soc=soc_floor,
                reason="planned EV window")
        for k in list(st.get("slot_holds", {}).keys()):
            if k not in wanted:
                await ctx.revoke_intent("hold_discharge",
                                        tag="slot:" + k)
        st["slot_holds"] = wanted
    elif st.get("slot_holds"):
        for k in list(st["slot_holds"].keys()):
            await ctx.revoke_intent("hold_discharge", tag="slot:" + k)
        st["slot_holds"] = {}

    # --- state file --------------------------------------------------
    st["planned_slots"] = slots
    st["status"] = status
    st["watt"] = watt
    st["last_error"] = None
    _save_state(ctx, st, now, active_s if _is_active(status, in_slot)
                else idle_s, settings, acct, online, plugged, car_soc,
                flags)

    if _is_active(status, in_slot):
        return active_s
    return idle_s


def _is_active(status, in_slot):
    return status in ("charging", "pending_approval") or bool(in_slot)


def _update_correction(ctx, st, now, session_open):
    """Keep ev_energy.json's correction list current: the open session
    window is the last entry (``open: true``) and gets rewritten each
    poll so the planner corrects today's load mid-session, not only at
    session end.  Once the draw gap exceeds the session threshold the
    entry closes and a session-history record is written once."""
    start = st.get("correction_start")
    kwh = st.get("cur_correction_kwh") or 0.0
    prev_close = st.get("corr_closed_start")
    try:
        en = ctx.load_json("ev_energy", {}) or {}
        corr = en.get("corrections") or []
        last = corr[-1] if corr and corr[-1].get("open") else None
        if session_open and start and kwh > 0.005:
            if last is None or last.get("start") != int(start):
                # A previous session's open entry must not linger —
                # e.g. a restart mid-session orphans it; close it so
                # its measured kWh still feeds the planner.
                for c in corr:
                    if c.get("open"):
                        c["open"] = False
                last = {"start": int(start), "open": True}
                corr.append(last)
            last["end"] = int(now)
            last["kwh"] = round(kwh, 4)
        elif last is not None and last.get("start") != prev_close:
            last["open"] = False
            st["corr_closed_start"] = last.get("start")
            try:
                hist = ctx.load_json("ev_sessions", []) or []
                hist.append({"start": last.get("start"),
                             "end": last.get("end"),
                             "kwh": round(last.get("kwh") or 0, 3)})
                ctx.save_json("ev_sessions", hist[-_SESSIONS_MAX:])
            except Exception:
                pass
        cutoff = now - _CORR_MAX_AGE_S
        en["corrections"] = [c for c in corr
                             if (c.get("end") or 0) >= cutoff][-200:]
        ctx.save_json("ev_energy", en)
    except Exception:
        pass


def _bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)


def _save_state(ctx, st, last_poll, interval, settings, acct=None,
                online=None, plugged=None, car_soc=None, flags=None):
    out = {
        "status": st.get("status"),
        "watts": st.get("watt", 0),
        "session_wh": round(st.get("session_wh") or 0.0, 1),
        "car_soc_pct": car_soc,
        "plugged": plugged,
        "charger_online": online,
        "serial": (acct or {}).get("serial"),
        "model": (acct or {}).get("model"),
        "firmware": (acct or {}).get("firmware"),
        "planned_slots": [{"start": s["start"], "end": s["end"],
                           "kw": s.get("kw", 0),
                           "start_iso": _iso(s["start"]),
                           "end_iso": _iso(s["end"])}
                          for s in (st.get("planned_slots") or [])],
        "hold": {"reactive": bool(st.get("hold_reactive")),
                 "suppressed_by": st.get("hold_suppressed"),
                 "slots": st.get("slot_holds") or {}},
        "flags": flags or {"slot_claimed_no_draw": False,
                           "draw_no_slot": False},
        "last_poll": _iso(last_poll) if last_poll else None,
        "next_poll": _iso(last_poll + interval)
        if last_poll else None,
        "last_error": st.get("last_error"),
        "saved": _iso(time.time()),
    }
    try:
        ctx.save_json("ev_state", out)
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Providers — pull model: the planner calls these; they only read files
# ---------------------------------------------------------------------------

def _register_providers(ctx, st):
    ctx.register_provider(
        "tariff.rates", lambda: _rates_provider(ctx, st))
    ctx.register_provider(
        "load.adjust", lambda: _adjust_provider(ctx, st))


def _rates_provider(ctx, st):
    """Planned EV windows → cheap import slots for the planner.

    Only publishes when cocharging is on, the supply-fuse check passes,
    and the car is plugged in (unless publish_slots_unplugged — Octopus
    dispatches can outlive the connection)."""
    out = {"import": [], "export": [], "cheap_windows": []}
    try:
        settings = st.get("settings") or {}
        ev = ctx.load_json("ev_state", {}) or {}
        slots = ev.get("planned_slots") or []
        if not slots:
            return out
        now = time.time()
        if not ev.get("plugged") and \
                not _bool(settings.get("publish_slots_unplugged")):
            return out
        for s in slots:
            if s.get("end", 0) <= now:
                continue
            out["cheap_windows"].append(
                {"start": s["start"], "end": s["end"]})
        if not _bool(settings.get("cocharge_during_slots")):
            return out
        # Mains-fuse check: EV draw + reserved house headroom must
        # leave room for the inverter's own charge rate.
        supply = float(settings.get("supply_limit_amps", 60))
        headroom = float(settings.get("headroom_amps", 22))
        ev_amps = float(ev.get("watts") or 0) / 230.0
        if ev_amps < 1:
            ev_amps = _EV_FALLBACK_AMPS
        if ev_amps + headroom > supply:
            return out
        pence = float(settings.get("ev_slot_rate_pence", 7.0))
        for s in slots:
            if s.get("end", 0) <= now:
                continue
            out["import"].append({"start": s["start"],
                                  "end": s["end"], "pence": pence})
        return out
    except Exception:
        return out


def _adjust_provider(ctx, st):
    """Delivered-EV energy corrections for the planner's load history
    (only meaningful when the charger sits inside the CT clamp)."""
    try:
        settings = st.get("settings") or {}
        if not _bool(settings.get("ev_inside_ct", True)):
            return {"corrections": []}
        en = ctx.load_json("ev_energy", {}) or {}
        return {"corrections": en.get("corrections") or []}
    except Exception:
        return {"corrections": []}


# ---------------------------------------------------------------------------
#  RPC — /api/plugins/ohme-ev/action
# ---------------------------------------------------------------------------

async def on_action(ctx, action, payload):
    if action == "approve":
        acct = _get_account(ctx)
        serial = (acct or {}).get("serial")
        if not serial:
            return {"success": False,
                    "error": "charger serial unknown — poll first"}
        settings = ctx.get_settings() or {}
        sess = ctx.load_json("_session", {}) or {}
        resp = await _api(ctx, settings, sess, "PUT",
                          "/v1/chargeSessions/{}/approve"
                          "?approve=true".format(serial))
        _save_session(ctx, sess)
        return {"success": resp.get("status") in (200, 204),
                "status": resp.get("status"),
                "error": resp.get("error")}
    if action == "refresh":
        st = _LOOP_STATE.get("st")
        if st is not None:
            st["wake"] = True
        return {"success": True, "message": "poll scheduled"}
    return {"success": False, "error": "unknown action"}


def _get_account(ctx):
    try:
        return _LOOP_STATE.get("st", {}).get("account")
    except Exception:
        return None


_LOOP_STATE = {"st": None}


# ---------------------------------------------------------------------------
#  Lifecycle
# ---------------------------------------------------------------------------

async def run(ctx):
    st = {"sess": ctx.load_json("_session", {}) or {},
          "account": None, "charging": False, "last_ts": 0,
          "session_wh": 0.0, "cur_correction_kwh": 0.0,
          "correction_start": 0, "slot_holds": {},
          "hold_reactive": False, "hold_suppressed": None,
          "planned_slots": [], "status": None, "watt": 0.0,
          "last_error": None, "wake": False}
    _LOOP_STATE["st"] = st
    _register_providers(ctx, st)
    ctx.log("Ohme EV charger plugin started")
    ctx.set_status("running", "polling")

    while True:
        interval = _DEFAULT_IDLE_S
        try:
            settings = ctx.get_settings() or {}
            st["settings"] = settings
            if not settings.get("enabled"):
                ctx.set_status("waiting", "disabled in settings")
            elif not settings.get("email") or \
                    not settings.get("password"):
                ctx.set_status("waiting", "no credentials configured")
            else:
                interval = await _poll(ctx, st, settings)
                ctx.set_status("running", "polling")
        except Exception as exc:
            ctx.log("poll error: {}".format(exc), "error")
        # Chunked sleep so the on_action "refresh" wakes us promptly.
        waited = 0.0
        while waited < interval and not st.get("wake"):
            step = min(10.0, interval - waited)
            await ctx.sleep_ms(int(step * 1000))
            waited += step
        st["wake"] = False


async def stop(ctx):
    """Release our intents on shutdown — revoke_all in the supervisor
    covers crashes, but a clean stop should tidy up itself."""
    try:
        await ctx.revoke_intent()
    except Exception:
        pass
