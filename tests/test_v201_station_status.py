"""Station-level and malformed StatusNotification handling for OCPP 2.0.1.

The websocket-level scenario in test_charge_point_v201.py covers a
station-level notification on a charger whose inventory builds a real map.
These unit tests cover the charger the map guard cannot serve: one whose
inventory yields no connector map at all - which is exactly the kind of
charger that sends station-level statuses in the first place.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import HAChargerStatuses as cstat
from custom_components.ocpp.ocppv201 import ChargePoint

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


def _mk_cp(hass):
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
        max_current=32.0,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    conn = SimpleNamespace(
        state=State.CLOSED,
        close=lambda: asyncio.sleep(0),
        subprotocol="ocpp2.0.1",
    )
    return ChargePoint("CP_A", conn, hass, entry, central, charger)


@pytest.mark.asyncio
async def test_station_level_status_applies_without_a_connector_map(hass):
    """A (0, 0) notification must not wait for a map that may never exist.

    It does not route through the map at all, so buffering it behind
    _ensure_connector_map stranded it forever on chargers whose inventory
    yields no connectors - leaving the charger-level Status unset.
    """
    cp = _mk_cp(hass)
    assert cp._evse_to_global == {}  # no map, and none is coming

    cp.on_status_notification(
        timestamp="2026-01-01T00:00:00Z",
        connector_status="Available",
        evse_id=0,
        connector_id=0,
    )

    assert cp._pending_status_notifications == []
    assert cp._metrics[(0, cstat.status.value)].value == "Available"


@pytest.mark.asyncio
@pytest.mark.parametrize("evse_id,connector_id", [(1, 0), (0, 1), (-1, 0), (0, -1)])
async def test_malformed_ids_are_dropped_not_crashed_or_misattributed(
    hass, evse_id, connector_id
):
    """Degenerate ids are neither station-level nor a real connector.

    Fed to the per-connector bookkeeping they would index with -1 - the
    IndexError / silent last-slot overwrite this guard exists to prevent -
    and recorded as station-level they would misattribute another entity's
    state to the charger. They must be dropped.
    """
    cp = _mk_cp(hass)

    cp.on_status_notification(
        timestamp="2026-01-01T00:00:00Z",
        connector_status="Faulted",
        evse_id=evse_id,
        connector_id=connector_id,
    )

    assert cp._pending_status_notifications == []
    assert cp._metrics[(0, cstat.status.value)].value is None
    assert cp._connector_status == []
