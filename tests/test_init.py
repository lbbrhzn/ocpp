"""Test ocpp setup process."""

# from homeassistant.exceptions import ConfigEntryNotReady
# import pytest
from collections.abc import AsyncGenerator

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp import CentralSystem
from custom_components.ocpp.const import DOMAIN

from .const import (
    MOCK_CONFIG_DATA,
    MOCK_CONFIG_DATA_1,
    MOCK_CONFIG_MIGRATION_FLOW,
    MOCK_CONFIG_DATA_1_MC,
)


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
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA_1,
        entry_id="test_cms1",
        title="test_cms1",
        version=2,
        minor_version=0,
    )
    config_entry.add_to_hass(hass)
    await hass.async_block_till_done()

    # Set up the entry and assert that the values set during setup are where we expect
    # them to be. Because we have patched the ocppDataUpdateCoordinator.async_get_data
    # call, no code from custom_components/ocpp/api.py actually runs.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data
    assert config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem

    # Reload the entry and assert that the data from above is still there
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data[DOMAIN]


async def test_setup_unload_and_reload_entry_multiple_connectors(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None
):
    """Test entry setup and unload."""
    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA_1_MC,
        entry_id="test_cms1_mc",
        title="test_cms1_mc",
        version=2,
        minor_version=1,
    )
    config_entry.add_to_hass(hass)
    await hass.async_block_till_done()

    # Set up the entry and assert that the values set during setup are where we expect
    # them to be. Because we have patched the ocppDataUpdateCoordinator.async_get_data
    # call, no code from custom_components/ocpp/api.py actually runs.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data
    assert config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem

    # Reload the entry and assert that the data from above is still there
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data[DOMAIN]


async def test_migration_entry(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None
):
    """Test entry migration."""
    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_MIGRATION_FLOW,
        entry_id="test_migration",
        title="test_migration",
        version=1,
    )
    config_entry.add_to_hass(hass)
    await hass.async_block_till_done()

    # Set up the entry and assert that the values set during setup are where we expect
    # them to be. Because we have patched the ocppDataUpdateCoordinator.async_get_data
    # call, no code from custom_components/ocpp/api.py actually runs.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data
    assert config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem
    # check migration has created new entry with correct keys
    assert config_entry.data.keys() == MOCK_CONFIG_DATA.keys()
    # check versions match
    assert config_entry.version == 2
    assert config_entry.minor_version == 1

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
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
