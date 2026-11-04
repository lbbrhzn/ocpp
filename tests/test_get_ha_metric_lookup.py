"""get_ha_metric must find the sensor the platform actually created.

The restore path after a mid-session restart reads Energy.Meter.Start back
from HA. When the lookup misses, meter_start falls back to the first
post-restart reading and the session energy is computed from the wrong
baseline. These tests set up the real sensor platform, so the entity ids are
the ones it generates, not ones written by hand.
"""

import asyncio
import contextlib
import copy
from datetime import UTC, datetime

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
import websockets
from ocpp.v16 import call

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    DOMAIN as OCPP_DOMAIN,
    sensor_unique_id,
)
from custom_components.ocpp.enums import HAChargerSession as csess

from .charge_point_test import create_configuration, remove_configuration, wait_ready
from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA
from .test_charge_point_core import _mk_cp
from .test_charge_point_v16 import ChargePoint


def _entry(cpid: str, port: int, num_connectors: int = 1) -> MockConfigEntry:
    config_data = copy.deepcopy(MOCK_CONFIG_DATA)
    cp_cfg = copy.deepcopy(MOCK_CONFIG_CP_APPEND)
    cp_cfg[CONF_CPID] = cpid
    cp_cfg[CONF_NUM_CONNECTORS] = num_connectors
    config_data[CONF_CPIDS].append({"CP_A": cp_cfg})
    config_data[CONF_PORT] = port
    return MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=config_data,
        entry_id=f"lookup_{port}",
        title=f"lookup_{port}",
        version=2,
        minor_version=0,
    )


async def _setup(hass, cpid: str, port: int, num_connectors: int = 1):
    entry = _entry(cpid, port, num_connectors)
    await create_configuration(hass, entry)
    cp = _mk_cp(hass)
    cp.settings.cpid = cpid
    return entry, cp


def _entity_id(hass, cpid: str, connector_id: int | None = None) -> str:
    uid = sensor_unique_id(cpid, csess.meter_start, connector_id)
    entity_id = er.async_get(hass).async_get_entity_id(SENSOR_DOMAIN, OCPP_DOMAIN, uid)
    assert entity_id is not None
    return entity_id


async def test_cpid_with_space_and_hyphen_is_found(hass, socket_enabled):
    """A cpid that isn't a slug resolves to the sensor the platform made."""
    entry, cp = await _setup(hass, "Grizzl-e Charger", 9520)
    entity_id = _entity_id(hass, "Grizzl-e Charger")
    assert entity_id == "sensor.grizzl_e_charger_energy_meter_start"

    hass.states.async_set(entity_id, "4562.026")
    assert cp.get_ha_metric(csess.meter_start, connector_id=1) == "4562.026"

    await remove_configuration(hass, entry)


async def test_renamed_entity_is_found_by_unique_id(hass, socket_enabled):
    """An entity id changed by the user still resolves through the registry."""
    entry, cp = await _setup(hass, "test_cpid", 9521)
    registry = er.async_get(hass)
    old_id = _entity_id(hass, "test_cpid")
    registry.async_update_entity(old_id, new_entity_id="sensor.garage_meter_start")
    await hass.async_block_till_done()

    hass.states.async_set("sensor.garage_meter_start", "4562.026")
    assert cp.get_ha_metric(csess.meter_start, connector_id=1) == "4562.026"

    await remove_configuration(hass, entry)


@pytest.mark.parametrize("cpid", ["test_cpid", "Garage Charger"])
async def test_each_connector_reads_its_own_sensor(hass, socket_enabled, cpid):
    """On a multi-connector charger each connector resolves its own sensor."""
    port = 9522 if cpid == "test_cpid" else 9523
    entry, cp = await _setup(hass, cpid, port, num_connectors=2)
    hass.states.async_set(_entity_id(hass, cpid, 1), "7.25")
    hass.states.async_set(_entity_id(hass, cpid, 2), "12.5")

    assert cp.get_ha_metric(csess.meter_start, connector_id=1) == "7.25"
    assert cp.get_ha_metric(csess.meter_start, connector_id=2) == "12.5"

    await remove_configuration(hass, entry)


def _cp_for(hass, cpid: str):
    cp = _mk_cp(hass)
    cp.settings.cpid = cpid
    return cp


