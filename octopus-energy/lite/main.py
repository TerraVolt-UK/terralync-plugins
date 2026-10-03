# Octopus Energy Lite — entry point.
#
# Split into local modules so each compile unit stays small: the loader
# compiles source in-RAM and the emit buffer must fit the largest
# contiguous heap block (~46 KB on a busy 2 MB PSRAM board).

import time

from helpers import DAYS, _TICK_S, _DISCOVERY_RETRY_S, _AGILE_PREFIX, _INTELLI_PREFIX, _DISPATCH_PREFIX, _parse_iso, _iso, _uk_dst, _local_hm, _local_day, _local_midnight, _days_from_civil, _product_from_tariff, _classify, _mode, _save_state, _synth_two_tier
from api import _oe_rest, _oe_gql, _fetch_rates, _discover
from sched import _slots_from_rates, _agile_update, _merged_blocks, _mark_day, _clean_other_days, _publish_rates_display, _intelli_fixed, _intelli_dispatch, _has_dispatch_blocks
from sessions import _region_match, _saving_sessions, _persist_ss, _log_ss
from provider import _provider


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

            # Discovery ran but found no agreement — usually means the
            # RTC was pre-NTP at boot. Re-discover in 2 min.
            if st.get("detected_mode") == "standard_variable" and \
                    not st.get("tariff_code"):
                st["discovered"] = False
                st["discover_next"] = now + 120
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
                        if not rates:
                            # Four-rate products (IOG, small-business)
                            # publish no standard-unit-rates — only flat
                            # day/night figures.  Synthesise the 2-tier
                            # schedule the provider + planner consume.
                            day_r = await _fetch_rates(
                                ctx, settings, prod, st["tariff_code"],
                                _iso(now - 86400), _iso(now + 86400),
                                kind="day-unit-rates")
                            night_r = await _fetch_rates(
                                ctx, settings, prod, st["tariff_code"],
                                _iso(now - 86400), _iso(now + 86400),
                                kind="night-unit-rates")
                            if day_r and night_r:
                                dp = day_r[-1].get("value_inc_vat", 0)
                                np_ = night_r[-1].get("value_inc_vat", 0)
                                rates = _synth_two_tier(
                                    dp, np_,
                                    settings.get("intelligent_go_start",
                                                 "23:30"),
                                    settings.get("intelligent_go_end",
                                                 "05:30"), now)
                                if rates:
                                    ctx.log("2-tier rates synthesised "
                                            "({}p day / {}p night)"
                                            .format(dp, np_), "info")
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
