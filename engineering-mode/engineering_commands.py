"""
Engineering Commands for GivEnergy Inverters
=============================================

⚠️  WARNING: DANGEROUS OPERATIONS ⚠️
====================================

This module contains commands that modify critical inverter configuration.
These commands can:
- Void your warranty
- Damage equipment
- Violate grid regulations
- Cause safety hazards

DO NOT SHIP THIS FILE IN PRODUCTION BUILDS.

This file should be:
1. Excluded from production releases
2. Only used by trained personnel
3. Used with full understanding of risks

Register Reference:
- HR(2): Inverter Config (certification + power rating)
- HR(5): AC Power Rating (0-12000W, deci format: value * 10)
- HR(26): Export Power Limit (0-20000W)
- HR(42): Reverse CT Clamp (0-1)
- HR(47): Meter Type (0=CT/EM418, 1=EM115)
- HR(48): Reverse EM115 Meter CT (0-1)
- HR(49): Reverse EM418 Meter CT (0-1)
- HR(54): Battery Type (0=Lead Acid, 1=Lithium)
- HR(55): Battery Nominal Capacity (Ah)
- HR(58): Auto Detect Battery Chemistry (0-1)
- HR(60): PV Start Voltage (V, raw value /10)
- HR(109): BMS Type (0=Others, 1=Givenergy)
- HR(126): Enable over 6kW Export (0-1)
- HR(175): Force Enable Battery BMS (0-1)
- HR(178): Enable G100 Limit (0-1)
"""

import logging
from enum import IntEnum
from typing import Optional

from givenergy_modbus_async.pdu import WriteHoldingRegisterRequest, TransparentRequest

_logger = logging.getLogger(__name__)


# =============================================================================
# ENGINEERING REGISTER MAP
# =============================================================================

class EngineeringRegister:
    """Engineering-level holding register addresses."""
    INVERTER_CONFIG = 2           # HR(2) - Certification + Power Rating
    INVERTER_AC_RATING = 5        # HR(5) - AC Power Rating
    EXPORT_POWER_LIMIT = 26       # HR(26) - Grid Export Limit
    REVERSE_CT_CLAMP = 42         # HR(42) - CT Clamp Direction
    METER_TYPE = 47               # HR(47) - Meter Type Selection
    REVERSE_EM115_METER = 48      # HR(48) - EM115 Meter CT Direction
    REVERSE_EM418_METER = 49      # HR(49) - EM418 Meter CT Direction
    BATTERY_TYPE = 54             # HR(54) - Battery Chemistry
    BATTERY_NOMINAL_CAPACITY = 55 # HR(55) - Battery Capacity (Ah)
    AUTO_DETECT_BATTERY = 58      # HR(58) - Auto Detect Battery Type
    PV_START_VOLTAGE = 60         # HR(60) - PV Start Voltage
    BMS_TYPE = 109                # HR(109) - BMS Type Selection
    ENABLE_6KW_EXPORT = 126       # HR(126) - Enable >6kW Export
    FORCE_ENABLE_BATTERY_BMS = 175 # HR(175) - Force Enable Battery
    ENABLE_G100_LIMIT = 178       # HR(178) - G100 Export Limit
    SERIAL_NUMBER_1 = 13          # HR(13) - Serial Number chars 1-2
    SERIAL_NUMBER_2 = 14          # HR(14) - Serial Number chars 3-4
    SERIAL_NUMBER_3 = 15          # HR(15) - Serial Number chars 5-6
    SERIAL_NUMBER_4 = 16          # HR(16) - Serial Number chars 7-8
    SERIAL_NUMBER_5 = 17          # HR(17) - Serial Number chars 9-10


class Certification(IntEnum):
    """Grid certification types."""
    UNKNOWN = 0
    G98 = 8       # UK <3.68kW
    G99 = 12      # UK >3.68kW
    G98_NI = 16   # Northern Ireland <3.68kW
    G99_NI = 17   # Northern Ireland >3.68kW


class MeterType(IntEnum):
    """Energy meter types."""
    CT_EM418 = 0
    EM115 = 1


class BatteryType(IntEnum):
    """Battery chemistry types."""
    LEAD_ACID = 0
    LITHIUM = 1


class BMSType(IntEnum):
    """BMS communication types."""
    OTHERS = 0
    GIVENERGY = 1


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _engineering_warning(operation: str, register: int, value: int) -> None:
    """Log engineering operation warning."""
    _logger.warning("=" * 60)
    _logger.warning("⚠️  ENGINEERING MODE OPERATION ⚠️")
    _logger.warning(f"Operation: {operation}")
    _logger.warning(f"Register: HR({register})")
    _logger.warning(f"Value: {value}")
    _logger.warning("=" * 60)


def _write_engineering_register(register: int, value: int, operation: str) -> list[TransparentRequest]:
    """Write to an engineering register with warning."""
    _engineering_warning(operation, register, value)
    return [WriteHoldingRegisterRequest(register, value)]


# =============================================================================
# 1. INVERTER CONFIG (HR2) - Certification + Power Rating
# =============================================================================

# Valid certification codes (0-28)
VALID_CERTIFICATIONS = set(range(29))

# Valid power ratings in watts that can be encoded in HR(2) lower byte (power / 100).
# Derived from known DTC codes in register.py Converter.inverter_max_power().
# HR(2) lower byte = power_watts // 100, so only multiples of 100 are valid.
VALID_POWER_RATINGS = {3000, 3600, 4600, 5000, 6000, 7000, 8000, 10000, 11000, 12000, 15000, 20000}


def set_inverter_config(value: int) -> list[TransparentRequest]:
    """
    Write raw value to HR(2) - Inverter Configuration.
    
    ⚠️ EXTREMELY DANGEROUS - Changes certification and power rating!
    
    Format: 0xCCPP where:
    - CC = Certification code (00-1C, see Certification enum)
    - PP = Power rating / 100 (e.g., 32 = 5000W, 24 = 3600W)
    
    Common Examples:
    - G98 3.6kW: 0x0824 = 2084
    - G99 5.0kW: 0x0C32 = 3122
    - G98_NI 3.6kW: 0x1024 = 4132
    - G99_NI 5.0kW: 0x1132 = 4402
    
    Validation enforces:
    - Certification must be 0-28 (valid grid codes)
    - Power rating must be 1000-10000W in 100W increments
    """
    if not 0 <= value <= 65535:
        raise ValueError(f"Inverter config value ({value}) must be 0-65535")
    
    # Extract and validate certification (upper byte)
    cert_code = (value >> 8) & 0xFF
    if cert_code not in VALID_CERTIFICATIONS:
        raise ValueError(f"Invalid certification code {cert_code} (0x{cert_code:02X}). Valid: 0-28")
    
    # Extract and validate power rating (lower byte * 100 = watts)
    power_code = value & 0xFF
    power_watts = power_code * 100
    if not 1000 <= power_watts <= 10000:
        raise ValueError(f"Invalid power rating {power_watts}W. Must be 1000-10000W")
    
    _logger.warning(f"⚠️ HR(2) WRITE: Cert={cert_code}, Power={power_watts}W, Raw=0x{value:04X} ({value})")
    
    return _write_engineering_register(
        EngineeringRegister.INVERTER_CONFIG, value,
        "Set Inverter Configuration (Certification + Power)"
    )


