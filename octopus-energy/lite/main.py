"""Octopus Energy Tariff Optimiser (Lite).

Fetches tariff + rate data from Octopus Energy's public APIs and:

- **Agile**: daily (~16:15 UTC) fetches tomorrow's half-hourly import
  rates, picks the cheapest slots, writes ``charge_slot`` blocks to the
  next day's scheduler.  Optionally fetches Agile Outgoing export rates.
- **Intelligent Go / fixed off-peak**: writes the fixed cheap window to
  every day; polls GraphQL ``plannedDispatches`` and writes in-flight
  smart charges as extra ``charge_slot`` blocks on today.
- **Saving Sessions**: polls ``octoplusAccountInfo`` and exports during
  JOINED events via ``discharge_now``/``quick_resume``.
- **tariff.rates provider**: publishes normalised rate/window data for
  the Energy Planner via ``ctx.register_provider``.

Lite notes vs the full version:

- Scheduler blocks are written in **UK local time** — the full version
  wrote UTC times verbatim, which lands an hour early during BST.
- ``read_schedule``/``write_schedule`` merge: plugin-owned block ids are
  prefixed ``agile_charge_``/``intelli_``; user blocks are preserved.
- Rate fetches retry with exponential backoff inside the tick loop
  (non-blocking) rather than sleeping for hours inside a fetch.
- ESP32 RTC is UTC; ``time.mktime`` differs between platforms, so all
  epoch conversion uses pure-arithmetic civil-date math.
"""

import time

_OE_REST = "https://api.octopus.energy/v1"
_OE_GQL = "https://api.octopus.energy/v1/graphql/"

DAYS = ["monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday"]

_TARIFF_INTELLIGENT = ("INTELLI",)
_TARIFF_AGILE = ("AGILE",)
_TARIFF_GO = ("GO", "E-1R-GO")

_MAX_RESUME_MIN = 240      # quick_settings auto_resume clamp
_REARM_LEAD_S = 300        # re-arm margin for long saving sessions
_MAX_PAGES = 4             # pagination cap (4×48 slots)
_TICK_S = 30               # main loop tick
_DISCOVERY_RETRY_S = 600
_SS_REGION_PREFIX = "_"

# Plugin-owned block prefixes — preserved user blocks on merge.
_AGILE_PREFIX = "agile_charge_"
_INTELLI_PREFIX = "intelli_"
_DISPATCH_PREFIX = "intelli_dispatch_"


# ---------------------------------------------------------------------------
#  Time (RTC = UTC on device → localtime == gmtime; gmtime used so host
#  runs of this source are TZ-deterministic too)
# ---------------------------------------------------------------------------

def _days_from_civil(y, m, d):
    y -= m <= 2
    era = y // 400 if y >= 0 else (y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _parse_iso(s):
    """ISO 8601 UTC timestamp → epoch seconds (None on failure)."""
    if not s or not isinstance(s, str):
        return None
    try:
        s = s.strip()
        tz_off = 0
        if s.endswith("Z"):
            s = s[:-1]
        else:
            for i in range(len(s) - 1, 9, -1):
                c = s[i]
                if c == "+" or c == "-":
                    off = s[i + 1:].replace(":", "")
                    if len(off) >= 4:
                        tz_off = int(off[:2]) * 3600 + int(off[2:4]) * 60
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
                int(tparts[0]) * 3600 + int(tparts[1]) * 60 + sec - tz_off)
    except Exception:
        return None


def _iso(ts):
    t = time.gmtime(int(ts))
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
        t[0], t[1], t[2], t[3], t[4], t[5])


def _uk_dst(epoch):
    """UK DST offset (3600 during BST: last Sun of Mar → Oct)."""
    t = time.gmtime(epoch)
    year = t[0]

    def _last_sunday(y, m):
        for d in range(31, 20, -1):
            lt = time.gmtime(_days_from_civil(y, m, d) * 86400 + 43200)
            if lt[6] == 6:
                return _days_from_civil(y, m, d) * 86400 + 43200
        return 0

    return 3600 if _last_sunday(year, 3) <= epoch < \
        _last_sunday(year, 10) else 0


