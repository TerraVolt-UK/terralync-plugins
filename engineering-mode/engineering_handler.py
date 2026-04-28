"""
Engineering Mode Handler — plugin-bundled copy
===============================================

⚠️  WARNING: DANGEROUS INVERTER OPERATIONS ⚠️
===============================================

This module is bundled inside the ``engineering-mode`` plugin.
Installing the plugin enables engineering mode; removing it disables it
completely (no code left to reverse-engineer).

The dashboard API handler dynamically imports this module from the
plugin directory when the plugin is installed and enabled.
"""

import logging
import os
import sys
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Ensure the plugin directory is on sys.path so we can import
# engineering_commands.py which lives alongside this file.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

# Import engineering commands from the local copy in this plugin directory
try:
    from engineering_commands import (
        set_inverter_config,
        set_certification_and_power,
        set_inverter_ac_rating,
        set_export_power_limit,
        set_reverse_ct_clamp,
        set_meter_type,
        set_reverse_em115_meter,
        set_reverse_em418_meter,
        set_battery_type,
        set_battery_nominal_capacity,
        set_auto_detect_battery,
        set_pv_start_voltage,
        set_bms_type,
        set_enable_6kw_export,
        set_force_enable_battery_bms,
        set_enable_g100_limit,
        set_serial_number,
        uprate_gen1_to_5kw,
        downrate_gen1_to_3600w,
        reapply_ac_rating_after_downrate,
        commission_inverter_preset,
        get_commissioning_presets,
        get_commissioning_info,
        get_inverter_families,
        COMMISSIONING_PRESETS,
        INVERTER_FAMILY_POWER_TIERS,
        INVERTER_FAMILY_LABELS,
        MeterType,
        BatteryType,
        BMSType,
        ENGINEERING_COMMANDS,
    )
    ENGINEERING_AVAILABLE = True
except ImportError as exc:
    ENGINEERING_AVAILABLE = False
    logger.info("Engineering commands not available: %s", exc)

# Battery calibration uses the standard command (not engineering_commands)
try:
    from givenergy_modbus_async.client.commands import set_calibrate_battery_soc
except ImportError:
    set_calibrate_battery_soc = None


# Engineering mode state
_engineering_mode_enabled = False
_engineering_unlock_code = "ENGINEERING_MODE_UNLOCK"  # Simple unlock mechanism


def is_engineering_available() -> bool:
    """Check if engineering commands are available (not production build)."""
    return ENGINEERING_AVAILABLE


def is_engineering_enabled() -> bool:
    """Check if engineering mode is currently enabled."""
    return ENGINEERING_AVAILABLE and _engineering_mode_enabled


def enable_engineering_mode(unlock_code: str) -> bool:
    """
    Enable engineering mode with unlock code.
    
    Returns True if successfully enabled.
    """
    global _engineering_mode_enabled
    
    if not ENGINEERING_AVAILABLE:
        logger.warning("Engineering mode not available - production build")
        return False
    
    if unlock_code == _engineering_unlock_code:
        _engineering_mode_enabled = True
        logger.critical("=" * 60)
        logger.critical("⚠️  ENGINEERING MODE ENABLED ⚠️")
        logger.critical("All engineering operations will be logged.")
        logger.critical("=" * 60)
        return True
    else:
        logger.warning("Invalid engineering unlock code attempted")
        return False


def disable_engineering_mode() -> None:
    """Disable engineering mode."""
    global _engineering_mode_enabled
    _engineering_mode_enabled = False
    logger.info("Engineering mode disabled")


def get_engineering_commands_info() -> list[Dict[str, Any]]:
    """
    Get information about available engineering commands.
    Used by dashboard to display options (greyed out if not enabled).
    """
    if not ENGINEERING_AVAILABLE:
        # Return static list for display purposes even in production
        return [
            {'id': 'inverter_config', 'name': 'Inverter Configuration', 'register': 'HR(2)', 'danger_level': 'CRITICAL'},
            {'id': 'inverter_ac_rating', 'name': 'AC Power Rating', 'register': 'HR(5)', 'danger_level': 'HIGH'},
            {'id': 'export_power_limit', 'name': 'Export Power Limit', 'register': 'HR(26)', 'danger_level': 'MEDIUM'},
            {'id': 'reverse_ct_clamp', 'name': 'Reverse CT Clamp', 'register': 'HR(42)', 'danger_level': 'LOW'},
            {'id': 'meter_type', 'name': 'Meter Type', 'register': 'HR(47)', 'danger_level': 'LOW'},
            {'id': 'reverse_em115_meter', 'name': 'Reverse EM115 Meter', 'register': 'HR(48)', 'danger_level': 'LOW'},
            {'id': 'reverse_em418_meter', 'name': 'Reverse EM418 Meter', 'register': 'HR(49)', 'danger_level': 'LOW'},
            {'id': 'battery_type', 'name': 'Battery Type', 'register': 'HR(54)', 'danger_level': 'HIGH'},
            {'id': 'battery_nominal_capacity', 'name': 'Battery Capacity', 'register': 'HR(55)', 'danger_level': 'HIGH'},
            {'id': 'auto_detect_battery', 'name': 'Auto Detect Battery', 'register': 'HR(58)', 'danger_level': 'MEDIUM'},
            {'id': 'pv_start_voltage', 'name': 'PV Start Voltage', 'register': 'HR(60)', 'danger_level': 'MEDIUM'},
            {'id': 'bms_type', 'name': 'BMS Type', 'register': 'HR(109)', 'danger_level': 'MEDIUM'},
            {'id': 'enable_6kw_export', 'name': 'Enable >6kW Export', 'register': 'HR(126)', 'danger_level': 'MEDIUM'},
            {'id': 'force_enable_battery_bms', 'name': 'Force Enable Battery', 'register': 'HR(175)', 'danger_level': 'HIGH'},
            {'id': 'enable_g100_limit', 'name': 'G100 Limit', 'register': 'HR(178)', 'danger_level': 'MEDIUM'},
            {'id': 'commission_preset', 'name': 'Commission Inverter (Preset)', 'register': 'HR(2) + HR(5)', 'danger_level': 'CRITICAL'},
        ]
    
    return [
        {
            'id': cmd_id,
            'name': cmd_info['name'],
            'register': cmd_info['register'],
            'danger_level': cmd_info['danger_level'],
            'description': cmd_info['description'],
        }
        for cmd_id, cmd_info in ENGINEERING_COMMANDS.items()
    ]