def set_certification_and_power(certification: str, power_watts: int) -> list[TransparentRequest]:
    """
    Set certification type and internal power rating.
    
    ⚠️ EXTREMELY DANGEROUS - Changes grid certification!
    
    Args:
        certification: Grid certification code name (e.g., 'G98', 'G99', 'VDE_0126', etc.)
        power_watts: Internal power rating (typically 3600 or 5000)
    """
    # Complete certification map matching Certification enum in register.py
    cert_map = {
        'VDE_0126': 0,        # Germany - VDE 0126
        'VDE_0126_2': 1,      # Germany - VDE 0126-2
        'EN_50549': 2,        # EU - EN 50549
        'AS4777_A': 3,        # Australia - AS4777 Type A
        'CEI_0_21': 4,        # Italy - CEI 0-21
        'MAURITIUS': 5,       # Mauritius
        'XINA_1': 6,          # China - Xina 1
        'VDE_AR_N_4105': 7,   # Germany - VDE-AR-N 4105
        'G98': 8,             # UK - G98 (<=3.68kW single phase)
        'NETHERLANDS': 9,     # Netherlands
        'CQC': 10,            # China - CQC
        'POLAND': 11,         # Poland
        'G99': 12,            # UK - G99 (>3.68kW or 3-phase)
        'BELGIUM': 13,        # Belgium
        'CQC_1': 14,          # China - CQC 1
        'NORTHERN_IRELAND': 15,  # Northern Ireland (legacy)
        'G98_NI': 16,         # Northern Ireland - G98 equivalent
        'G99_NI': 17,         # Northern Ireland - G99 equivalent
        'NRS_097': 18,        # South Africa - NRS 097
        'NEW_ZEALAND': 19,    # New Zealand
        'AS4777_B': 20,       # Australia - AS4777 Type B
        'AS4777_C': 21,       # Australia - AS4777 Type C
        'SWEDEN': 22,         # Sweden
        'FINLAND': 23,        # Finland
        'DENMARK_1': 24,      # Denmark 1
        'ROMANIA': 25,        # Romania
        'CZECH': 26,          # Czech Republic
        'SPAIN': 27,          # Spain
        'DENMARK_2': 28,      # Denmark 2
    }
    
    if certification not in cert_map:
        raise ValueError(f"Invalid certification: {certification}. Valid: {list(cert_map.keys())}")
    if not 1000 <= power_watts <= 10000:
        raise ValueError(f"Power rating ({power_watts}W) must be 1000-10000W")
    
    cert_hex = cert_map[certification]
    power_hex = power_watts // 100
    packed_value = (cert_hex << 8) | power_hex
    
    _logger.critical(f"⚠️ CERTIFICATION CHANGE: {certification}, Power: {power_watts}W, Raw: 0x{packed_value:04X}")
    return set_inverter_config(packed_value)


# =============================================================================
# 2. AC POWER RATING (HR5) - Independent AC Rating
# =============================================================================

def set_inverter_ac_rating(power_watts: int) -> list[TransparentRequest]:
    """
    Set AC power rating independently of HR(2).
    
    ⚠️ DANGEROUS - Changes grid export capacity!
    
    Args:
        power_watts: AC power rating in watts (0-12000W)
        
    Note: Register uses deci format (value * 10)
    """
    if not 0 <= power_watts <= 12000:
        raise ValueError(f"AC power rating ({power_watts}W) must be 0-12000W")
    
    raw_value = power_watts * 10  # deci format
    return _write_engineering_register(
        EngineeringRegister.INVERTER_AC_RATING, raw_value,
        f"Set AC Power Rating to {power_watts}W"
    )


# =============================================================================
# 3. EXPORT POWER LIMIT (HR26)
# =============================================================================

def set_export_power_limit(power_watts: int) -> list[TransparentRequest]:
    """
    Set grid export power limit.
    
    Args:
        power_watts: Export limit in watts (0-24000W, supports multiple inverters)
    """
    if not 0 <= power_watts <= 24000:
        raise ValueError(f"Export power limit ({power_watts}W) must be 0-24000W")
    
    return _write_engineering_register(
        EngineeringRegister.EXPORT_POWER_LIMIT, power_watts,
        f"Set Export Power Limit to {power_watts}W"
    )


# =============================================================================
# 4. REVERSE CT CLAMP (HR42)
# =============================================================================

def set_reverse_ct_clamp(enabled: bool) -> list[TransparentRequest]:
    """
    Reverse CT clamp polarity.
    
    Args:
        enabled: True = Negative (reversed), False = Positive (normal)
    """
    value = 1 if enabled else 0
    return _write_engineering_register(
        EngineeringRegister.REVERSE_CT_CLAMP, value,
        f"Set CT Clamp Direction: {'Reversed' if enabled else 'Normal'}"
    )


# =============================================================================
# 5. METER TYPE (HR47)
# =============================================================================

def set_meter_type(meter_type: MeterType) -> list[TransparentRequest]:
    """
    Set energy meter type.
    
    Args:
        meter_type: MeterType.CT_EM418 (0) or MeterType.EM115 (1)
    """
    return _write_engineering_register(
        EngineeringRegister.METER_TYPE, int(meter_type),
        f"Set Meter Type: {meter_type.name}"
    )


# =============================================================================
# 6. REVERSE EM115 METER CT (HR48)
# =============================================================================

def set_reverse_em115_meter(enabled: bool) -> list[TransparentRequest]:
    """
    Reverse EM115 meter CT polarity.
    
    Args:
        enabled: True = Negative (reversed), False = Positive (normal)
    """
    value = 1 if enabled else 0
    return _write_engineering_register(
        EngineeringRegister.REVERSE_EM115_METER, value,
        f"Set EM115 Meter Direction: {'Reversed' if enabled else 'Normal'}"
    )


# =============================================================================
# 7. REVERSE EM418 METER CT (HR49)
# =============================================================================

