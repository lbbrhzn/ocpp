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
    sensor_unique_id,
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
    cp._metrics[(0, cstat.heartbeat)].value = stale

    before = datetime.now(tz=UTC)
    with patch.object(ChargePoint, "update", AsyncMock()):
        cp.on_heartbeat()
        await hass.async_block_till_done()
    after = datetime.now(tz=UTC)

    recorded = cp._metrics[(0, cstat.heartbeat)].value
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

    recorded = cp._metrics[(0, cstat.heartbeat)].value
    assert result.current_time == recorded.isoformat()


def _register_metric_sensor(hass, metric, cpid="test_cpid", suffix="renamed"):
    """Register a metric sensor as the platform would, renamed even.

    The unique_id comes from the same canonical helper production uses,
    so this fixture moves with the format instead of hiding drift - a
    hand-written literal here would keep passing while the resolution
    silently fell back to the full update. The renamed entity_id pins
    that the dispatch resolves through the registry rather than
    reconstructing sensor.<cpid>_<metric> by slug.
    """
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        sensor_unique_id(cpid, metric),
        suggested_object_id=f"{cpid}_{metric.lower().replace('.', '_')}_{suffix}",
    )
    return entry.entity_id


def _register_heartbeat_sensor(hass, cpid="test_cpid"):
    """Register the heartbeat sensor under a deliberately renamed id."""
    return _register_metric_sensor(hass, cstat.heartbeat, cpid, "pulse")


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
    assert cp16._metrics[0][cstat.heartbeat].value is not None


@pytest.mark.asyncio
async def test_the_fallback_is_temporary(hass):
    """Once the sensor registers, the helper must switch to targeted.

    The fallback exists for the startup window only; a helper that kept
    falling back would silently reinstate the full-refresh cost forever.
    """
    cp = _mk_cp(hass)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        cp.on_heartbeat()
        await hass.async_block_till_done()
        update.assert_awaited_once_with("test_cpid")

        entity_id = _register_heartbeat_sensor(hass)
        cp.on_heartbeat()
        await hass.async_block_till_done()

        update.assert_awaited_once_with("test_cpid")
        assert seen == [({entity_id},)]


def _drive_monitor_once(monkeypatch, cp, fail_first_iteration=False):
    """Prepare monitor_connection to run exactly one measuring iteration.

    Everything runs on real asyncio - the only monkeypatch is our own
    module's backstop delay. The scripted ping resolves immediately for
    the success path, or never for the timeout path so the real
    wait_for(timeout=0.01) raises a genuine TimeoutError, and it closes
    the connection so the loop exits after one iteration.
    """
    from custom_components.ocpp import chargepoint as cp_mod

    monkeypatch.setattr(cp_mod, "MONITOR_BACKSTOP_DELAY", 0, raising=True)
    cp.post_connect_success = True
    cp._connection.state = State.OPEN
    cp.cs_settings.websocket_ping_interval = 0.0
    cp.cs_settings.websocket_ping_timeout = 0.01
    cp.cs_settings.websocket_ping_tries = 1

    async def scripted_ping():
        cp._connection.state = State.CLOSED
        if fail_first_iteration:
            await asyncio.get_running_loop().create_future()  # never resolves
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    cp._connection.ping = scripted_ping


@pytest.mark.asyncio
async def test_the_ping_loop_publishes_the_latency_sensors(hass, monkeypatch):
    """The loop is these sensors' only publisher; it must dispatch them.

    The heartbeat handler's full update() used to republish them as a
    side effect - on an idle charger, removing it silently froze both
    latency sensors, which feed long-term statistics. The loop now
    pushes exactly the two entities it wrote.
    """
    cp = _mk_cp(hass)
    eid_ping = _register_metric_sensor(hass, cstat.latency_ping)
    eid_pong = _register_metric_sensor(hass, cstat.latency_pong)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        _drive_monitor_once(monkeypatch, cp)
        await cp.monitor_connection()
        await hass.async_block_till_done()

    update.assert_not_awaited()
    assert seen == [({eid_ping, eid_pong},)]


@pytest.mark.asyncio
async def test_a_ping_timeout_also_publishes_the_latency_sensors(hass, monkeypatch):
    """The timeout branch records the timeout value; it must publish too.

    A charger falling off the network is exactly when the user looks at
    the latency sensors, so the degraded reading matters most.
    """
    cp = _mk_cp(hass)
    eid_ping = _register_metric_sensor(hass, cstat.latency_ping)
    eid_pong = _register_metric_sensor(hass, cstat.latency_pong)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        _drive_monitor_once(monkeypatch, cp, fail_first_iteration=True)
        await cp.monitor_connection()
        await hass.async_block_till_done()

    update.assert_not_awaited()
    assert seen == [({eid_ping, eid_pong},)]


@pytest.mark.asyncio
async def test_unregistered_latency_sensors_skip_rather_than_fall_back(
    hass, monkeypatch
):
    """The periodic caller must not buy the full walk at ping rate.

    During the startup window the heartbeat's fallback runs at most at
    heartbeat rate; a ping-loop fallback would run the full registry walk
    every ping interval, more often than the behaviour this optimisation
    replaced. Skipping is safe - the next tick republishes.
    """
    cp = _mk_cp(hass)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        _drive_monitor_once(monkeypatch, cp)
        await cp.monitor_connection()
        await hass.async_block_till_done()

    update.assert_not_awaited()
    assert seen == []


@pytest.mark.asyncio
async def test_a_partially_registered_pair_dispatches_what_resolved(hass, monkeypatch):
    """Skipping must not discard the sensor that did resolve.

    One registered sensor and one missing is the transient shape while
    platforms add entities; the resolved one still deserves its refresh.
    """
    cp = _mk_cp(hass)
    eid_ping = _register_metric_sensor(hass, cstat.latency_ping)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        _drive_monitor_once(monkeypatch, cp)
        await cp.monitor_connection()
        await hass.async_block_till_done()

    update.assert_not_awaited()
    assert seen == [({eid_ping},)]


@pytest.mark.asyncio
async def test_a_sensor_deleted_after_startup_does_not_revive_the_full_walk(hass):
    """The fallback is for the startup window, not for deleted entities.

    Once the sensor has resolved, a later registry miss means the user
    removed the entity - there is nothing to refresh, and falling back
    would silently reinstate the full update at heartbeat rate, forever.
    """
    cp = _mk_cp(hass)
    entity_id = _register_heartbeat_sensor(hass)
    seen = _capture_dispatches(hass)

    with patch.object(ChargePoint, "update", AsyncMock()) as update:
        cp.on_heartbeat()
        await hass.async_block_till_done()
        assert seen == [({entity_id},)]

        er.async_get(hass).async_remove(entity_id)
        cp.on_heartbeat()
        await hass.async_block_till_done()

    update.assert_not_awaited()
    assert seen == [({entity_id},)]
