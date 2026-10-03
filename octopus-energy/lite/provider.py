# Octopus Energy Lite — tariff.rates provider for the Energy Planner.

import time

from helpers import _mode, _parse_iso, _local_midnight


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
