"""Test sensor for ocpp integration."""

import asyncio
import websockets
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import DOMAIN as OCPP_DOMAIN

from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.components.sensor.const import (
    SensorDeviceClass,
    SensorStateClass,
    ATTR_STATE_CLASS,
)
from .const import (
    MOCK_CONFIG_DATA,
    CONF_CPIDS,
    MOCK_CONFIG_CP_APPEND,
    CONF_PORT,
    CONF_CPID,
)
from .charge_point_test import create_configuration, remove_configuration


async def test_sensor(hass, socket_enabled):
    """Test sensor."""

    cp_id = "CP_1_sens"
    cpid = "test_cpid_sens"
    data = MOCK_CONFIG_DATA.copy()
    cp_data = MOCK_CONFIG_CP_APPEND.copy()
    cp_data[CONF_CPID] = cpid
    data[CONF_CPIDS].append({cp_id: cp_data})
    data[CONF_PORT] = 9015
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=data,
        entry_id="test_cms_sens",
        title="test_cms_sens",
        version=2,
        minor_version=0,
    )

    # start clean entry for server
    await create_configuration(hass, config_entry)

    # connect to websocket to trigger charger setup
    async with websockets.connect(
        f"ws://127.0.0.1:{data[CONF_PORT]}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ):
        # Wait for setup to complete
        await asyncio.sleep(1)
        # Test reactive power sensor
        state = hass.states.get(f"sensor.{cpid}_power_reactive_import")
        assert (
            state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.REACTIVE_POWER
        )
        assert state.attributes.get(ATTR_STATE_CLASS) == SensorStateClass.MEASUREMENT
        # Test reactive energy sensor, not having own device class yet
        state = hass.states.get(f"sensor.{cpid}_energy_reactive_import_register")
        assert state.attributes.get(ATTR_DEVICE_CLASS) is None
        assert state.attributes.get(ATTR_STATE_CLASS) is None

    await remove_configuration(hass, config_entry)