def set_reverse_em418_meter(enabled: bool) -> list[TransparentRequest]:
    """
    Reverse EM418 meter CT polarity.
    
    Args:
        enabled: True = Negative (reversed), False = Positive (normal)
    """
    value = 1 if enabled else 0
    return _write_engineering_register(
        EngineeringRegister.REVERSE_EM418_METER, value,
        f"Set EM418 Meter Direction: {'Reversed' if enabled else 'Normal'}"
    )


# =============================================================================
# 8. BATTERY TYPE (HR54)
# =============================================================================

def set_battery_type(battery_type: BatteryType) -> list[TransparentRequest]:
    """
    Set battery chemistry type.
    
    ⚠️ DANGEROUS - Incorrect setting can damage batteries!
    
    Args:
        battery_type: BatteryType.LEAD_ACID (0) or BatteryType.LITHIUM (1)
    """
    return _write_engineering_register(
        EngineeringRegister.BATTERY_TYPE, int(battery_type),
        f"Set Battery Type: {battery_type.name}"
    )


# =============================================================================
# 9. BATTERY NOMINAL CAPACITY (HR55)
# =============================================================================

def set_battery_nominal_capacity(capacity_ah: int) -> list[TransparentRequest]:
    """
    Set battery nominal capacity.
    
    ⚠️ DANGEROUS - Incorrect value affects SOC calculations!
    
    Args:
        capacity_ah: Battery capacity in Amp-hours (1-1000)
    """
    if not 1 <= capacity_ah <= 1000:
        raise ValueError(f"Battery capacity ({capacity_ah}Ah) must be 1-1000Ah")
    
    return _write_engineering_register(
        EngineeringRegister.BATTERY_NOMINAL_CAPACITY, capacity_ah,
        f"Set Battery Capacity to {capacity_ah}Ah"
    )


# =============================================================================
# 10. AUTO DETECT BATTERY CHEMISTRY (HR58)
# =============================================================================

def set_auto_detect_battery(enabled: bool) -> list[TransparentRequest]:
    """
    Enable/disable automatic battery type detection.
    
    Args:
        enabled: True = Auto-detect, False = Manual
    """
    value = 1 if enabled else 0
    return _write_engineering_register(
        EngineeringRegister.AUTO_DETECT_BATTERY, value,
        f"Set Auto Detect Battery: {'Enabled' if enabled else 'Disabled'}"
    )


# =============================================================================
# 11. PV START VOLTAGE (HR60)
# =============================================================================

def set_pv_start_voltage(voltage: int) -> list[TransparentRequest]:
    """
    Set PV input start voltage.
    
    Args:
        voltage: Start voltage in volts (typically 120-500V)
        
    Note: Register uses deci format - raw value is voltage * 10
          Display: raw 1500 → 150V, raw 800 → 80V
    """
    if not 80 <= voltage <= 600:
        raise ValueError(f"PV start voltage ({voltage}V) must be 80-600V")
    
    # Register uses deci format (value / 10 when reading)
    # So we must store voltage * 10 for correct register value
    raw_value = voltage * 10
    return _write_engineering_register(
        EngineeringRegister.PV_START_VOLTAGE, raw_value,
        f"Set PV Start Voltage to {voltage}V (raw: {raw_value})"
    )


# =============================================================================
# 12. BMS TYPE (HR109)
# =============================================================================

def set_bms_type(bms_type: BMSType) -> list[TransparentRequest]:
    """
    Set BMS communication type.
    
    Args:
        bms_type: BMSType.OTHERS (0) or BMSType.GIVENERGY (1)
    """
    return _write_engineering_register(
        EngineeringRegister.BMS_TYPE, int(bms_type),
        f"Set BMS Type: {bms_type.name}"
    )


# =============================================================================
# 13. ENABLE OVER 6KW EXPORT (HR126)
# =============================================================================

def set_enable_6kw_export(enabled: bool) -> list[TransparentRequest]:
    """
    Enable system for >6kW export (CEI021 compliance).
    
    Args:
        enabled: True = Enable >6kW, False = Disable
    """
    value = 1 if enabled else 0
    return _write_engineering_register(
        EngineeringRegister.ENABLE_6KW_EXPORT, value,
        f"Set >6kW Export: {'Enabled' if enabled else 'Disabled'}"
    )


# =============================================================================
# 14. FORCE ENABLE BATTERY BMS (HR175)
# =============================================================================

def set_force_enable_battery_bms(enabled: bool) -> list[TransparentRequest]:
    """
    Force enable battery with PV or Grid.
    
    ⚠️ DANGEROUS - Can wake battery unexpectedly!
    
    Args:
        enabled: True = Force enable, False = Normal operation
    """
    value = 1 if enabled else 0
    return _write_engineering_register(
        EngineeringRegister.FORCE_ENABLE_BATTERY_BMS, value,
        f"Set Force Enable Battery BMS: {'Enabled' if enabled else 'Disabled'}"
    )


# =============================================================================
# 15. ENABLE G100 LIMIT (HR178)
# =============================================================================

def set_enable_g100_limit(enabled: bool) -> list[TransparentRequest]:
    """
    Enable G100 export limitation.
    
    Args:
        enabled: True = Enable G100 limit, False = Disable
    """
    value = 1 if enabled else 0
    return _write_engineering_register(
        EngineeringRegister.ENABLE_G100_LIMIT, value,
        f"Set G100 Limit: {'Enabled' if enabled else 'Disabled'}"
    )


# =============================================================================
# 16. INVERTER SERIAL NUMBER (HR13-17)
# =============================================================================

