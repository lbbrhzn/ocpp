"""The multi-connector stale-entity cleanup must fire, and say so.

When a charger reports more than one connector, the CONNECTOR_ONLY
metrics exist per-connector and the charger-level (flat) variant is a
genuine orphan, lingering as 'unavailable'. sensor.py has removed such
orphans at setup since the cleanup was introduced - except it never
actually matched one: its local unique_id mirror lowercased without
replacing dots, so every dotted metric (Status.Connector, all the
measurands) missed. Single-sourcing the format in const.sensor_unique_id
made the cleanup work for the first time, which makes it user-visible on
upgrade: a removal destroys renames and customisations with it, so it
must be logged.

Nothing previously drove this path in tests, which let an undefined
logger reference sit on it - HA swallows platform setup errors, so the
whole sensor platform would have died silently on exactly the installs
the cleanup targets.
"""

import asyncio
from unittest.mock import patch

import pytest
import websockets.asyncio.server
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import DOMAIN, sensor_unique_id
from custom_components.ocpp.enums import HAChargerStatuses as cstat

from .const import MOCK_CONFIG_DATA_1
from .lifecycle_asserts import assert_no_swallowed_lifecycle_errors


@pytest.fixture(name="bypass_websockets")
def bypass_websockets_fixture():
    """Stub only the websocket server; the real state machine stays."""
    future = asyncio.Future()
    future.set_result(websockets.asyncio.server.Server)
    with (
        patch("websockets.asyncio.server.serve", return_value=future),
        patch("websockets.asyncio.server.Server.close"),
        patch("websockets.asyncio.server.Server.wait_closed"),
    ):
        yield


async def test_setup_removes_the_stale_flat_entity_and_logs_it(
    hass, bypass_websockets, caplog
):
    """A dotted metric's orphan is removed at setup, with a log line.

    MOCK_CONFIG_DATA_1's charger reports two connectors, so
    Status.Connector exists per-connector and the flat variant seeded
    here is exactly the orphan the cleanup exists for. The removal
    assert also guards the code path itself: a broken cleanup dies
    inside platform setup, which Home Assistant swallows, so without
    this test the sensor platform could fail silently on precisely the
    multi-connector installs the cleanup targets.
    """
    cpid = "test_cpid_9001"
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        sensor_unique_id(cpid, cstat.status_connector.value),
        suggested_object_id=f"{cpid}_status_connector_renamed",
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA_1,
        entry_id="test_stale_cleanup",
        title="test_stale_cleanup",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The platform came up cleanly - a failure inside the cleanup would
    # have been swallowed into a lifecycle log line rather than raising.
    assert_no_swallowed_lifecycle_errors(caplog)
    assert registry.async_get(stale.entity_id) is None
    removal_logs = [
        r
        for r in caplog.records
        if "Removing stale charger-level entity" in r.getMessage()
    ]
    assert len(removal_logs) >= 1
    assert stale.entity_id in removal_logs[0].getMessage()


async def test_single_connector_setup_removes_nothing(hass, bypass_websockets, caplog):
    """Control: with one connector the flat entities are the live ones.

    The cleanup must never fire there - removing the flat entity on a
    single-connector charger would delete the entity actually in use.
    Seeding the flat entity first makes this assert something real: an
    over-eager cleanup would remove it, where an empty registry would
    let even a broken guard pass.
    """
    from .const import MOCK_CONFIG_DATA, MOCK_CONFIG_CP_APPEND
    from custom_components.ocpp.const import CONF_CPIDS

    registry = er.async_get(hass)
    live = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        sensor_unique_id("test_cpid", cstat.status_connector.value),
        suggested_object_id="test_cpid_status_connector",
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CONFIG_DATA,
            CONF_CPIDS: [{"CP_single": {**MOCK_CONFIG_CP_APPEND}}],
        },
        entry_id="test_no_cleanup",
        title="test_no_cleanup",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(live.entity_id) is not None
    assert not [
        r
        for r in caplog.records
        if "Removing stale charger-level entity" in r.getMessage()
    ]
    assert_no_swallowed_lifecycle_errors(caplog)