def _local_hm(epoch):
    """epoch → "HH:MM" in UK local time (what the scheduler compares)."""
    t = time.gmtime(int(epoch) + _uk_dst(epoch))
    return "{:02d}:{:02d}".format(t[3], t[4])


def _local_day(epoch):
    """epoch → weekday name in UK local time."""
    t = time.gmtime(int(epoch) + _uk_dst(epoch))
    return DAYS[t[6]]


def _local_midnight(epoch):
    """UTC epoch of local midnight containing epoch (for day windows)."""
    dst = _uk_dst(epoch)
    t = time.gmtime(int(epoch) + dst)
    return _days_from_civil(t[0], t[1], t[2]) * 86400 - dst


def _b64(s):
    try:
        import ubinascii as u
    except ImportError:
        import binascii as u
    return u.b2a_base64(s.encode()).decode().strip()


def _auth(key):
    """Octopus API: HTTP Basic with the API key as username."""
    return {"Authorization": "Basic " + _b64(key + ":")}


# ---------------------------------------------------------------------------
#  Octopus API
# ---------------------------------------------------------------------------

async def _oe_rest(ctx, settings, path):
    """GET <base>/<path> → parsed JSON dict (raises on failure)."""
    if not path.startswith("/"):
        path = "/" + path
    resp = await ctx.http_get(
        _OE_REST + path, headers=_auth(settings["api_key"]), timeout=25)
    if resp.get("status") != 200:
        raise Exception("Octopus REST {} → {}".format(
            path.split("?")[0], resp.get("status")))
    return resp.get("json") or {}


async def _oe_gql(ctx, settings, query, variables):
    """POST GraphQL → data dict (raises on failure)."""
    resp = await ctx.http_post(
        _OE_GQL, {"query": query, "variables": variables},
        headers=_auth(settings["api_key"]), timeout=25)
    if resp.get("status") != 200:
        raise Exception("Octopus GQL → {}".format(resp.get("status")))
    data = resp.get("json") or {}
    return data.get("data") or {}


async def _fetch_rates(ctx, settings, product, tariff,
                       period_from, period_to, kind="standard-unit-rates"):
    """Paginated rate fetch — returns list of {valid_from, valid_to,
    value_inc_vat} sorted by valid_from."""
    out = []
    url = ("products/{}/electricity-tariffs/{}/{}/?period_from={}"
           "&period_to={}").format(product, tariff, kind,
                                   period_from, period_to)
    for _ in range(_MAX_PAGES):
        data = await _oe_rest(ctx, settings, url)
        out.extend(data.get("results") or [])
        nxt = data.get("next")
        if not nxt:
            break
        # `next` is an absolute URL — keep only the path+query part
        i = nxt.find("/v1/")
        if i < 0:
            break
        url = nxt[i + 3:]
    out.sort(key=lambda r: r.get("valid_from", ""))
    return out


def _product_from_tariff(tariff_code):
    """E-1R-INTELLI-VAR-24-10-29-C → INTELLI-VAR-24-10-29."""
    try:
        parts = tariff_code.split("-")
        if parts[0] == "E" and parts[1].endswith("R"):
            return "-".join(parts[2:-1])
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
#  Account discovery
# ---------------------------------------------------------------------------

_REGION_FROM_MPAN = {"10": "A", "11": "B", "12": "C", "13": "D",
                     "14": "E", "15": "F", "16": "G", "17": "H",
                     "18": "J", "19": "K", "20": "L", "21": "M",
                     "22": "N", "23": "P"}


def _classify(tariff_code):
    u = tariff_code.upper()
    for p in _TARIFF_INTELLIGENT:
        if p in u:
            return "intelligent_go"
    for p in _TARIFF_AGILE:
        if p in u:
            return "agile"
    for p in _TARIFF_GO:
        if p in u:
            return "fixed_offpeak"
    return "standard_variable"


