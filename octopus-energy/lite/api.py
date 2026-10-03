# Octopus Energy Lite — REST/GraphQL client + account discovery.

import time

from helpers import _OE_REST, _OE_GQL, _auth, _MAX_PAGES, _parse_iso, _REGION_FROM_MPAN, _classify, _save_state


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


# ---------------------------------------------------------------------------
#  Account discovery
# ---------------------------------------------------------------------------

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
