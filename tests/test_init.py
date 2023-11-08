"""Test ocpp setup process."""
# from homeassistant.exceptions import ConfigEntryNotReady
# import pytest
from typing import AsyncGenerator

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp import (
    CentralSystem,
    async_reload_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ocpp.const import DOMAIN

from .const import MOCK_CONFIG_DATA_1


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_setup_unload_and_reload_entry(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None
):
    """Test entry setup and unload."""
    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG_DATA_1, entry_id="test_cms1", title="test_cms1"
    )
    # config_entry.add_to_hass(hass);
    hass.config_entries._entries[config_entry.entry_id] = config_entry

    # Set up the entry and assert that the values set during setup are where we expect
    # them to be. Because we have patched the ocppDataUpdateCoordinator.async_get_data
    # call, no code from custom_components/ocpp/api.py actually runs.
    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()

    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem

    # Reload the entry and assert that the data from above is still there
    assert await async_reload_entry(hass, config_entry) is None
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem

    # Unload the entry and verify that the data has been removed
    unloaded = await async_unload_entry(hass, config_entry)
    assert unloaded
    assert config_entry.entry_id not in hass.data[DOMAIN]


# async def test_setup_entry_exception(hass, error_on_get_data):
#     """Test ConfigEntryNotReady when API raises an exception during entry setup."""
#     config_entry = MockConfigEntry(
#         domain=DOMAIN, data=MOCK_CONFIG_DATA, entry_id="test"
#     )
#     config_entry.add_to_hass(config_entry)
#
#     # In this case we are testing the condition where async_setup_entry raises
#     # ConfigEntryNotReady using the `error_on_get_data` fixture which simulates
#     # an error.
#     with pytest.raises(ConfigEntryNotReady):
#         assert await async_setup_entry(hass, config_entry)
