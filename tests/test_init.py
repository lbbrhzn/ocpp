"""Test ocpp setup process."""

# from homeassistant.exceptions import ConfigEntryNotReady
# import pytest
from collections.abc import AsyncGenerator
from copy import deepcopy

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp import CentralSystem, async_migrate_entry
from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_ENABLE_HA_NOTIFICATIONS,
    DEFAULT_ENABLE_HA_NOTIFICATIONS,
    DOMAIN,
)

from .const import (
    MOCK_CONFIG_DATA,
    MOCK_CONFIG_DATA_1,
    MOCK_CONFIG_MIGRATION_FLOW,
    MOCK_CONFIG_DATA_1_MC,
)
from .lifecycle_asserts import (
    assert_no_swallowed_lifecycle_errors,
    assert_platforms_hold,
    assert_rebuilt,
    live_entity,
)

# Both single- and multi-connector mocks configure cpid "test_cpid_9001"
# with at least one connector, so this entity exists in each lifecycle
# test and its identity across a reload is the load-bearing assertion.
MAX_CURRENT_EID = "number.test_cpid_9001_connector_1_maximum_current"


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_setup_unload_and_reload_entry(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None, caplog
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
    # Structural: the forward actually registered every entity platform.
    assert_platforms_hold(hass, config_entry.entry_id)
    entity_before = live_entity(hass, MAX_CURRENT_EID, "number")

    # Reload the entry and assert that the data from above is still there
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem
    # Structural: fresh platforms, fresh entity - not the old ones surviving.
    assert_platforms_hold(hass, config_entry.entry_id)
    assert_rebuilt(entity_before, live_entity(hass, MAX_CURRENT_EID, "number"))

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert_no_swallowed_lifecycle_errors(caplog)


async def test_setup_unload_and_reload_entry_multiple_connectors(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None, caplog
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
    assert_platforms_hold(hass, config_entry.entry_id)
    entity_before = live_entity(hass, MAX_CURRENT_EID, "number")

    # Reload the entry and assert that the data from above is still there
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem
    assert_platforms_hold(hass, config_entry.entry_id)
    assert_rebuilt(entity_before, live_entity(hass, MAX_CURRENT_EID, "number"))

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert_no_swallowed_lifecycle_errors(caplog)


async def test_migration_entry(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None, caplog
):
    """Test entry migration."""
    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_MIGRATION_FLOW,
        entry_id="test_migration",
        title="test_migration",
        version=1,
        minor_version=1,
    )
    config_entry.add_to_hass(hass)
    await hass.async_block_till_done()

    # Ensure cp id is present in state machine to trigger migration flow.
    # This simulates a user with the id sensor still in HA, holding the
    # charger's OCPP cp_id as its VALUE. The value is deliberately
    # different from the cpid: they are different things in production
    # (OCPP identity vs friendly name), and seeding them equal would let
    # a migration that stored the cpid pass the key assertion below.
    hass.states.async_set(
        f"sensor.{MOCK_CONFIG_MIGRATION_FLOW[CONF_CPID].lower()}_id",
        "CP_migration_1",
    )

    # Set up the entry and assert that the values set during setup are where we expect
    # them to be. Because we have patched the ocppDataUpdateCoordinator.async_get_data
    # call, no code from custom_components/ocpp/api.py actually runs.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data
    assert config_entry.entry_id in hass.data[DOMAIN]
    assert type(hass.data[DOMAIN][config_entry.entry_id]) is CentralSystem
    # The migrated entry's setup must have forwarded the platforms too.
    assert_platforms_hold(hass, config_entry.entry_id)
    # check migration has created new entry with correct keys
    assert config_entry.data.keys() == MOCK_CONFIG_DATA.keys()
    # The charge point key must be the seeded sensor's VALUE, as a plain
    # string. The migration stored the whole State object until 2026-08 -
    # unserialisable entry data and a key no connecting charger could
    # match - and this test could not see it, because a global
    # StateMachine.get patch replaced the state seeded above with a
    # synthetic one and only the top-level keys were checked.
    migrated_key = next(iter(config_entry.data[CONF_CPIDS][0]))
    assert isinstance(migrated_key, str)
    assert migrated_key == "CP_migration_1"
    # check versions match
    assert config_entry.version == 2
    assert config_entry.minor_version == 2

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert_no_swallowed_lifecycle_errors(caplog)


async def test_migration_adds_notification_preference_to_existing_chargers(
    hass: HomeAssistant,
):
    """Version 2.1 entries get the per-charger notification default."""
    old_data = deepcopy(MOCK_CONFIG_DATA_1)
    for cp_map in old_data[CONF_CPIDS]:
        for cp_data in cp_map.values():
            cp_data.pop(CONF_ENABLE_HA_NOTIFICATIONS, None)

    cp_map = old_data[CONF_CPIDS][0]
    second_cp_data = deepcopy(next(iter(cp_map.values())))
    second_cp_data[CONF_CPID] = "test_cpid_9002"
    cp_map["CP_2"] = second_cp_data
    cp_map["legacy_value"] = "preserved"

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=old_data,
        entry_id="test_notification_migration",
        version=2,
        minor_version=1,
    )
    config_entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, config_entry)
    migrated_cp_map = config_entry.data[CONF_CPIDS][0]
    assert migrated_cp_map.keys() == cp_map.keys()
    assert all(
        cp_data[CONF_ENABLE_HA_NOTIFICATIONS] is DEFAULT_ENABLE_HA_NOTIFICATIONS
        for cp_data in migrated_cp_map.values()
        if isinstance(cp_data, dict)
    )
    assert migrated_cp_map["legacy_value"] == "preserved"
    assert config_entry.version == 2
    assert config_entry.minor_version == 2


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


