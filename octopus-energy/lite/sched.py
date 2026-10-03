# Octopus Energy Lite — weekday schedule writes (Agile, Intelligent
# fixed window, Intelligent dispatches) + stale-block lifecycle.

import time

from helpers import DAYS, _AGILE_PREFIX, _INTELLI_PREFIX, _DISPATCH_PREFIX, _parse_iso, _iso, _local_hm, _local_day, _local_midnight, _product_from_tariff
from api import _oe_gql, _fetch_rates


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


# ---------------------------------------------------------------------------
#  Plugin-owned block lifecycle
# ---------------------------------------------------------------------------

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
         "    start end delta meta { source location }"
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
                "source": (d.get("meta") or {}).get("source", ""),
                "delta": d.get("delta")} for d in dispatches],
        })
    except Exception:
        pass


def _has_dispatch_blocks(sched):
    for b in (sched or {}).get("blocks") or []:
        if b.get("id", "").startswith(_DISPATCH_PREFIX):
            return True
    return False
