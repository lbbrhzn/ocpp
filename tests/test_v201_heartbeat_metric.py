"""The heartbeat sensor must track the charger on OCPP 2.0.1.

The v201 heartbeat handler answered the charger but recorded nothing, so
sensor.<cpid>_heartbeat kept whatever an earlier session left - on a
charger switched from 1.6 it showed the 1.6-era timestamp all day while
2.0.1 heartbeats arrived on the wire. The 1.6 handler writes the metric
and pushes the entities; 2.0.1 now mirrors it.
"""

import asyncio
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        max_current=32,
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
async def test_a_heartbeat_writes_the_metric(hass):
    """The sensor's backing metric must move when the charger beats."""
    cp = _mk_cp(hass)
    stale = datetime(2026, 8, 8, 0, 41, 40, tzinfo=UTC)
    cp._metrics[(0, cstat.heartbeat.value)].value = stale

    before = datetime.now(tz=UTC)
    with patch.object(ChargePoint, "update", AsyncMock()):
        cp.on_heartbeat()
        await hass.async_block_till_done()
    after = datetime.now(tz=UTC)

    recorded = cp._metrics[(0, cstat.heartbeat.value)].value
    assert recorded != stale
    # Bounded by the test's own clock reads - no wall-clock window to flake.
    assert before <= recorded <= after


@pytest.mark.asyncio
async def test_the_reply_and_the_metric_agree(hass):
    """The time told to the charger is the time shown to the user."""
    cp = _mk_cp(hass)

    with patch.object(ChargePoint, "update", AsyncMock()):
        result = cp.on_heartbeat()
        await hass.async_block_till_done()

    recorded = cp._metrics[(0, cstat.heartbeat.value)].value
    assert result.current_time == recorded.isoformat()


@pytest.mark.asyncio
async def test_the_entities_are_pushed(hass):
    """Writing the metric is not enough; the sensor updates on push.

    The 1.6 handler schedules an entity update on every heartbeat so the
    sensor refreshes without waiting for another state change; parity
    matters because on an idle charger heartbeats may be the only traffic.
    """
    cp = _mk_cp(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        cp.on_heartbeat()
        await hass.async_block_till_done()

    update.assert_awaited_once_with("test_cpid")