async def handle_engineering_command(
    client,
    command: str,
    value: Any,
    inverter_data: Optional[Dict] = None
) -> Dict[str, Any]:
    """
    Handle an engineering command.
    
    Returns dict with 'success', 'message', and optionally 'requests'.
    """
    if not ENGINEERING_AVAILABLE:
        return {
            'success': False,
            'message': 'Engineering commands not available in production build'
        }
    
    if not _engineering_mode_enabled:
        return {
            'success': False,
            'message': 'Engineering mode not enabled. Unlock required.'
        }
    
    logger.critical(f"⚠️ ENGINEERING COMMAND: {command} = {value}")
    
    try:
        requests = []
        
        # Route to appropriate command function
        if command == 'inverter_config':
            requests = set_inverter_config(int(value))
            
        elif command == 'certification_and_power':
            # Expects {'certification': 'G99', 'power': 5000}
            requests = set_certification_and_power(
                value.get('certification', 'G99'),
                int(value.get('power', 5000))
            )
            
        elif command == 'inverter_ac_rating':
            requests = set_inverter_ac_rating(int(value))
            
        elif command == 'export_power_limit':
            requests = set_export_power_limit(int(value))
            
        elif command == 'reverse_ct_clamp':
            requests = set_reverse_ct_clamp(bool(value))
            
        elif command == 'meter_type':
            requests = set_meter_type(MeterType(int(value)))
            
        elif command == 'reverse_em115_meter':
            requests = set_reverse_em115_meter(bool(value))
            
        elif command == 'reverse_em418_meter':
            requests = set_reverse_em418_meter(bool(value))
            
        elif command == 'battery_type':
            requests = set_battery_type(BatteryType(int(value)))
            
        elif command == 'battery_nominal_capacity':
            requests = set_battery_nominal_capacity(int(value))
            
        elif command == 'auto_detect_battery':
            requests = set_auto_detect_battery(bool(value))
            
        elif command == 'pv_start_voltage':
            requests = set_pv_start_voltage(int(value))
            
        elif command == 'bms_type':
            requests = set_bms_type(BMSType(int(value)))
            
        elif command == 'enable_6kw_export':
            requests = set_enable_6kw_export(bool(value))
            
        elif command == 'force_enable_battery_bms':
            requests = set_force_enable_battery_bms(bool(value))
            
        elif command == 'enable_g100_limit':
            requests = set_enable_g100_limit(bool(value))
            
        elif command == 'serial_number':
            requests = set_serial_number(str(value))
            
        elif command == 'uprate_to_5kw':
            requests = uprate_gen1_to_5kw()
            
        elif command == 'downrate_to_3600w':
            requests = downrate_gen1_to_3600w()
        
        elif command == 'reapply_ac_rating_after_downrate':
            requests = reapply_ac_rating_after_downrate()
        
        elif command == 'soc_force_adjust':
            # Battery calibration: 0=Cancel, 1=Full, 3=Top
            if set_calibrate_battery_soc is None:
                return {'success': False, 'message': 'Battery calibration command not available'}
            requests = set_calibrate_battery_soc(int(value))
        
        # ── Commissioning commands ────────────────────────────────────
        elif command == 'commission_preset':
            # Expects {'preset': 'UK_G99_5000W'}
            preset_name = value.get('preset') if isinstance(value, dict) else str(value)
            requests = commission_inverter_preset(preset_name)
            
        elif command == 'get_commissioning_presets':
            # Info-only command — no register writes
            return {
                'success': True,
                'message': 'Commissioning presets retrieved',
                'presets': get_commissioning_presets(),
                'families': get_inverter_families(),
                'info': get_commissioning_info(),
            }
            
        else:
            return {
                'success': False,
                'message': f'Unknown engineering command: {command}'
            }
        
        # Execute the requests via persistent connection (not one_shot_command)
        if requests and client:
            # Send each request individually to avoid overwhelming the inverter
            for req in requests:
                await client.send_request_and_await_response(req, timeout=3.0, retries=0)
            
            logger.critical(f"⚠️ ENGINEERING COMMAND EXECUTED: {command}")
            return {
                'success': True,
                'message': f'Engineering command {command} executed successfully',
                'requests_count': len(requests)
            }
        else:
            return {
                'success': False,
                'message': 'No client available or no requests generated'
            }
            
    except ValueError as e:
        logger.error(f"Engineering command validation error: {e}")
        return {
            'success': False,
            'message': f'Validation error: {str(e)}'
        }
    except Exception as e:
        logger.error(f"Engineering command error: {e}")
        return {
            'success': False,
            'message': f'Error: {str(e)}'
        }