def set_serial_number(serial: str) -> list[TransparentRequest]:
    """
    Set inverter serial number.
    
    ⚠️ CRITICAL - Changes inverter identity!
    
    The serial number is stored across 5 registers (HR13-17), each holding 2 characters.
    Total length must be exactly 10 characters.
    
    Args:
        serial: 10-character serial number (e.g., 'SD1234G123')
        
    Format:
        - HR(13): Characters 1-2 (high byte = char1, low byte = char2)
        - HR(14): Characters 3-4
        - HR(15): Characters 5-6
        - HR(16): Characters 7-8
        - HR(17): Characters 9-10
    """
    if not isinstance(serial, str):
        raise ValueError(f"Serial number must be a string, got {type(serial)}")
    
    if len(serial) != 10:
        raise ValueError(f"Serial number must be exactly 10 characters, got {len(serial)}")
    
    # Validate all characters are printable ASCII
    if not all(32 <= ord(c) <= 126 for c in serial):
        raise ValueError("Serial number contains non-printable ASCII characters")
    
    # Convert string to register values
    # Each register holds 2 characters: (char1 << 8) | char2
    registers = [
        EngineeringRegister.SERIAL_NUMBER_1,
        EngineeringRegister.SERIAL_NUMBER_2,
        EngineeringRegister.SERIAL_NUMBER_3,
        EngineeringRegister.SERIAL_NUMBER_4,
        EngineeringRegister.SERIAL_NUMBER_5,
    ]
    
    requests = []
    for i, reg in enumerate(registers):
        char1 = serial[i * 2]
        char2 = serial[i * 2 + 1]
        value = (ord(char1) << 8) | ord(char2)
        
        _logger.warning(f"⚠️ HR({reg}) WRITE: Serial chars {i*2+1}-{i*2+2} = '{char1}{char2}' (0x{value:04X})")
        requests.append(WriteHoldingRegisterRequest(reg, value))
    
    _logger.critical(f"⚠️ SERIAL NUMBER CHANGE: '{serial}'")
    return requests


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def uprate_gen1_to_5kw() -> list[TransparentRequest]:
    """
    Uprate a Gen1 Hybrid inverter from 3.6kW to 5kW.
    
    ⚠️ EXTREMELY DANGEROUS - Warranty voiding operation!
    
    This sets:
    - HR(5): 5000W AC rating (FIRST - doesn't cause reboot)
    - HR(2): G99 certification + 5000W internal power (SECOND - causes reboot)
    
    Note: HR(5) must be set BEFORE HR(2) as HR(2) triggers an immediate reboot.
    """
    _logger.critical("⚠️ UPRATING GEN1 INVERTER TO 5KW - WARRANTY VOIDING OPERATION!")
    requests = []
    # Set AC rating FIRST (no reboot)
    requests.extend(set_inverter_ac_rating(5000))
    # Set certification SECOND (causes reboot)
    requests.extend(set_certification_and_power('G99', 5000))
    return requests


def downrate_gen1_to_3600w() -> list[TransparentRequest]:
    """
    Downrate a Gen1 Hybrid inverter from 5kW to 3.6kW.
    
    ⚠️ EXTREMELY DANGEROUS - Warranty voiding operation!
    
    This sets:
    - HR(5): 3600W AC rating (FIRST - doesn't cause reboot)
    - HR(2): G98 certification + 3600W internal power (SECOND - causes reboot)
    
    Note: HR(5) must be set BEFORE HR(2) as HR(2) triggers an immediate reboot.
    The inverter may reset HR(5) during boot, so it should be re-applied after reboot.
    """
    _logger.critical("⚠️ DOWNRATING GEN1 INVERTER TO 3.6KW - WARRANTY VOIDING OPERATION!")
    requests = []
    # Set AC rating FIRST (no reboot)
    requests.extend(set_inverter_ac_rating(3600))
    # Set certification SECOND (causes reboot)
    requests.extend(set_certification_and_power('G98', 3600))
    return requests


def reapply_ac_rating_after_downrate() -> list[TransparentRequest]:
    """
    Re-apply AC rating after inverter reboot from downrate operation.
    
    The inverter may reset HR(5) during boot, so we re-apply it after reconnect.
    This should only be called after the inverter has successfully rebooted.
    """
    _logger.warning("Re-applying AC rating after inverter reboot")
    return set_inverter_ac_rating(3600)


# =============================================================================
# COMMISSIONING SYSTEM
# =============================================================================
#
# Commissioning Flow:
#   1. Set HR(2) = (Certification_Code << 8) | (Power_Watts / 100)
#   2. Set HR(5) = Power_Watts * 10  (deciwatts)
#   3. Inverter reboots — HR(0) auto-updates to correct DTC
#   4. Some registers may reset to defaults — verify settings after reboot
#
# HR(0) is READ-ONLY (write returns 0x86 rejection).
# The DTC auto-derives from the HR(2) + HR(5) combination.
#
# Known DTC patterns (HR(0) hex):
#   0x20xx = Hybrid (Gen1/2/3, differentiated by firmware)
#   0x21xx = Polar
#   0x22xx = Gen3+
#   0x23xx = PV
#   0x30xx = AC Coupled
#   0x40xx = 3PH Hybrid
#   0x41xx = AIO Commercial
#   0x50xx = EMS
#   0x60xx = AC 3PH
#   0x80xx = AIO
#   0x81xx = HV Gen3
#   0x82xx = AIO Hybrid
#   0x83xx = Gen4
#
# Known DTC → Power mappings:
#   0x2001 = Hybrid 5kW    0x2003 = Hybrid 3.6kW
#   0x2201 = Gen3+ 5.4kW   0x3001 = AC3 3kW
#   0x3002 = AC3 3kW (v2)  0x8001 = AIO 6kW
#   0x8102 = HV Gen3 8kW   0x8103 = HV Gen3 10kW
#

# Inverter family → valid power tiers (watts)
# Derived from known DTC codes, battery_max_power, and inverter_max_power mappings.
# Key = Model enum value string (matches register.py Model StrEnum)
# 'unknown' allows all tiers (for uncommissioned units where the user must know)
INVERTER_FAMILY_POWER_TIERS = {
    # Derived from register.py Converter.inverter_max_power() DTC mapping.
    # Only lists powers that are valid commissioning targets (not legacy 4600W).
    'HYBRID_GEN1':       [3600, 5000],          # DTC 2003→3.6k, 2001→5k
    'HYBRID_GEN2':       [3600, 5000],          # DTC 2003→3.6k, 2001→5k (fw-differentiated)
    'HYBRID_GEN3':       [3600, 5000],          # DTC 2003→3.6k, 2001→5k (fw-differentiated)
    'POLAR':             [3600, 5000, 6000],    # DTC 2103→3.6k, 2101→5k, 2104→6k
    'HYBRID_GEN3_PLUS':  [3600, 4600, 5000, 6000, 7000, 8000],    # DTC 2203→3.6k, 2202→4.6k, 2201→5k, 2204→6k, 2205→7k, 2206→8k (Australian variants)
    'AC':                [3000, 3600],           # DTC 3001→3k, 3002→3.6k (3.6kW unverified — no known product)
    'ALL_IN_ONE':        [3600, 4600, 5000, 6000],    # DTC 8002→3.6k, 8004→4.6k, 8003→5k, 8001→6k (AIO1 series)
    'HYBRID_HV_GEN3':   [6000, 8000, 10000],    # DTC 8101→6k, 8102→8k, 8103→10k
    'HYBRID_GEN4':       [3600, 4600, 5000, 6000],    # DTC 8301→3.6k, 8302→4.6k, 8303→5k, 8304→6k
    'ALL_IN_ONE_HYBRID': [6000, 8000, 10000, 11000, 12000],    # DTC 8201→6k, 8202→8k, 8203→10k, 8204→12k, 8205→11k
    'HYBRID_3PH':        [6000, 8000, 10000, 11000, 15000, 20000],    # DTC 4001→6k, 4002→8k, 4003→10k, 4004→11k, 4005→15k, 4006→20k
    'AC_3PH':            [6000, 8000, 10000, 11000, 15000, 20000],    # DTC 60xx — same range as 3PH Hybrid
    'PV':                [3600, 5000, 6000],     # DTC 2303→3.6k, 2301→5k, 2304→6k
    'UNKNOWN':           [3000, 3600, 5000, 6000, 8000, 10000, 11000, 12000, 15000, 20000],  # all valid tiers
}

