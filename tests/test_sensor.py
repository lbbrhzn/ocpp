"""Test sensor for ocpp integration."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import DOMAIN as OCPP_DOMAIN

from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.components.sensor.const import (
    SensorDeviceClass,
    SensorStateClass,
    ATTR_STATE_CLASS,
)
from .const import MOCK_CONFIG_DATA
from .charge_point_test import create_configuration, remove_configuration


async def test_sensor(hass, socket_enabled):
    """Test sensor."""
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=MOCK_CONFIG_DATA,
        entry_id="test_cms_sens",
        title="test_cms_sens",
        version=2,
        minor_version=0,
    )

    # start clean entry for services
    await create_configuration(hass, config_entry)

    # Test reactive power sensor
    state = hass.states.get("sensor.test_cpid_power_reactive_import")
    assert state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.REACTIVE_POWER
    assert state.attributes.get(ATTR_STATE_CLASS) == SensorStateClass.MEASUREMENT
    # Test reactive energy sensor, not having own device class yet
    state = hass.states.get("sensor.test_cpid_energy_reactive_import_register")
    assert state.attributes.get(ATTR_DEVICE_CLASS) is None
    assert state.attributes.get(ATTR_STATE_CLASS) is None

    await remove_configuration(hass, config_entry)
