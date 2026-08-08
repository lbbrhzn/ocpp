"""The force_smart_charging override on OCPP 2.0.1.

SmartChargingCtrlr/Available is optional in OCPP 2.0.1, so a charger can
implement smart charging and still not advertise it - the FoxESS A-series
reports ProfileStackLevel, RateUnit and PeriodsPerSchedule but no Available -
and the SMART profile is then dropped. The override existed only in the OCPP
1.6 path, so on 2.0.1 it silently did nothing and there was no way to restore
the profile.
"""

import asyncio
from types import SimpleNamespace

import pytest
from ocpp.exceptions import OCPPError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import Profiles
from custom_components.ocpp.ocppv201 import ChargePoint, InventoryReport

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


def _mk_cp(hass, force_smart_charging: bool):
    """Build a v201 ChargePoint detached from any real connection."""
    data = {
        "host": "127.0.0.1",
        "port": 0,
        "csid": "cs",
        "cpids": [{"CP_A": {"cpid": "test_cpid"}}],
        "subprotocols": ["ocpp2.0.1"],
        "websocket_close_timeout": 5,
        "ssl": False,
        "websocket_ping_interval": 0.0,
        "websocket_ping_timeout": 0.01,
        "websocket_ping_tries": 0,
        "ssl_certfile_path": CONF_SSL_CERTFILE_PATH,
        "ssl_keyfile_path": CONF_SSL_KEYFILE_PATH,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    central = CentralSystemSettings(**data)
    charger = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=force_smart_charging,
    )
    conn = SimpleNamespace(
        state=State.CLOSED,
        close=lambda: asyncio.sleep(0),
        subprotocol="ocpp2.0.1",
    )
    cp = ChargePoint("CP_A", conn, hass, entry, central, charger)
    # A charger that does not advertise SmartChargingCtrlr/Available. Seeding
    # the inventory also short-circuits _get_inventory, so the only calls made
    # are the UpdateFirmware and TriggerMessage capability probes.
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    async def refuse_probes(req):
        raise OCPPError("not supported")

    cp.call = refuse_probes
    return cp


@pytest.mark.asyncio
async def test_force_smart_charging_restores_the_profile(hass):
    """The override must apply on 2.0.1, as it already does on 1.6."""
    cp = _mk_cp(hass, force_smart_charging=True)

    features = await cp.get_supported_features()

    assert Profiles.SMART in features


@pytest.mark.asyncio
async def test_smart_profile_absent_without_the_override(hass):
    """Control: the same charger yields no SMART when the override is off.

    Without this the test above would pass even if the override were ignored
    and SMART came from somewhere else.
    """
    cp = _mk_cp(hass, force_smart_charging=False)

    features = await cp.get_supported_features()

    assert Profiles.SMART not in features


@pytest.mark.asyncio
async def test_override_does_not_invent_other_profiles(hass):
    """Forcing SMART must not imply reservation or local auth support."""
    cp = _mk_cp(hass, force_smart_charging=True)

    features = await cp.get_supported_features()

    assert Profiles.RES not in features
    assert Profiles.AUTH not in features


@pytest.mark.asyncio
async def test_detection_still_observable_with_the_override_off(hass):
    """SMART must still be reachable by detection alone, via the real parser.

    Every charger config in tests/const.py sets force_smart_charging, so now
    that the override is honoured on 2.0.1 the integration-level feature
    assertions in test_charge_point_v201.py are satisfied whether detection
    works or not. This drives the actual NotifyReport parser with the
    override off, so a regression in the SmartChargingCtrlr/Available
    handling fails here rather than passing unnoticed.
    """
    cp = _mk_cp(hass, force_smart_charging=False)
    cp._inventory = None
    cp._wait_inventory = asyncio.Event()

    cp.on_report(
        1,
        "2026-01-01T00:00:00Z",
        0,
        report_data=[
            {
                "component": {"name": "SmartChargingCtrlr"},
                "variable": {"name": "Available"},
                "variable_attribute": [{"value": "true"}],
            }
        ],
    )

    assert cp._inventory.smart_charging_available is True

    features = await cp.get_supported_features()

    assert Profiles.SMART in features