# Human-readable labels for the UI
INVERTER_FAMILY_LABELS = {
    'HYBRID_GEN1':       'Hybrid Gen1',
    'HYBRID_GEN2':       'Hybrid Gen2',
    'HYBRID_GEN3':       'Hybrid Gen3',
    'POLAR':             'Polar',
    'HYBRID_GEN3_PLUS':  'Gen3+',
    'AC':                'AC Coupled (3.6kW unverified)',
    'ALL_IN_ONE':        'All-In-One',
    'HYBRID_HV_GEN3':   'HV Gen3',
    'HYBRID_GEN4':       'Gen4',
    'ALL_IN_ONE_HYBRID': 'AIO Hybrid',
    'HYBRID_3PH':        '3-Phase Hybrid',
    'AC_3PH':            'AC 3-Phase',
    'PV':                'PV Only',
    'UNKNOWN':           'Unknown / Uncommissioned',
}


# Commissioning presets: keyed by a descriptive name
# Each preset defines the certification, internal power (W), and AC rating (W)
# that will be written to HR(2) and HR(5) respectively.
# 'families' lists which inverter families this preset is valid for.
COMMISSIONING_PRESETS = {
    # ── UK ───────────────────────────────────────────────────────────
    'UK_G98_3000W': {
        'certification': 'G98',
        'power_watts': 3000,
        'ac_rating_watts': 3000,
        'description': 'UK G98 3kW (AC Coupled)',
        'region': 'UK',
        'expected_hr2': 0x081E,   # (8 << 8) | 30
        'expected_hr5': 30000,
        'families': ['AC'],
    },
    'UK_G98_3600W': {
        'certification': 'G98',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'UK G98 3.6kW',
        'region': 'UK',
        'expected_hr2': 0x0824,   # (8 << 8) | 36
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV', 'ALL_IN_ONE'],
    },
    'UK_G98_4600W': {
        'certification': 'G98',
        'power_watts': 4600,
        'ac_rating_watts': 4600,
        'description': 'UK G98 4.6kW (AIO1)',
        'region': 'UK',
        'expected_hr2': 0x082E,
        'expected_hr5': 46000,
        'families': ['ALL_IN_ONE'],
    },
    'UK_G99_5000W': {
        'certification': 'G99',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'UK G99 5kW',
        'region': 'UK',
        'expected_hr2': 0x0C32,   # (12 << 8) | 50
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'HYBRID_3PH', 'PV', 'ALL_IN_ONE'],
    },
    'UK_G99_6000W': {
        'certification': 'G99',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'UK G99 6kW',
        'region': 'UK',
        'expected_hr2': 0x0C3C,   # (12 << 8) | 60
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    'UK_G99_8000W': {
        'certification': 'G99',
        'power_watts': 8000,
        'ac_rating_watts': 8000,
        'description': 'UK G99 8kW',
        'region': 'UK',
        'expected_hr2': 0x0C50,   # (12 << 8) | 80
        'expected_hr5': 80000,
        'families': ['HYBRID_HV_GEN3', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH'],
    },
    'UK_G99_10000W': {
        'certification': 'G99',
        'power_watts': 10000,
        'ac_rating_watts': 10000,
        'description': 'UK G99 10kW',
        'region': 'UK',
        'expected_hr2': 0x0C64,   # (12 << 8) | 100
        'expected_hr5': 100000,
        'families': ['HYBRID_HV_GEN3', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH'],
    },
    'UK_G99_11000W': {
        'certification': 'G99',
        'power_watts': 11000,
        'ac_rating_watts': 11000,
        'description': 'UK G99 11kW',
        'region': 'UK',
        'expected_hr2': 0x0C6E,   # (12 << 8) | 110
        'expected_hr5': 110000,
        'families': ['HYBRID_3PH', 'AC_3PH'],
    },
    'UK_G99_12000W': {
        'certification': 'G99',
        'power_watts': 12000,
        'ac_rating_watts': 12000,
        'description': 'UK G99 12kW',
        'region': 'UK',
        'expected_hr2': 0x0C78,   # (12 << 8) | 120
        'expected_hr5': 120000,
        'families': ['ALL_IN_ONE_HYBRID'],
    },
    'UK_G99_15000W': {
        'certification': 'G99',
        'power_watts': 15000,
        'ac_rating_watts': 15000,
        'description': 'UK G99 15kW',
        'region': 'UK',
        'expected_hr2': 0x0C96,   # (12 << 8) | 150
        'expected_hr5': 150000,
        'families': ['HYBRID_3PH', 'AC_3PH'],
    },
    'UK_G99_20000W': {
        'certification': 'G99',
        'power_watts': 20000,
        'ac_rating_watts': 20000,
        'description': 'UK G99 20kW',
        'region': 'UK',
        'expected_hr2': 0x0CC8,   # (12 << 8) | 200
        'expected_hr5': 200000,
        'families': ['HYBRID_3PH', 'AC_3PH'],
    },
    # ── Northern Ireland ─────────────────────────────────────────────
    'NI_G98_3000W': {
        'certification': 'G98_NI',
        'power_watts': 3000,
        'ac_rating_watts': 3000,
        'description': 'NI G98 3kW (AC Coupled)',
        'region': 'Northern Ireland',
        'expected_hr2': 0x101E,   # (16 << 8) | 30
        'expected_hr5': 30000,
        'families': ['AC'],
    },
    'NI_G98_3600W': {
        'certification': 'G98_NI',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'NI G98 3.6kW',
        'region': 'Northern Ireland',
        'expected_hr2': 0x1024,   # (16 << 8) | 36
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'NI_G99_5000W': {
        'certification': 'G99_NI',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'NI G99 5kW',
        'region': 'Northern Ireland',
        'expected_hr2': 0x1132,   # (17 << 8) | 50
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'NI_G99_6000W': {
        'certification': 'G99_NI',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'NI G99 6kW',
        'region': 'Northern Ireland',
        'expected_hr2': 0x113C,   # (17 << 8) | 60
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    # ── EU (EN 50549) ────────────────────────────────────────────────
    'EU_EN50549_3600W': {
        'certification': 'EN_50549',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'EU EN 50549 3.6kW',
        'region': 'EU',
        'expected_hr2': 0x0224,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'EU_EN50549_5000W': {
        'certification': 'EN_50549',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'EU EN 50549 5kW',
        'region': 'EU',
        'expected_hr2': 0x0232,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'EU_EN50549_6000W': {
        'certification': 'EN_50549',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'EU EN 50549 6kW',
        'region': 'EU',
        'expected_hr2': 0x023C,
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    # ── Gen4 Specific (4.6kW model) ─────────────────────────────────────
    'UK_G98_4600W': {
        'certification': 'G98',
        'power_watts': 4600,
        'ac_rating_watts': 4600,
        'description': 'UK G98 4.6kW (Gen4)',
        'region': 'UK',
        'expected_hr2': 0x082E,   # (8 << 8) | 46
        'expected_hr5': 46000,
        'families': ['HYBRID_GEN4'],
    },
    'EU_EN50549_4600W': {
        'certification': 'EN_50549',
        'power_watts': 4600,
        'ac_rating_watts': 4600,
        'description': 'EU EN 50549 4.6kW (Gen4)',
        'region': 'EU',
        'expected_hr2': 0x022E,   # (2 << 8) | 46
        'expected_hr5': 46000,
        'families': ['HYBRID_GEN4'],
    },
    # ── Germany (VDE-AR-N 4105) ──────────────────────────────────────
    'DE_VDE4105_3600W': {
        'certification': 'VDE_AR_N_4105',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Germany VDE-AR-N 4105 3.6kW',
        'region': 'Germany',
        'expected_hr2': 0x0724,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'DE_VDE4105_5000W': {
        'certification': 'VDE_AR_N_4105',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Germany VDE-AR-N 4105 5kW',
        'region': 'Germany',
        'expected_hr2': 0x0732,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'DE_VDE4105_6000W': {
        'certification': 'VDE_AR_N_4105',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'Germany VDE-AR-N 4105 6kW',
        'region': 'Germany',
        'expected_hr2': 0x073C,
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    # ── Italy (CEI 0-21) ─────────────────────────────────────────────
    'IT_CEI021_3600W': {
        'certification': 'CEI_0_21',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Italy CEI 0-21 3.6kW',
        'region': 'Italy',
        'expected_hr2': 0x0424,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'IT_CEI021_5000W': {
        'certification': 'CEI_0_21',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Italy CEI 0-21 5kW',
        'region': 'Italy',
        'expected_hr2': 0x0432,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'IT_CEI021_6000W': {
        'certification': 'CEI_0_21',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'Italy CEI 0-21 6kW',
        'region': 'Italy',
        'expected_hr2': 0x043C,
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    # ── Australia (AS4777) ───────────────────────────────────────────
    'AU_AS4777A_3600W': {
        'certification': 'AS4777_A',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Australia AS4777-A 3.6kW',
        'region': 'Australia',
        'expected_hr2': 0x0324,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'AU_AS4777A_5000W': {
        'certification': 'AS4777_A',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Australia AS4777-A 5kW',
        'region': 'Australia',
        'expected_hr2': 0x0332,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'AU_AS4777A_6000W': {
        'certification': 'AS4777_A',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'Australia AS4777-A 6kW',
        'region': 'Australia',
        'expected_hr2': 0x033C,
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    'AU_AS4777A_4600W': {
        'certification': 'AS4777_A',
        'power_watts': 4600,
        'ac_rating_watts': 4600,
        'description': 'Australia AS4777-A 4.6kW',
        'region': 'Australia',
        'expected_hr2': 0x032E,
        'expected_hr5': 46000,
        'families': ['HYBRID_GEN3_PLUS', 'ALL_IN_ONE'],
    },
    'AU_AS4777A_7000W': {
        'certification': 'AS4777_A',
        'power_watts': 7000,
        'ac_rating_watts': 7000,
        'description': 'Australia AS4777-A 7kW',
        'region': 'Australia',
        'expected_hr2': 0x0346,
        'expected_hr5': 70000,
        'families': ['HYBRID_GEN3_PLUS'],
    },
    'AU_AS4777A_8000W': {
        'certification': 'AS4777_A',
        'power_watts': 8000,
        'ac_rating_watts': 8000,
        'description': 'Australia AS4777-A 8kW',
        'region': 'Australia',
        'expected_hr2': 0x0350,
        'expected_hr5': 80000,
        'families': ['HYBRID_GEN3_PLUS'],
    },
    'AU_AS4777B_3600W': {
        'certification': 'AS4777_B',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Australia AS4777-B 3.6kW',
        'region': 'Australia',
        'expected_hr2': 0x1424,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'AU_AS4777B_5000W': {
        'certification': 'AS4777_B',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Australia AS4777-B 5kW',
        'region': 'Australia',
        'expected_hr2': 0x1432,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'AU_AS4777B_6000W': {
        'certification': 'AS4777_B',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'Australia AS4777-B 6kW',
        'region': 'Australia',
        'expected_hr2': 0x143C,
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    'AU_AS4777B_4600W': {
        'certification': 'AS4777_B',
        'power_watts': 4600,
        'ac_rating_watts': 4600,
        'description': 'Australia AS4777-B 4.6kW',
        'region': 'Australia',
        'expected_hr2': 0x142E,
        'expected_hr5': 46000,
        'families': ['HYBRID_GEN3_PLUS', 'ALL_IN_ONE'],
    },
    'AU_AS4777B_7000W': {
        'certification': 'AS4777_B',
        'power_watts': 7000,
        'ac_rating_watts': 7000,
        'description': 'Australia AS4777-B 7kW',
        'region': 'Australia',
        'expected_hr2': 0x1446,
        'expected_hr5': 70000,
        'families': ['HYBRID_GEN3_PLUS'],
    },
    'AU_AS4777B_8000W': {
        'certification': 'AS4777_B',
        'power_watts': 8000,
        'ac_rating_watts': 8000,
        'description': 'Australia AS4777-B 8kW',
        'region': 'Australia',
        'expected_hr2': 0x1450,
        'expected_hr5': 80000,
        'families': ['HYBRID_GEN3_PLUS'],
    },
    'AU_AS4777C_5000W': {
        'certification': 'AS4777_C',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Australia AS4777-C 5kW',
        'region': 'Australia',
        'expected_hr2': 0x1532,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    # ── South Africa (NRS 097) ───────────────────────────────────────
    'ZA_NRS097_3600W': {
        'certification': 'NRS_097',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'South Africa NRS 097 3.6kW',
        'region': 'South Africa',
        'expected_hr2': 0x1224,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'ZA_NRS097_5000W': {
        'certification': 'NRS_097',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'South Africa NRS 097 5kW',
        'region': 'South Africa',
        'expected_hr2': 0x1232,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'ZA_NRS097_6000W': {
        'certification': 'NRS_097',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'South Africa NRS 097 6kW',
        'region': 'South Africa',
        'expected_hr2': 0x123C,
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    # ── Netherlands ──────────────────────────────────────────────────
    'NL_3600W': {
        'certification': 'NETHERLANDS',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Netherlands 3.6kW',
        'region': 'Netherlands',
        'expected_hr2': 0x0924,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'NL_5000W': {
        'certification': 'NETHERLANDS',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Netherlands 5kW',
        'region': 'Netherlands',
        'expected_hr2': 0x0932,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    # ── Belgium ──────────────────────────────────────────────────────
    'BE_3600W': {
        'certification': 'BELGIUM',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Belgium 3.6kW',
        'region': 'Belgium',
        'expected_hr2': 0x0D24,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'BE_5000W': {
        'certification': 'BELGIUM',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Belgium 5kW',
        'region': 'Belgium',
        'expected_hr2': 0x0D32,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    # ── Poland ───────────────────────────────────────────────────────
    'PL_3600W': {
        'certification': 'POLAND',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Poland 3.6kW',
        'region': 'Poland',
        'expected_hr2': 0x0B24,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'PL_5000W': {
        'certification': 'POLAND',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Poland 5kW',
        'region': 'Poland',
        'expected_hr2': 0x0B32,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    # ── Sweden ───────────────────────────────────────────────────────
    'SE_3600W': {
        'certification': 'SWEDEN',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Sweden 3.6kW',
        'region': 'Sweden',
        'expected_hr2': 0x1624,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'SE_5000W': {
        'certification': 'SWEDEN',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Sweden 5kW',
        'region': 'Sweden',
        'expected_hr2': 0x1632,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    # ── Finland ──────────────────────────────────────────────────────
    'FI_3600W': {
        'certification': 'FINLAND',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Finland 3.6kW',
        'region': 'Finland',
        'expected_hr2': 0x1724,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'FI_5000W': {
        'certification': 'FINLAND',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Finland 5kW',
        'region': 'Finland',
        'expected_hr2': 0x1732,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    # ── Spain ────────────────────────────────────────────────────────
    'ES_3600W': {
        'certification': 'SPAIN',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'Spain 3.6kW',
        'region': 'Spain',
        'expected_hr2': 0x1B24,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'ES_5000W': {
        'certification': 'SPAIN',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'Spain 5kW',
        'region': 'Spain',
        'expected_hr2': 0x1B32,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
    'ES_6000W': {
        'certification': 'SPAIN',
        'power_watts': 6000,
        'ac_rating_watts': 6000,
        'description': 'Spain 6kW',
        'region': 'Spain',
        'expected_hr2': 0x1B3C,
        'expected_hr5': 60000,
        'families': ['POLAR', 'HYBRID_GEN3_PLUS', 'ALL_IN_ONE', 'HYBRID_HV_GEN3', 'HYBRID_GEN4', 'ALL_IN_ONE_HYBRID', 'HYBRID_3PH', 'AC_3PH', 'PV'],
    },
    # ── New Zealand ──────────────────────────────────────────────────
    'NZ_3600W': {
        'certification': 'NEW_ZEALAND',
        'power_watts': 3600,
        'ac_rating_watts': 3600,
        'description': 'New Zealand 3.6kW',
        'region': 'New Zealand',
        'expected_hr2': 0x1324,
        'expected_hr5': 36000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'AC', 'PV'],
    },
    'NZ_5000W': {
        'certification': 'NEW_ZEALAND',
        'power_watts': 5000,
        'ac_rating_watts': 5000,
        'description': 'New Zealand 5kW',
        'region': 'New Zealand',
        'expected_hr2': 0x1332,
        'expected_hr5': 50000,
        'families': ['HYBRID_GEN1', 'HYBRID_GEN2', 'HYBRID_GEN3', 'POLAR', 'HYBRID_GEN3_PLUS', 'PV'],
    },
}


def get_commissioning_presets() -> dict:
    """Return available commissioning presets for UI population."""
    return {
        name: {
            'description': preset['description'],
            'region': preset['region'],
            'certification': preset['certification'],
            'power_watts': preset['power_watts'],
            'ac_rating_watts': preset['ac_rating_watts'],
            'expected_hr2': f"0x{preset['expected_hr2']:04X} ({preset['expected_hr2']})",
            'expected_hr5': preset['expected_hr5'],
            'families': preset.get('families', []),
        }
        for name, preset in COMMISSIONING_PRESETS.items()
    }


def get_inverter_families() -> dict:
    """Return inverter family definitions for UI population."""
    return {
        family: {
            'label': INVERTER_FAMILY_LABELS.get(family, family),
            'valid_powers': powers,
        }
        for family, powers in INVERTER_FAMILY_POWER_TIERS.items()
    }


def get_presets_by_region() -> dict:
    """Return commissioning presets grouped by region."""
    regions = {}
    for name, preset in COMMISSIONING_PRESETS.items():
        region = preset['region']
        if region not in regions:
            regions[region] = []
        regions[region].append({
            'preset_name': name,
            'description': preset['description'],
            'certification': preset['certification'],
            'power_watts': preset['power_watts'],
        })
    return regions


def commission_inverter_preset(preset_name: str) -> list[TransparentRequest]:
    """
    Commission an inverter using a named preset.
    
    ⚠️ EXTREMELY DANGEROUS - Changes inverter identity & grid compliance!
    ⚠️ CAUSES REBOOT - Inverter will restart after registers are written!
    ⚠️ REGISTER RESET - Some registers may reset to defaults after reboot!
    
    Process:
        1. Writes HR(2) = (Certification << 8) | (Power / 100)
        2. Writes HR(5) = Power * 10 (deciwatts)
        3. HR(0) auto-updates after reboot
    
    Args:
        preset_name: Key from COMMISSIONING_PRESETS
    
    Returns:
        List of Modbus write requests to execute in order
    """
    if preset_name not in COMMISSIONING_PRESETS:
        available = list(COMMISSIONING_PRESETS.keys())
        raise ValueError(f"Unknown preset '{preset_name}'. Available: {available}")
    
    preset = COMMISSIONING_PRESETS[preset_name]
    cert = preset['certification']
    power = preset['power_watts']
    ac_rating = preset['ac_rating_watts']
    
    _logger.critical("=" * 70)
    _logger.critical("⚠️  INVERTER COMMISSIONING — %s", preset['description'])
    _logger.critical("    Certification: %s, Power: %dW, AC Rating: %dW", cert, power, ac_rating)
    _logger.critical("    Expected HR(2): 0x%04X (%d)", preset['expected_hr2'], preset['expected_hr2'])
    _logger.critical("    Expected HR(5): %d", preset['expected_hr5'])
    _logger.critical("    ⚠️ INVERTER WILL REBOOT AFTER THIS OPERATION!")
    _logger.critical("=" * 70)
    
    requests = []
    # Step 1: Set HR(2) — certification + internal power rating (MUST be first)
    requests.extend(set_certification_and_power(cert, power))
    # Step 2: Set HR(5) — AC power rating
    requests.extend(set_inverter_ac_rating(ac_rating))
    return requests


def get_commissioning_info() -> dict:
    """
    Return comprehensive commissioning information for display/documentation.
    
    Includes:
        - How commissioning works (register flow)
        - Available presets grouped by region
        - Full certification list
        - Known DTC code mappings
    """
    return {
        'how_it_works': {
            'step_1': 'Write HR(2) = (Certification_Code << 8) | (Power_Watts / 100)',
            'step_2': 'Write HR(5) = Power_Watts * 10 (deciwatts)',
            'step_3': 'Inverter reboots automatically',
            'step_4': 'HR(0) / Device Type Code updates automatically after reboot',
            'warnings': [
                'HR(0) is READ-ONLY — cannot be written directly (0x86 rejection)',
                'Writing HR(2) + HR(5) causes an inverter reboot',
                'Some registers may reset to defaults after reboot — verify all settings',
                'Incorrect certification may violate grid regulations',
                'Incorrect power rating may damage equipment',
            ],
        },
        'presets_by_region': get_presets_by_region(),
        'known_dtc_codes': {
            '0x20xx': 'Hybrid (Gen1/2/3)',
            '0x21xx': 'Polar',
            '0x22xx': 'Gen3+',
            '0x23xx': 'PV Only',
            '0x30xx': 'AC Coupled',
            '0x40xx': '3-Phase Hybrid',
            '0x41xx': 'AIO Commercial',
            '0x50xx': 'EMS',
            '0x60xx': 'AC 3-Phase',
            '0x80xx': 'All-In-One',
            '0x81xx': 'HV Gen3',
            '0x82xx': 'AIO Hybrid',
            '0x83xx': 'Gen4',
        },
        'known_dtc_examples': {
            '0x2001': 'Hybrid 5kW',
            '0x2003': 'Hybrid 3.6kW',
            '0x2201': 'Gen3+ 5kW',
            '0x3001': 'AC3 3kW',
            '0x3002': 'AC3 3kW (variant)',
            '0x8001': 'AIO 6kW',
            '0x8102': 'HV Gen3 8kW',
            '0x8103': 'HV Gen3 10kW',
        },
    }


# =============================================================================
# ENGINEERING COMMANDS REGISTRY
# =============================================================================

ENGINEERING_COMMANDS = {
    'inverter_config': {
        'function': set_inverter_config,
        'name': 'Inverter Configuration',
        'register': 'HR(2)',
        'danger_level': 'CRITICAL',
        'description': 'Raw certification + power rating value',
    },
    'certification_and_power': {
        'function': set_certification_and_power,
        'name': 'Certification & Power',
        'register': 'HR(2)',
        'danger_level': 'CRITICAL',
        'description': 'Set grid certification and internal power rating',
    },
    'inverter_ac_rating': {
        'function': set_inverter_ac_rating,
        'name': 'AC Power Rating',
        'register': 'HR(5)',
        'danger_level': 'HIGH',
        'description': 'Set AC grid power rating (0-7200W)',
    },
    'export_power_limit': {
        'function': set_export_power_limit,
        'name': 'Export Power Limit',
        'register': 'HR(26)',
        'danger_level': 'MEDIUM',
        'description': 'Set grid export power limit (0-20000W)',
    },
    'reverse_ct_clamp': {
        'function': set_reverse_ct_clamp,
        'name': 'Reverse CT Clamp',
        'register': 'HR(42)',
        'danger_level': 'LOW',
        'description': 'Reverse CT clamp polarity',
    },
    'meter_type': {
        'function': set_meter_type,
        'name': 'Meter Type',
        'register': 'HR(47)',
        'danger_level': 'LOW',
        'description': 'Select energy meter type',
    },
    'reverse_em115_meter': {
        'function': set_reverse_em115_meter,
        'name': 'Reverse EM115 Meter',
        'register': 'HR(48)',
        'danger_level': 'LOW',
        'description': 'Reverse EM115 meter CT polarity',
    },
    'reverse_em418_meter': {
        'function': set_reverse_em418_meter,
        'name': 'Reverse EM418 Meter',
        'register': 'HR(49)',
        'danger_level': 'LOW',
        'description': 'Reverse EM418 meter CT polarity',
    },
    'battery_type': {
        'function': set_battery_type,
        'name': 'Battery Type',
        'register': 'HR(54)',
        'danger_level': 'HIGH',
        'description': 'Set battery chemistry type',
    },
    'battery_nominal_capacity': {
        'function': set_battery_nominal_capacity,
        'name': 'Battery Capacity',
        'register': 'HR(55)',
        'danger_level': 'HIGH',
        'description': 'Set battery nominal capacity (Ah)',
    },
    'auto_detect_battery': {
        'function': set_auto_detect_battery,
        'name': 'Auto Detect Battery',
        'register': 'HR(58)',
        'danger_level': 'MEDIUM',
        'description': 'Enable/disable auto battery detection',
    },
    'pv_start_voltage': {
        'function': set_pv_start_voltage,
        'name': 'PV Start Voltage',
        'register': 'HR(60)',
        'danger_level': 'MEDIUM',
        'description': 'Set PV input start voltage',
    },
    'bms_type': {
        'function': set_bms_type,
        'name': 'BMS Type',
        'register': 'HR(109)',
        'danger_level': 'MEDIUM',
        'description': 'Select BMS communication type',
    },
    'enable_6kw_export': {
        'function': set_enable_6kw_export,
        'name': 'Enable >6kW Export',
        'register': 'HR(126)',
        'danger_level': 'MEDIUM',
        'description': 'Enable system for >6kW export',
    },
    'force_enable_battery_bms': {
        'function': set_force_enable_battery_bms,
        'name': 'Force Enable Battery',
        'register': 'HR(175)',
        'danger_level': 'HIGH',
        'description': 'Force enable battery with PV/Grid',
    },
    'enable_g100_limit': {
        'function': set_enable_g100_limit,
        'name': 'G100 Limit',
        'register': 'HR(178)',
        'danger_level': 'MEDIUM',
        'description': 'Enable G100 export limitation',
    },
    # ── Commissioning ────────────────────────────────────────────────
    'commission_preset': {
        'function': commission_inverter_preset,
        'name': 'Commission Inverter (Preset)',
        'register': 'HR(2) + HR(5)',
        'danger_level': 'CRITICAL',
        'description': 'Commission inverter using a regional preset — CAUSES REBOOT',
    },
}
