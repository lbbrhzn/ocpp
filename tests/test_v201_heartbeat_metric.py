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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DATA_UPDATED,
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import HAChargerStatuses as cstat
from custom_components.ocpp.ocppv16 import ChargePoint as ChargePoint16
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


def _mk_cp16(hass):
    """Build the 1.6 twin of _mk_cp, for the shared-helper parity test."""
    data = {
        "host": "127.0.0.1",
        "port": 0,
        "csid": "cs",
        "cpids": [{"CP_A": {"cpid": "test_cpid"}}],
        "subprotocols": ["ocpp1.6"],
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
        subprotocol="ocpp1.6",
    )
    return ChargePoint16("CP_A", conn, hass, entry, central, charger)


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


def _register_heartbeat_sensor(hass, cpid="test_cpid"):
    """Register the heartbeat sensor as the platform would, renamed even.

    The renamed entity_id pins that the dispatch resolves through the
    registry rather than reconstructing sensor.<cpid>_heartbeat by slug -
    users rename entities, unique_ids are forever.
    """
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}.{cpid}.heartbeat.sensor",
        suggested_object_id=f"{cpid}_pulse_renamed",
    )
    return entry.entity_id


def _capture_dispatches(hass):
    """Collect every DATA_UPDATED payload."""
    seen = []
    async_dispatcher_connect(hass, DATA_UPDATED, lambda *args: seen.append(args))
    return seen


@pytest.mark.asyncio
async def test_a_heartbeat_refreshes_only_its_own_sensor(hass):
    """One metric moved, so one entity refreshes.

    The full update() walks the device registry and force-refreshes every
    entity of the charger, at a rate the charger controls - a charger
    beating every 10 s did all that work for one timestamp. The dispatch
    must carry exactly the heartbeat sensor, resolved via the registry.
    """
    cp = _mk_cp(hass)
    entity_id = _register_heartbeat_sensor(hass)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        cp.on_heartbeat()
        await hass.async_block_till_done()

    update.assert_not_awaited()
    assert seen == [({entity_id},)]


@pytest.mark.asyncio
async def test_an_unregistered_sensor_falls_back_to_the_full_update(hass):
    """The first heartbeat can arrive before the platforms add entities.

    Dropping the refresh there would leave the sensor stale until the next
    unrelated update; the pre-optimisation full push is the safe fallback.
    """
    cp = _mk_cp(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        cp.on_heartbeat()
        await hass.async_block_till_done()

    update.assert_awaited_once_with("test_cpid")


@pytest.mark.asyncio
async def test_the_v16_handler_refreshes_the_same_way(hass):
    """Both protocols share the helper; neither walks the registry per beat."""
    cp16 = _mk_cp16(hass)
    entity_id = _register_heartbeat_sensor(hass)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint16, "update", AsyncMock()) as update:
        cp16.on_heartbeat()
        await hass.async_block_till_done()

    update.assert_not_awaited()
    assert seen == [({entity_id},)]
    assert cp16._metrics[0][cstat.heartbeat.value].value is not None