async def test_migration_refuses_a_sentinel_charger_id(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None, caplog
):
    """An 'unavailable' id sensor must fail migration, not become the key.

    Sentinel states are plain strings, so without the guard they pass an
    isinstance check and get stored as the charge point key - JSON-clean,
    but matching no charger ever: the same broken entry the State-object
    bug produced, by a quieter route.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_MIGRATION_FLOW,
        entry_id="test_migration_sentinel",
        title="test_migration_sentinel",
        version=1,
        minor_version=1,
    )
    config_entry.add_to_hass(hass)
    await hass.async_block_till_done()

    hass.states.async_set(
        f"sensor.{MOCK_CONFIG_MIGRATION_FLOW[CONF_CPID].lower()}_id",
        "unavailable",
    )

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.version == 1
    # The refusal is a clean, deliberate failure ("Error migrating entry"
    # from core) - none of the swallowed-failure signatures may appear.
    assert_no_swallowed_lifecycle_errors(caplog)


async def test_migration_finds_a_non_slug_cpid(
    hass: AsyncGenerator[HomeAssistant, None], bypass_get_data: None, caplog
):
    """A cpid like 'Garage Charger' must still resolve its id sensor.

    The sensor platform slugifies object ids, so the entity really lives
    at sensor.garage_charger_id - a migration lookup built with .lower()
    would ask for 'sensor.garage charger_id' and always miss, sending the
    user to a clean install for no reason.
    """
    data = {**MOCK_CONFIG_MIGRATION_FLOW, CONF_CPID: "Garage Charger"}
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        entry_id="test_migration_slug",
        title="test_migration_slug",
        version=1,
        minor_version=1,
    )
    config_entry.add_to_hass(hass)
    await hass.async_block_till_done()

    hass.states.async_set("sensor.garage_charger_id", "CP_migration_2")

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert_platforms_hold(hass, config_entry.entry_id)
    migrated_key = next(iter(config_entry.data[CONF_CPIDS][0]))
    assert migrated_key == "CP_migration_2"

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert_no_swallowed_lifecycle_errors(caplog)