async def test_unregistered_sensor_is_found_by_slug(hass):
    """Without a registry entry, the fallback is the slugified entity id."""
    hass.states.async_set("sensor.grizzl_e_charger_energy_meter_start", "4562.026")
    cp = _cp_for(hass, "Grizzl-e Charger")
    assert cp.get_ha_metric(csess.meter_start, connector_id=1) == "4562.026"


@pytest.mark.parametrize("own_state", ["111.0", "unknown", "unavailable", None])
async def test_slug_of_another_charger_is_not_read(hass, own_state):
    """A registered sensor's slug can belong to another charger.

    "CP_A" and "CP-A" slugify alike, so the second one registered gets the
    "_2" suffix and its slug is the first charger's sensor.
    """
    registry = er.async_get(hass)
    for cpid, object_id in (
        ("CP_A", "cp_a_energy_meter_start"),
        ("CP-A", "cp_a_energy_meter_start_2"),
    ):
        entity = registry.async_get_or_create(
            SENSOR_DOMAIN,
            OCPP_DOMAIN,
            sensor_unique_id(cpid, csess.meter_start),
            suggested_object_id=object_id,
        )
        assert entity.entity_id == f"sensor.{object_id}"
    hass.states.async_set("sensor.cp_a_energy_meter_start", "999.0")
    if own_state is not None:
        hass.states.async_set("sensor.cp_a_energy_meter_start_2", own_state)

    expected = "111.0" if own_state == "111.0" else None
    cp = _cp_for(hass, "CP-A")
    assert cp.get_ha_metric(csess.meter_start, connector_id=1) == expected


@pytest.mark.parametrize("own_state", ["111.0", "unavailable", None])
async def test_fallback_slug_owned_by_another_charger_is_not_read(hass, own_state):
    """The generic fallback skips a slug the registry gives to another charger.

    A multi-connector "CP-A" has no generic Energy.Meter.Start sensor, so its
    fallback slug "cp_a_energy_meter_start" is the sensor of "CP_A".
    """
    registry = er.async_get(hass)
    for cpid, connector_id, object_id in (
        ("CP_A", None, "cp_a_energy_meter_start"),
        ("CP-A", 1, "cp_a_connector_1_energy_meter_start"),
    ):
        registry.async_get_or_create(
            SENSOR_DOMAIN,
            OCPP_DOMAIN,
            sensor_unique_id(cpid, csess.meter_start, connector_id),
            suggested_object_id=object_id,
        )
    hass.states.async_set("sensor.cp_a_energy_meter_start", "999.0")
    if own_state is not None:
        hass.states.async_set("sensor.cp_a_connector_1_energy_meter_start", own_state)

    expected = "111.0" if own_state == "111.0" else None
    cp = _cp_for(hass, "CP-A")
    assert cp.get_ha_metric(csess.meter_start, connector_id=1) == expected


@pytest.mark.timeout(20)
async def test_restart_mid_session_restores_meter_start(hass, socket_enabled):
    """The restored Energy.Meter.Start is the session baseline after a restart.

    Mirrors the report in #2163: cpid "Grizzl-e Charger", meterStart
    4562.026 kWh before the restart, first sample after it 4566000 Wh.
    """
    port, cp_id, restored = 9524, "CP_A", 4562.026
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.grizzl_e_charger_energy_meter_start", str(restored)),
                {"native_value": restored, "native_unit_of_measurement": "kWh"},
            ),
        ],
    )
    entry = _entry("Grizzl-e Charger", port)
    cs = await create_configuration(hass, entry)

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        client = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(client.start())
        try:
            await client.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            await hass.async_block_till_done()
            await client.call(
                call.MeterValues(
                    connector_id=1,
                    meter_value=[
                        {
                            "timestamp": datetime.now(tz=UTC).isoformat(),
                            "sampledValue": [
                                {
                                    "value": "4566000",
                                    "measurand": "Energy.Active.Import.Register",
                                    "unit": "Wh",
                                    "context": "Sample.Periodic",
                                }
                            ],
                        }
                    ],
                    transaction_id=7,
                ),
                suppress=False,
            )
            srv = cs.charge_points[cp_id]
            assert srv._metrics[(1, csess.meter_start)].value == restored
            assert srv._metrics[(1, csess.session_energy)].value == pytest.approx(3.974)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    await remove_configuration(hass, entry)
