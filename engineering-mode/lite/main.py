"""Engineering Mode (Lite) — raw holding-register writes.

⚠️  DANGEROUS OPERATIONS — incorrect use can void warranty, damage
equipment, or violate grid regulations.

The dashboard's Engineering tab posts commands to
``POST /api/engineering/command``; the webserver proxies them to this
plugin's ``on_action``.  Every write goes through
``ctx.raw_register_write`` which enforces the manager-held unlock
token, the manifest's ``engineering.registers`` allowlist + bounds,
verified Modbus write, and audit logging — all owned by the host, not
this file.

This module is intentionally tiny: validation bounds mirror the full
version's ``engineering_commands.py`` but the dangerous encodings are
expressed as a static table so no request-building code is needed.
"""

# Certification codes packed into HR(2) high byte — mirrors the full
# version's cert_map (register.py Certification enum order).
CERT_CODES = {
    "VDE_0126": 0, "VDE_0126_2": 1, "EN_50549": 2, "AS4777_A": 3,
    "CEI_0_21": 4, "MAURITIUS": 5, "XINA_1": 6, "VDE_AR_N_4105": 7,
    "G98": 8, "NETHERLANDS": 9, "CQC": 10, "POLAND": 11, "G99": 12,
    "BELGIUM": 13, "CQC_1": 14, "NORTHERN_IRELAND": 15, "G98_NI": 16,
    "G99_NI": 17, "NRS_097": 18, "NEW_ZEALAND": 19, "AS4777_B": 20,
    "AS4777_C": 21, "SWEDEN": 22, "FINLAND": 23, "DENMARK_1": 24,
    "ROMANIA": 25, "CZECH": 26, "SPAIN": 27, "DENMARK_2": 28,
}

# Commissioning presets: name -> (HR2 value, HR5 value).  The display
# metadata (region, families, descriptions) lives in the dashboard's
# config-manager; this table only needs the expected register values —
# mirrors the full version's COMMISSIONING_PRESETS expected_hr2/hr5.
PRESETS = {
    # UK
    "UK_G98_3000W": (0x081E, 30000), "UK_G98_3600W": (0x0824, 36000),
    "UK_G98_4600W": (0x082E, 46000), "UK_G99_5000W": (0x0C32, 50000),
    "UK_G99_6000W": (0x0C3C, 60000), "UK_G99_8000W": (0x0C50, 80000),
    "UK_G99_10000W": (0x0C64, 100000),
    "UK_G99_11000W": (0x0C6E, 110000),
    "UK_G99_12000W": (0x0C78, 120000),
    "UK_G99_15000W": (0x0C96, 150000),
    "UK_G99_20000W": (0x0CC8, 200000),
    # Northern Ireland
    "NI_G98_3000W": (0x101E, 30000), "NI_G98_3600W": (0x1024, 36000),
    "NI_G99_5000W": (0x1132, 50000), "NI_G99_6000W": (0x113C, 60000),
    # EU
    "EU_EN50549_3600W": (0x0224, 36000),
    "EU_EN50549_4600W": (0x022E, 46000),
    "EU_EN50549_5000W": (0x0232, 50000),
    "EU_EN50549_6000W": (0x023C, 60000),
    # Germany
    "DE_VDE4105_3600W": (0x0724, 36000),
    "DE_VDE4105_5000W": (0x0732, 50000),
    "DE_VDE4105_6000W": (0x073C, 60000),
    # Italy
    "IT_CEI021_3600W": (0x0424, 36000),
    "IT_CEI021_5000W": (0x0432, 50000),
    "IT_CEI021_6000W": (0x043C, 60000),
    # Australia
    "AU_AS4777A_3600W": (0x0324, 36000),
    "AU_AS4777A_4600W": (0x032E, 46000),
    "AU_AS4777A_5000W": (0x0332, 50000),
    "AU_AS4777A_6000W": (0x033C, 60000),
    "AU_AS4777A_7000W": (0x0346, 70000),
    "AU_AS4777A_8000W": (0x0350, 80000),
    "AU_AS4777B_3600W": (0x1424, 36000),
    "AU_AS4777B_4600W": (0x142E, 46000),
    "AU_AS4777B_5000W": (0x1432, 50000),
    "AU_AS4777B_6000W": (0x143C, 60000),
    "AU_AS4777B_7000W": (0x1446, 70000),
    "AU_AS4777B_8000W": (0x1450, 80000),
    "AU_AS4777C_5000W": (0x1532, 50000),
    # South Africa
    "ZA_NRS097_3600W": (0x1224, 36000),
    "ZA_NRS097_5000W": (0x1232, 50000),
    "ZA_NRS097_6000W": (0x123C, 60000),
    # Netherlands / Belgium / Poland / Sweden / Finland / Spain / NZ
    "NL_3600W": (0x0924, 36000), "NL_5000W": (0x0932, 50000),
    "BE_3600W": (0x0D24, 36000), "BE_5000W": (0x0D32, 50000),
    "PL_3600W": (0x0B24, 36000), "PL_5000W": (0x0B32, 50000),
    "SE_3600W": (0x1624, 36000), "SE_5000W": (0x1632, 50000),
    "FI_3600W": (0x1724, 36000), "FI_5000W": (0x1732, 50000),
    "ES_3600W": (0x1B24, 36000), "ES_5000W": (0x1B32, 50000),
    "ES_6000W": (0x1B3C, 60000),
    "NZ_3600W": (0x1324, 36000), "NZ_5000W": (0x1332, 50000),
}

