# Octopus Energy Lite — shared constants, time and auth helpers.
# All modules keep sources small: the plugin loader compiles each file
# separately and MicroPython's emit buffer must fit in the largest
# contiguous heap block (~46 KB on a busy 2 MB PSRAM board).

import time

_OE_REST = "https://api.octopus.energy/v1"
_OE_GQL = "https://api.octopus.energy/v1/graphql/"

DAYS = ["monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday"]

_TARIFF_INTELLIGENT = ("INTELLI", "IOG")
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
#  Tariff classification
# ---------------------------------------------------------------------------

def _product_from_tariff(tariff_code):
    """E-1R-INTELLI-VAR-24-10-29-C → INTELLI-VAR-24-10-29."""
    try:
        parts = tariff_code.split("-")
        if parts[0] == "E" and parts[1].endswith("R"):
            return "-".join(parts[2:-1])
    except Exception:
        pass
    return ""


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