async def _discover(ctx, st, settings):
    """Populate st: mpan, region_code, tariff_code, detected_mode."""
    acc = await _oe_rest(
        ctx, settings, "accounts/{}/".format(settings["account_number"]))
    props = acc.get("properties") or []
    if not props:
        raise Exception("no properties on account")
    points = props[0].get("electricity_meter_points") or []
    if not points:
        raise Exception("no electricity meter points")

    want = settings.get("mpan") or ""
    chosen = None
    for mp in points:
        if want and mp.get("mpan") == want:
            chosen = mp
            break
    if chosen is None:
        for mp in points:
            if not mp.get("is_export"):
                chosen = mp
                break
    chosen = chosen or points[0]

    mpan = chosen.get("mpan", "")
    st["mpan"] = mpan
    st["region_code"] = settings.get("region_code") or \
        _REGION_FROM_MPAN.get(mpan[-10:-8], "C")

    now = time.time()
    for ag in chosen.get("agreements") or []:
        vf = _parse_iso(ag.get("valid_from", ""))
        vt = _parse_iso(ag.get("valid_to", "")) if ag.get("valid_to") \
            else None
        if vf and vf <= now and (vt is None or vt > now):
            st["tariff_code"] = ag.get("tariff_code", "")
            st["detected_mode"] = _classify(st["tariff_code"])
            break
    else:
        st["detected_mode"] = "standard_variable"
        st["tariff_code"] = ""

    ctx.log("discovered: mpan={} region={} tariff={} ({})".format(
        st["mpan"], st["region_code"], st["tariff_code"],
        st["detected_mode"]))
    _save_state(ctx, st)


def _mode(st, settings):
    m = settings.get("tariff_mode", "auto")
    return st.get("detected_mode") or "standard_variable" \
        if m == "auto" else m


