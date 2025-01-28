"""Test ocpp config flow."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant import config_entries, data_entry_flow
import pytest

from custom_components.ocpp.const import (  # BINARY_SENSOR,; PLATFORMS,; SENSOR,; SWITCH,
    DOMAIN,
)

from .const import MOCK_CONFIG_CS, MOCK_CONFIG_CP, MOCK_CONFIG_FLOW, CONF_CPIDS, CONF_MONITORED_VARIABLES_AUTOCONFIG


# This fixture bypasses the actual setup of the integration
# since we only want to test the config flow. We test the
# actual functionality of the integration in other test modules.
@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent setup."""
    with (
        patch(
            "custom_components.ocpp.async_setup",
            return_value=True,
        ),
        patch(
            "custom_components.ocpp.async_setup_entry",
            return_value=True,
        ),
    ):
        yield


# Here we simiulate a successful config flow from the backend.
# Note that we use the `bypass_get_data` fixture here because
# we want the config flow validation to succeed during the test.
async def test_successful_config_flow(hass, bypass_get_data):
    """Test a successful config flow."""
    # Initialize a config flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    # Remove cpids key as it gets added in flow
    config = MOCK_CONFIG_CS.copy()
    config.pop(CONF_CPIDS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=config
    )

    # Check that the config flow is complete and a new entry is created with
    # the input data
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "test_csid_flow"
    assert result["data"] == MOCK_CONFIG_CS
    assert result["result"]


async def test_successful_discovery_flow(hass, bypass_get_data):
    """Test a discovery config flow."""
    # Mock the config flow for the central system
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_CS,
        entry_id="test_cms_disc",
        title="test_cms_disc",
        version=2,
        minor_version=0,
    )
    # Need to ensure data entry exists as skipped init.py setup
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entry = hass.config_entries._entries.get_entries_for_domain(DOMAIN)[0]
    info = {"cp_id": "test_cp_id", "entry": entry}
    # data here is discovery_info not user_input
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data=info,
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "cp_user"
    result["discovery_info"] = info

    # Switch to manual measurand selection to test full flow
    input = MOCK_CONFIG_CP.copy()
    input[CONF_MONITORED_VARIABLES_AUTOCONFIG] = False
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=input
    )

    result3 = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=DEFAULT_MONITORED_VARIABLES
    )

    # Check that the config flow is complete and a new entry is created with
    # the input data
    output = MOCK_CONFIG_FLOW.copy()
    output[CONF_CPIDS][-1]["test_cp_id"][CONF_MONITORED_VARIABLES_AUTOCONFIG] = False
    assert result3["type"] == data_entry_flow.FlowResultType.ABORT
    entry = hass.config_entries._entries.get_entries_for_domain(DOMAIN)[0]
    assert entry.data == output


# In this case, we want to simulate a failure during the config flow.
# We use the `error_on_get_data` mock instead of `bypass_get_data`
# (note the function parameters) to raise an Exception during
# validation of the input config.
# async def test_failed_config_flow(hass, error_on_get_data):
#     """Test a failed config flow due to credential validation failure."""
#
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )
#
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["step_id"] == "user"
#
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=MOCK_CONFIG
#     )
#
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["errors"] == {"base": "auth"}
#
#
# # Our config flow also has an options flow, so we must test it as well.
# async def test_options_flow(hass):
#     """Test an options flow."""
#     # Create a new MockConfigEntry and add to HASS (we're bypassing config
#     # flow entirely)
#     entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
#     entry.add_to_hass(hass)
#
#     # Initialize an options flow
#     await hass.config_entries.async_setup(entry.entry_id)
#     result = await hass.config_entries.options.async_init(entry.entry_id)
#
#     # Verify that the first options step is a user form
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["step_id"] == "user"
#
#     # Enter some fake data into the form
#     result = await hass.config_entries.options.async_configure(
#         result["flow_id"],
#         user_input={platform: platform != SENSOR for platform in PLATFORMS},
#     )
#
#     # Verify that the flow finishes
#     assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
#     assert result["title"] == "test_username"
#
#     # Verify that the options were updated
#     assert entry.options == {BINARY_SENSOR: True, SENSOR: False, SWITCH: True}