# Boolean (0/1) commands -> register.
_BOOL_COMMANDS = {
    "reverse_ct_clamp": 42,
    "reverse_em115_meter": 48,
    "reverse_em418_meter": 49,
    "auto_detect_battery": 58,
    "enable_6kw_export": 126,
    "force_enable_battery_bms": 175,
    "enable_g100_limit": 178,
}

# Enum (0/1) commands -> register.
_ENUM_COMMANDS = {
    "meter_type": 47,       # 0=CT/EM418, 1=EM115
    "battery_type": 54,     # 0=Lead Acid, 1=Lithium
    "bms_type": 109,        # 0=Others, 1=GivEnergy
}


def _bool_val(v):
    if isinstance(v, str):
        return 0 if v.strip().lower() in ("0", "false", "off", "no") else 1
    return 1 if v else 0


def _writes(command, value):
    """Map (command, value) -> [(register, raw_value), ...].

    Raises ValueError with a user-facing message on bad input.  Bounds
    here mirror the full version's set_* validators; the manifest
    allowlist applies a second, coarser bound per register.
    """
    if command == "inverter_config":
        v = int(value)
        if not 0 <= v <= 65535:
            raise ValueError("inverter config must be 0-65535")
        cert = (v >> 8) & 0xFF
        if cert > 28:
            raise ValueError(
                "invalid certification code {} ({})".format(
                    cert, hex(cert)))
        pwr = (v & 0xFF) * 100
        if not 1000 <= pwr <= 10000:
            raise ValueError(
                "power rating {}W must be 1000-10000W".format(pwr))
        return [(2, v)]

    if command == "certification_and_power":
        if not isinstance(value, dict):
            raise ValueError("expects {certification, power}")
        cert = str(value.get("certification", "G99"))
        power = int(value.get("power", 5000))
        if cert not in CERT_CODES:
            raise ValueError("invalid certification '{}'".format(cert))
        if not 1000 <= power <= 10000:
            raise ValueError(
                "power rating {}W must be 1000-10000W".format(power))
        return [(2, (CERT_CODES[cert] << 8) | (power // 100))]

    if command == "inverter_ac_rating":
        v = int(value)
        # HR(5) stores watts*10 in a u16 — 6553W is the representable max.
        if not 0 <= v <= 6553:
            raise ValueError(
                "AC power rating {}W must be 0-6553W".format(v))
        return [(5, v * 10)]

    if command == "export_power_limit":
        v = int(value)
        if not 0 <= v <= 24000:
            raise ValueError(
                "export power limit {}W must be 0-24000W".format(v))
        return [(26, v)]

    if command in _BOOL_COMMANDS:
        return [(_BOOL_COMMANDS[command], _bool_val(value))]

    if command in _ENUM_COMMANDS:
        v = int(value)
        if v not in (0, 1):
            raise ValueError("{} must be 0 or 1".format(command))
        return [(_ENUM_COMMANDS[command], v)]

    if command == "battery_nominal_capacity":
        v = int(value)
        if not 1 <= v <= 1000:
            raise ValueError(
                "battery capacity {}Ah must be 1-1000Ah".format(v))
        return [(55, v)]

    if command == "pv_start_voltage":
        v = int(value)
        if not 80 <= v <= 600:
            raise ValueError(
                "PV start voltage {}V must be 80-600V".format(v))
        return [(60, v * 10)]

    if command == "serial_number":
        s = str(value)
        if len(s) != 10:
            raise ValueError("serial number must be exactly 10 chars")
        for c in s:
            if not 32 <= ord(c) <= 126:
                raise ValueError("serial has non-printable characters")
        return [(13 + i, (ord(s[2 * i]) << 8) | ord(s[2 * i + 1]))
                for i in range(5)]

    # ── Multi-write quick commands (order matters: HR2 reboots) ──
    if command == "uprate_to_5kw":
        # HR(5) first (no reboot), then HR(2) G99+5kW (reboots).
        return [(5, 50000), (2, 0x0C32)]

    if command == "downrate_to_3600w":
        # HR(5) first (no reboot), then HR(2) G98+3.6kW (reboots).
        return [(5, 36000), (2, 0x0824)]

    if command == "reapply_ac_rating_after_downrate":
        return [(5, 36000)]

    if command == "commission_preset":
        name = value.get("preset") if isinstance(value, dict) \
            else str(value)
        p = PRESETS.get(name)
        if p is None:
            raise ValueError("unknown preset '{}'".format(name))
        # Commission order: HR(2) first, then HR(5) — matches the
        # full version's commission_inverter_preset.
        return [(2, p[0]), (5, p[1])]

    raise ValueError("unknown engineering command '{}'".format(command))


async def _execute_writes(ctx, writes):
    """Write each (register, value) — verified + audited via ctx.

    Small inter-write delay: HR(2) triggers an inverter reboot, so
    writes after it may legitimately fail; the caller sees the error.
    """
    for reg, val in writes:
        await ctx.raw_register_write(reg, val)
        if len(writes) > 1:
            await ctx.sleep_ms(250)


async def on_action(ctx, action, payload):
    """Handle /api/engineering/command {command, value} dispatches."""
    # SOC calibration uses the named-write policy, not a raw register.
    if action == "soc_force_adjust":
        try:
            v = int(payload)
        except (TypeError, ValueError):
            return {"success": False,
                    "message": "soc_force_adjust expects 0, 1 or 3"}
        try:
            await ctx.write_commands([{
                "command": "set_calibrate_battery_soc",
                "params": {"val": v}}])
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        return {"success": True,
                "message": "Battery SOC calibration {} sent".format(v)}

    try:
        writes = _writes(action, payload)
    except (TypeError, ValueError) as exc:
        return {"success": False,
                "message": "Validation error: {}".format(exc)}

    ctx.log("ENGINEERING {} -> {}".format(action, writes), "warning")
    try:
        await _execute_writes(ctx, writes)
    except Exception as exc:
        return {"success": False, "message": str(exc)}
    return {"success": True,
            "message": "Engineering command {} executed".format(action),
            "requests_count": len(writes)}


async def run(ctx):
    """Keepalive — the plugin's work is all in on_action dispatch."""
    ctx.set_status("running", "ready — commands via Engineering tab")
    ctx.log("Engineering Mode started; commands dispatch via "
            "/api/engineering/command", "info")
    while True:
        await ctx.sleep_ms(30000)