def _save_state(ctx, st):
    try:
        old = ctx.load_json("octopus_state", {}) or {}
        ctx.save_json("octopus_state", {
            "mpan": st.get("mpan"), "region_code": st.get("region_code"),
            "tariff_code": st.get("tariff_code"),
            "detected_mode": st.get("detected_mode"),
            "ss_active": bool(st.get("ss_active") or
                              old.get("ss_active")),
            "last_updated": _iso(time.time()),
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Agile — tomorrow's rates → charge_slot blocks
# ---------------------------------------------------------------------------

def _slots_from_rates(rates, n_slots, max_price):
    """Cheapest n half-hour rates (≤max_price if >0) → contiguous
    merged [(start,end)] as "HH:MM" UK local pairs."""
    cheap = sorted(rates, key=lambda r: r.get("value_inc_vat", 999))
    if max_price > 0:
        cheap = [r for r in cheap
                 if r.get("value_inc_vat", 999) <= max_price]
    cheap = cheap[:int(n_slots)]
    if not cheap:
        return []
    spans = []
    for r in cheap:
        s = _parse_iso(r.get("valid_from", ""))
        e = _parse_iso(r.get("valid_to", ""))
        if s and e:
            spans.append((s, e))
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s == merged[-1][1]:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(_local_hm(s), _local_hm(e)) for s, e in merged]


async def _agile_update(ctx, st, settings):
    """Fetch tomorrow's rates, write tomorrow's charge slots.
    Returns True once rates for tomorrow are in hand."""
    tariff = st.get("tariff_code", "")
    product = _product_from_tariff(tariff) or "AGILE-FLEX-22-11-25"
    region = st.get("region_code") or "C"
    full_tariff = "E-1R-{}-{}".format(product, region)

    now = time.time()
    tom0 = _local_midnight(now) + 86400          # tomorrow 00:00 local
    tom1 = tom0 + 86400
    rates = await _fetch_rates(
        ctx, settings, product, full_tariff,
        _iso(tom0), _iso(tom1))
    if len(rates) < 40:
        ctx.log("Agile rates not ready ({} slots)".format(len(rates)),
                "info")
        return False

    # Cache + merge with anything older still in the file
    cache = ctx.load_json("agile_cache", {}) or {}
    seen = {}
    for r in (cache.get("rates") or []) + rates:
        k = r.get("valid_from")
        if k:
            seen[k] = r
    merged = sorted(seen.values(), key=lambda r: r["valid_from"])
    ctx.save_json("agile_cache", {
        "date": _iso(tom0)[:10], "rates": merged,
        "last_updated": _iso(now)})

    # Optional export (Agile Outgoing) rates
    export_rates = []
    if settings.get("enable_export_tariff"):
        prod = settings.get("export_tariff_product",
                            "AGILE-OUTGOING-19-05-13")
        try:
            export_rates = await _fetch_rates(
                ctx, settings, prod,
                "E-1R-{}-{}".format(prod, region),
                _iso(tom0), _iso(tom1), kind="export-payment-rates")
            ctx.save_json("agile_export_cache", {
                "date": _iso(tom0)[:10], "rates": export_rates,
                "last_updated": _iso(now)})
        except Exception as exc:
            ctx.log("export rates unavailable: {}".format(exc), "info")

    # Cheapest slots → tomorrow's schedule
    n = int(settings.get("agile_cheap_slots", 8))
    maxp = float(settings.get("agile_max_price_pence", 15.0) or 0)
    slots = _slots_from_rates(rates, n, maxp)
    if not slots:
        ctx.log("no cheap slots below {}p".format(maxp), "warning")
        return True

    day = _local_day(tom0)
    blocks = _merged_blocks(ctx.read_schedule(day),
                            (_AGILE_PREFIX, _DISPATCH_PREFIX))
    for i, (s, e) in enumerate(slots):
        blocks.append({
            "id": _AGILE_PREFIX + str(i) + "_" + s.replace(":", ""),
            "type": "charge_slot",
            "start_time": s, "end_time": e,
            "settings": {
                "charge_target": int(settings.get(
                    "charge_target_soc", 100)),
                "charge_power": int(settings.get(
                    "charge_power_steps", 50)),
            }})
    await ctx.write_schedule(day, blocks)
    _mark_day(st, day)
    ctx.log("wrote {} agile charge slot(s) for {}".format(
        len(slots), day))

    _publish_rates_display(ctx, st, settings, merged, export_rates,
                           slots)
    return True


def _merged_blocks(sched, drop_prefixes):
    """Existing blocks minus plugin-owned ids."""
    out = []
    for b in (sched or {}).get("blocks") or []:
        bid = b.get("id", "")
        if any(bid.startswith(p) for p in drop_prefixes):
            continue
        out.append(b)
    return out


def _mark_day(st, day):
    """Remember which weekday schedules we wrote agile/dispatch blocks
    into — plugin windows must not re-fire the same weekday next week."""
    days = st.setdefault("write_days", {})
    days[day] = True


async def _clean_other_days(ctx, st):
    """Strip agile/dispatch blocks from previously-written days other
    than today/tomorrow — they were one-off windows."""
    now = time.time()
    keep = (_local_day(now),
            _local_day(_local_midnight(now) + 86400))
    for day in list(st.get("write_days") or {}):
        if day in keep:
            continue
        try:
            sched = ctx.read_schedule(day)
            old = sched.get("blocks") or []
            blocks = _merged_blocks(sched, (_AGILE_PREFIX,
                                            _DISPATCH_PREFIX))
            if len(blocks) != len(old):
                await ctx.write_schedule(day, blocks)
                ctx.log("removed stale plugin block(s) from " + day)
        except Exception as exc:
            ctx.log("stale-block clean {}: {}".format(day, exc),
                    "warning")
        st["write_days"].pop(day, None)


def _publish_rates_display(ctx, st, settings, import_rates,
                           export_rates, slots):
    """rates_display.json — fetched by the plugin frontend."""
    try:
        ctx.save_json("rates_display", {
            "tariff_type": "agile",
            "region": st.get("region_code", ""),
            "last_updated": _iso(time.time()),
            "import_rates": [{
                "start": r.get("valid_from", ""),
                "end": r.get("valid_to", ""),
                "price_pence": r.get("value_inc_vat", 0),
            } for r in import_rates],
            "export_rates": [{
                "start": r.get("valid_from", ""),
                "end": r.get("valid_to", ""),
                "price_pence": r.get("value_inc_vat", 0),
            } for r in export_rates],
            "selected_slots": [{"start": s, "end": e}
                               for s, e in slots],
        })
    except Exception as exc:
        ctx.log("rates_display write failed: {}".format(exc), "warning")


# ---------------------------------------------------------------------------
#  Intelligent / fixed off-peak
# ---------------------------------------------------------------------------

async def _intelli_fixed(ctx, st, settings):
    """Fixed cheap window → intelli_charge slot on every day (once)."""
    start = settings.get("intelligent_go_start", "23:30")
    end = settings.get("intelligent_go_end", "05:30")
    block_id = (_INTELLI_PREFIX + "charge_" +
                start.replace(":", ""))
    for day in DAYS:
        try:
            sched = ctx.read_schedule(day)
            if any(b.get("id") == block_id
                   for b in sched.get("blocks") or []):
                continue          # already written
            blocks = _merged_blocks(sched, (_INTELLI_PREFIX,))
            blocks.append({
                "id": block_id, "type": "charge_slot",
                "start_time": start, "end_time": end,
                "settings": {
                    "charge_target": int(settings.get(
                        "charge_target_soc", 100)),
                    "charge_power": int(settings.get(
                        "charge_power_steps", 50)),
                }})
            await ctx.write_schedule(day, blocks)
        except Exception as exc:
            ctx.log("fixed window write {}: {}".format(day, exc),
                    "error")


async def _intelli_dispatch(ctx, st, settings):
    """GraphQL plannedDispatches → charge slots on today."""
    q = ("query PlannedDispatches($accountNumber: String!) {"
         "  plannedDispatches(accountNumber: $accountNumber) {"
         "    start end source location meta { totalCostAdded }"
         "  } }")
    data = await _oe_gql(ctx, settings, q,
                         {"accountNumber": settings["account_number"]})
    dispatches = data.get("plannedDispatches") or []
    now = time.time()
    mode = settings.get("intelligent_dispatch_mode",
                        "planned_and_started")
    rel = []
    for d in dispatches:
        s = _parse_iso(d.get("start", ""))
        e = _parse_iso(d.get("end", ""))
        if not s or not e:
            continue
        if mode == "started_only" and not (s <= now <= e):
            continue
        if e < now:
            continue
        rel.append((s, e, d))

    ctx.save_json("intelligent_dispatches", {
        "timestamp": _iso(now),
        "dispatches": dispatches,
        "relevant": [{"start": _iso(s), "end": _iso(e)}
                     for s, e, _ in rel],
    })

    today = _local_day(now)
    sched = ctx.read_schedule(today)
    blocks = _merged_blocks(sched, (_DISPATCH_PREFIX,))
    for i, (s, e, d) in enumerate(rel):
        blocks.append({
            "id": _DISPATCH_PREFIX + str(i) + "_" +
            _local_hm(s).replace(":", ""),
            "type": "charge_slot",
            "start_time": _local_hm(s), "end_time": _local_hm(e),
            "settings": {
                "charge_target": int(settings.get(
                    "charge_target_soc", 100)),
                "charge_power": int(settings.get(
                    "charge_power_steps", 50)),
            }})
    if rel or _has_dispatch_blocks(sched):
        await ctx.write_schedule(today, blocks)
        _mark_day(st, today)
        ctx.log("{} intelligent dispatch slot(s) for {}".format(
            len(rel), today))

    # Also publish display data for the frontend
    try:
        ctx.save_json("rates_display", {
            "tariff_type": "intelligent_go",
            "last_updated": _iso(now),
            "fixed_window": {
                "start": settings.get("intelligent_go_start", "23:30"),
                "end": settings.get("intelligent_go_end", "05:30")},
            "planned_dispatches": [{
                "start": d.get("start", ""), "end": d.get("end", ""),
                "source": d.get("source", ""),
                "energy_added": (d.get("meta") or {}).get(
                    "totalCostAdded", 0)} for d in dispatches],
        })
    except Exception:
        pass


def _has_dispatch_blocks(sched):
    for b in (sched or {}).get("blocks") or []:
        if b.get("id", "").startswith(_DISPATCH_PREFIX):
            return True
    return False


# ---------------------------------------------------------------------------
#  Saving sessions → quick-action export
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
#  tariff.rates provider — Energy Planner capability
# ---------------------------------------------------------------------------

def _provider(ctx, st, settings):
    """Provider fn → normalised rates/windows for the planner."""
    mode = _mode(st, settings)
    out = {"tariff": mode, "fetched": st.get("rates_fetched", 0),
           "import": [], "export": [], "cheap_windows": []}
    if mode == "agile":
        cache = ctx.load_json("agile_cache", {}) or {}
        for r in cache.get("rates") or []:
            s = _parse_iso(r.get("valid_from", ""))
            e = _parse_iso(r.get("valid_to", ""))
            if s and e:
                out["import"].append({
                    "start": s, "end": e,
                    "pence": r.get("value_inc_vat", 0)})
        xcache = ctx.load_json("agile_export_cache", {}) or {}
        for r in xcache.get("rates") or []:
            s = _parse_iso(r.get("valid_from", ""))
            e = _parse_iso(r.get("valid_to", ""))
            if s and e:
                out["export"].append({
                    "start": s, "end": e,
                    "pence": r.get("value_inc_vat", 0)})
        out["fetched"] = _parse_iso(
            (cache.get("last_updated") or "")) or 0
    else:
        # Intelligent / fixed off-peak — publish the tariff's own
        # unit-rate schedule (cached daily; 2-tier for Intelligent Go)
        # as `import` slots so the planner can price the day, plus
        # cheap_windows for display.
        std = ctx.load_json("standard_rates_cache", {}) or {}
        cheap_pence = None
        for r in std.get("rates") or []:
            s = _parse_iso(r.get("valid_from", ""))
            e = _parse_iso(r.get("valid_to", ""))
            if s and e:
                p = r.get("value_inc_vat", 0)
                out["import"].append({
                    "start": s, "end": e, "pence": p})
                if cheap_pence is None or p < cheap_pence:
                    cheap_pence = p
        start = settings.get("intelligent_go_start", "23:30")
        end = settings.get("intelligent_go_end", "05:30")
        now = time.time()
        mid = _local_midnight(now)
        try:
            sh, sm = [int(x) for x in start.split(":")]
            eh, em = [int(x) for x in end.split(":")]
            for d in (-86400, 0, 86400):
                s0 = mid + d + sh * 3600 + sm * 60
                e0 = mid + d + eh * 3600 + em * 60
                if e0 <= s0:
                    e0 += 86400
                out["cheap_windows"].append({"start": s0, "end": e0})
        except Exception:
            pass
        disp = ctx.load_json("intelligent_dispatches", {}) or {}
        for d in disp.get("relevant") or []:
            s = _parse_iso(d.get("start", ""))
            e = _parse_iso(d.get("end", ""))
            if s and e:
                out["cheap_windows"].append({"start": s, "end": e})
                # dispatch windows bill at the off-peak rate — expose
                # them as priced import slots for the planner
                if cheap_pence is not None:
                    out["import"].append(
                        {"start": s, "end": e, "pence": cheap_pence})
        out["fetched"] = _parse_iso(disp.get("timestamp") or "") or 0
    return out


# ---------------------------------------------------------------------------
#  Lifecycle
# ---------------------------------------------------------------------------

async def run(ctx):
    st = {"region_code": None, "detected_mode": None,
          "tariff_code": None, "mpan": None,
          "ss_active": False, "ss_event": None, "armed_until": 0,
          "agile_last_day": "", "agile_next_try": 0,
          "agile_delay": 60, "ss_next": 0, "disp_next": 0,
          "discovered": False, "discover_next": 0,
          "fixed_written": False}

    # Restore across restarts
    saved = ctx.load_json("octopus_state", {}) or {}
    for k in ("mpan", "region_code", "tariff_code", "detected_mode"):
        st[k] = saved.get(k)
    st["discovered"] = bool(st.get("mpan"))

    ctx.register_provider("tariff.rates",
                          lambda: _provider(ctx, st, ctx.get_settings()))
    ctx.log("Octopus Energy started", "info")
    ctx.set_status("running", "initialising")

    while True:
        try:
            settings = ctx.get_settings() or {}
            if not settings.get("enabled"):
                ctx.set_status("waiting", "disabled in settings")
                await ctx.sleep_ms(60000)
                continue
            if not settings.get("api_key") or \
                    not settings.get("account_number"):
                ctx.set_status("waiting", "API key/account required")
                await ctx.sleep_ms(60000)
                continue

            now = time.time()
            day_tag_today = _iso(_local_midnight(now))[:10]

            if not st["discovered"]:
                if now >= st["discover_next"]:
                    try:
                        await _discover(ctx, st, settings)
                        st["discovered"] = True
                    except Exception as exc:
                        ctx.log("discovery failed: {}".format(exc),
                                "error")
                        st["discover_next"] = now + _DISCOVERY_RETRY_S
                await ctx.sleep_ms(_TICK_S * 1000)
                continue

            mode = _mode(st, settings)
            ctx.set_status("running", mode)

            # --- fixed cheap window (intelligent / fixed_offpeak) ---
            if mode in ("intelligent_go", "fixed_offpeak") and \
                    not st["fixed_written"]:
                await _intelli_fixed(ctx, st, settings)
                st["fixed_written"] = True

            # --- agile daily rate fetch (after 16:15 UTC) ---
            if mode == "agile":
                lt = time.gmtime(now)
                past_1615 = (lt[3], lt[4]) >= (16, 15)
                day_tag = _iso(_local_midnight(now) + 86400)[:10]
                if past_1615 and st["agile_last_day"] != day_tag and \
                        now >= st["agile_next_try"]:
                    try:
                        if await _agile_update(ctx, st, settings):
                            st["agile_last_day"] = day_tag
                            st["agile_delay"] = int(settings.get(
                                "agile_poll_retry_delay", 60))
                            st["rates_fetched"] = now
                        else:
                            raise Exception("rates not published")
                    except Exception as exc:
                        delay = min(st["agile_delay"],
                                    int(settings.get(
                                        "max_agile_retry_hours", 6))
                                    * 3600)
                        st["agile_next_try"] = now + delay
                        st["agile_delay"] = min(delay * 2, 3600)
                        ctx.log("agile retry in {}s ({})".format(
                            delay, exc), "info")

            # --- non-agile: cache the tariff's own unit rates daily ---
            # For Intelligent/Go the published standard-unit-rates are
            # the real 2-tier schedule — lets the planner price the day.
            if mode != "agile" and st.get("tariff_code") and \
                    st.get("std_rates_day") != day_tag_today:
                prod = _product_from_tariff(st["tariff_code"])
                if prod:
                    try:
                        rates = await _fetch_rates(
                            ctx, settings, prod, st["tariff_code"],
                            _iso(now - 86400), _iso(now + 86400))
                        if rates:
                            ctx.save_json("standard_rates_cache",
                                          {"date": day_tag_today,
                                           "rates": rates})
                            st["std_rates_day"] = day_tag_today
                            st["rates_fetched"] = now
                    except Exception as exc:
                        ctx.log("standard rates fetch: {}".format(exc),
                                "info")

            # --- intelligent dispatch poll ---
            if mode == "intelligent_go" and now >= st["disp_next"]:
                try:
                    await _intelli_dispatch(ctx, st, settings)
                except Exception as exc:
                    ctx.log("dispatch poll: {}".format(exc), "warning")
                st["disp_next"] = now + int(settings.get(
                    "intelligent_poll_interval", 5)) * 60

            # --- stale plugin blocks on other days ---
            if st.get("write_days"):
                await _clean_other_days(ctx, st)

            # --- saving sessions poll ---
            if settings.get("saving_sessions_enabled", True) and \
                    now >= st["ss_next"]:
                try:
                    await _saving_sessions(ctx, st, settings)
                except Exception as exc:
                    ctx.log("saving-session poll: {}".format(exc),
                            "warning")
                st["ss_next"] = now + int(settings.get(
                    "saving_session_poll_interval", 15)) * 60

        except Exception as exc:
            ctx.log("tick error: {}".format(exc), "error")
        await ctx.sleep_ms(_TICK_S * 1000)


async def stop(ctx):
    """Release the inverter if a saving session was active on stop, and
    strip every plugin-owned schedule block — stale charge windows must
    not keep firing while disabled/uninstalled."""
    for day in DAYS:
        try:
            sched = ctx.read_schedule(day)
            old = sched.get("blocks") or []
            blocks = _merged_blocks(sched, (
                _AGILE_PREFIX, _INTELLI_PREFIX, _DISPATCH_PREFIX))
            if len(blocks) != len(old):
                await ctx.write_schedule(day, blocks)
                ctx.log("stop: removed plugin block(s) from " + day)
        except Exception:
            pass
    st = ctx.load_json("octopus_state", {}) or {}
    if st.get("ss_active"):
        ctx.log("stopping mid-saving-session — resuming", "warning")
        try:
            await ctx.quick_resume()
        except Exception as exc:
            ctx.log("shutdown resume failed: {}".format(exc), "error")
