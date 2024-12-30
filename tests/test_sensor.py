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


async def test_sensor(hass, socket_enabled):
    """Test sensor."""
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=MOCK_CONFIG_DATA,
        entry_id="test_cms",
        title="test_cms",
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Test reactive power sensor
    state = hass.states.get("sensor.test_cpid_power_reactive_import")
    assert state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.REACTIVE_POWER
    assert state.attributes.get(ATTR_STATE_CLASS) == SensorStateClass.MEASUREMENT
    # Test reactive energx sensor, not having own device class yet
    state = hass.states.get("sensor.test_cpid_energy_reactive_import_register")
    assert state.attributes.get(ATTR_DEVICE_CLASS) is None
    assert state.attributes.get(ATTR_STATE_CLASS) is None
