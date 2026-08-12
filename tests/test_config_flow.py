"""Test ocpp config flow."""

from copy import deepcopy
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant import config_entries, data_entry_flow
from homeassistant.data_entry_flow import InvalidData
import pytest

from custom_components.ocpp.const import (
    CONF_ENABLE_HA_NOTIFICATIONS,
    CONF_NUM_CONNECTORS,
    DEFAULT_NUM_CONNECTORS,
    DOMAIN,
)

from .const import (
    MOCK_CONFIG_CS,
    MOCK_CONFIG_CP,
    MOCK_CONFIG_FLOW,
    CONF_CPID,
    CONF_CPIDS,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    DEFAULT_MONITORED_VARIABLES,
)


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
    result_disc = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data=info,
    )

    # Check that the config flow shows the user form as the first step
    assert result_disc["type"] == data_entry_flow.FlowResultType.FORM
    assert result_disc["step_id"] == "cp_user"
    result_disc["discovery_info"] = info

    # Switch to manual measurand selection to test full flow
    cp_input = MOCK_CONFIG_CP.copy()
    cp_input[CONF_MONITORED_VARIABLES_AUTOCONFIG] = False
    cp_input[CONF_ENABLE_HA_NOTIFICATIONS] = False
    result_cp = await hass.config_entries.flow.async_configure(
        result_disc["flow_id"], user_input=cp_input
    )

    measurand_input = dict.fromkeys(DEFAULT_MONITORED_VARIABLES.split(","), True)
    result_meas = await hass.config_entries.flow.async_configure(
        result_cp["flow_id"], user_input=measurand_input
    )

    # Check that the config flow is complete and a new entry is created with
    # the input data
    flow_output = deepcopy(MOCK_CONFIG_FLOW)
    flow_output[CONF_CPIDS][-1]["test_cp_id"][CONF_MONITORED_VARIABLES_AUTOCONFIG] = (
        False
    )
    flow_output[CONF_CPIDS][-1]["test_cp_id"][CONF_NUM_CONNECTORS] = (
        DEFAULT_NUM_CONNECTORS
    )
    flow_output[CONF_CPIDS][-1]["test_cp_id"][CONF_ENABLE_HA_NOTIFICATIONS] = False

    assert result_meas["type"] == data_entry_flow.FlowResultType.ABORT
    entry = hass.config_entries._entries.get_entries_for_domain(DOMAIN)[0]
    assert entry.data == flow_output

    # Test different CP IDs are allowed
    info2 = {"cp_id": "different_cp_id", "entry": entry}
    result2_disc = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data=info2,
    )
    # Check that the config flow shows the user form as the first step
    assert result2_disc["type"] == data_entry_flow.FlowResultType.FORM
    assert result2_disc["step_id"] == "cp_user"
    result2_disc["discovery_info"] = info2

    # Use a different cpid too: cpid (not cp_id) is what entity unique_ids
    # are built from, so two charge points sharing one would collide.
    cp2_input = MOCK_CONFIG_CP.copy()
    cp2_input[CONF_CPID] = "test_cpid_2"
    result_cp2 = await hass.config_entries.flow.async_configure(
        result2_disc["flow_id"], user_input=cp2_input
    )

    assert result_cp2["type"] == data_entry_flow.FlowResultType.ABORT
    # Check there are 2 cpid entries
    assert len(entry.data[CONF_CPIDS]) == 2


async def test_duplicate_cpid_discovery_flow(hass, bypass_get_data):
    """Test discovery flow with duplicate CP ID."""
    # Setup first charger
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_CS,
        entry_id="test_cms_disc",
        title="test_cms_disc",
        version=2,
    )
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Try to add same CP ID twice
    entry = hass.config_entries._entries.get_entries_for_domain(DOMAIN)[0]
    info = {"cp_id": "test_cp_id", "entry": entry}

    # First discovery should succeed
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data=info,
    )
    assert result1["type"] == data_entry_flow.FlowResultType.FORM

    # Second discovery with same CP ID should abort
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data=info,
    )
    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "already_in_progress"


async def test_duplicate_cpid_value_rejected(hass, bypass_get_data):
    """Reject a second, different charge point that reuses an already-used cpid.

    cp_id (the OCPP websocket identity) and cpid (the user-chosen name used
    to build entity unique_ids) are different things. Two distinct chargers
    (distinct cp_id) must not be allowed to share the same cpid, otherwise
    their entities collide on unique_id.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_CS,
        entry_id="test_cms_dup_cpid",
        title="test_cms_dup_cpid",
        version=2,
    )
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entry = hass.config_entries._entries.get_entries_for_domain(DOMAIN)[0]

    # First charge point (cp_id "cp_a") configures cpid "shared_cpid".
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": "cp_a", "entry": entry},
    )
    cp1_input = MOCK_CONFIG_CP.copy()
    cp1_input[CONF_CPID] = "shared_cpid"
    result1 = await hass.config_entries.flow.async_configure(
        result1["flow_id"], user_input=cp1_input
    )
    assert result1["type"] == data_entry_flow.FlowResultType.ABORT
    assert len(entry.data[CONF_CPIDS]) == 1

    # Second, different charge point (cp_id "cp_b") tries to reuse "shared_cpid".
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": "cp_b", "entry": entry},
    )
    cp2_input = MOCK_CONFIG_CP.copy()
    cp2_input[CONF_CPID] = "shared_cpid"
    result2 = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=cp2_input
    )

    # Rejected with a form error, not silently accepted.
    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["step_id"] == "cp_user"
    assert result2["errors"]["base"] == "duplicate_cpid"
    # No second charge point was added to this entry.
    assert len(entry.data[CONF_CPIDS]) == 1

    # Retrying with a genuinely unique cpid succeeds.
    cp2_input[CONF_CPID] = "cp_b_cpid"
    result3 = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=cp2_input
    )
    assert result3["type"] == data_entry_flow.FlowResultType.ABORT
    assert len(entry.data[CONF_CPIDS]) == 2


async def test_duplicate_cpid_value_rejected_across_entries(hass, bypass_get_data):
    """A cpid must be unique across every OCPP config entry (central system).

    Entity unique_id is built from DOMAIN + cpid only, with no per-entry
    scoping, so a second central system reusing a cpid already used by a
    charge point on a different central system would also collide.
    """
    entry_a = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_CS, "port": 9101},
        entry_id="test_cms_dup_cpid_a",
        title="test_cms_dup_cpid_a",
        version=2,
    )
    entry_b_data = {**MOCK_CONFIG_CS, "port": 9102}
    from custom_components.ocpp.const import CONF_CSID

    entry_b_data[CONF_CSID] = "test_csid_flow_b"
    entry_b = MockConfigEntry(
        domain=DOMAIN,
        data=entry_b_data,
        entry_id="test_cms_dup_cpid_b",
        title="test_cms_dup_cpid_b",
        version=2,
    )
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    # Setting up the first entry also brings up the "ocpp" component, which
    # in turn loads every other already-registered entry of that domain
    # (entry_b included), so a second explicit async_setup call for entry_b
    # would find it already loaded.
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()
    assert entry_a.state is config_entries.ConfigEntryState.LOADED
    assert entry_b.state is config_entries.ConfigEntryState.LOADED

    # Charge point on central system A takes "shared_cpid".
    result_a = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": "cp_on_a", "entry": entry_a},
    )
    cp_a_input = MOCK_CONFIG_CP.copy()
    cp_a_input[CONF_CPID] = "shared_cpid"
    result_a = await hass.config_entries.flow.async_configure(
        result_a["flow_id"], user_input=cp_a_input
    )
    assert result_a["type"] == data_entry_flow.FlowResultType.ABORT

    # A charge point on the unrelated central system B tries to reuse it.
    result_b = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": "cp_on_b", "entry": entry_b},
    )
    cp_b_input = MOCK_CONFIG_CP.copy()
    cp_b_input[CONF_CPID] = "shared_cpid"
    result_b = await hass.config_entries.flow.async_configure(
        result_b["flow_id"], user_input=cp_b_input
    )

    assert result_b["type"] == data_entry_flow.FlowResultType.FORM
    assert result_b["errors"]["base"] == "duplicate_cpid"


async def test_duplicate_cpid_rejected_when_cp_id_matches_on_another_entry(
    hass, bypass_get_data
):
    """Two central systems can each have a charge point with the same cp_id.

    cp_id is the OCPP-level identity, chosen by the charger, so it is not
    unique across central systems. Excluding a record on cp_id alone would
    also exclude the *other* system's charge point and let its cpid be reused.
    """
    from custom_components.ocpp.const import CONF_CSID

    entry_a = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_CS, "port": 9111},
        entry_id="test_cms_same_cpid_a",
        title="test_cms_same_cpid_a",
        version=2,
    )
    entry_b_data = {**MOCK_CONFIG_CS, "port": 9112}
    entry_b_data[CONF_CSID] = "test_csid_same_cp_id_b"
    entry_b = MockConfigEntry(
        domain=DOMAIN,
        data=entry_b_data,
        entry_id="test_cms_same_cpid_b",
        title="test_cms_same_cpid_b",
        version=2,
    )
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    # Both chargers announce themselves with the *same* cp_id.
    compartido = "charger"

    result_a = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": compartido, "entry": entry_a},
    )
    cp_a_input = MOCK_CONFIG_CP.copy()
    cp_a_input[CONF_CPID] = "taken_cpid"
    result_a = await hass.config_entries.flow.async_configure(
        result_a["flow_id"], user_input=cp_a_input
    )
    assert result_a["type"] == data_entry_flow.FlowResultType.ABORT

    # The one on B shares the cp_id but is a different charge point, so it
    # must not be able to take the cpid already in use on A.
    result_b = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": compartido, "entry": entry_b},
    )
    cp_b_input = MOCK_CONFIG_CP.copy()
    cp_b_input[CONF_CPID] = "taken_cpid"
    result_b = await hass.config_entries.flow.async_configure(
        result_b["flow_id"], user_input=cp_b_input
    )

    assert result_b["type"] == data_entry_flow.FlowResultType.FORM
    assert result_b["errors"]["base"] == "duplicate_cpid"


async def test_duplicate_cpid_caught_at_measurands_step(hass, bypass_get_data):
    """With autoconfig off the cpid is validated a step before it is written.

    Another flow can take it in between, so the check is repeated immediately
    before persisting. Simulated here by writing the colliding cpid into the
    entry after the first validation has already passed.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_CS, "port": 9121},
        entry_id="test_cms_measurands_race",
        title="test_cms_measurands_race",
        version=2,
    )
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": "cp_race", "entry": entry},
    )
    cp_input = MOCK_CONFIG_CP.copy()
    cp_input[CONF_CPID] = "raced_cpid"
    cp_input[CONF_MONITORED_VARIABLES_AUTOCONFIG] = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=cp_input
    )
    assert result["step_id"] == "measurands"

    # Someone else persists the same cpid while this flow sits on the
    # measurands form.
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_CPIDS: [{"other_cp": {CONF_CPID: "raced_cpid"}}],
        },
    )

    measurands = dict.fromkeys(DEFAULT_MONITORED_VARIABLES.split(","), True)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=measurands
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "duplicate_cpid"


async def test_reconfigure_own_cpid_not_flagged_duplicate(hass, bypass_get_data):
    """Resubmitting a charge point's own cpid must not be flagged as a duplicate.

    The duplicate check excludes the cp_id currently being configured, so a
    charge point being (re)configured is never compared against itself.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_CS,
        entry_id="test_cms_self_cpid",
        title="test_cms_self_cpid",
        version=2,
    )
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entry = hass.config_entries._entries.get_entries_for_domain(DOMAIN)[0]

    # Configure the charge point once.
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": "cp_self", "entry": entry},
    )
    cp_input = MOCK_CONFIG_CP.copy()
    cp_input[CONF_CPID] = "self_cpid"
    result1 = await hass.config_entries.flow.async_configure(
        result1["flow_id"], user_input=cp_input
    )
    assert result1["type"] == data_entry_flow.FlowResultType.ABORT

    # Run the cp_user step again for the same cp_id, submitting the same
    # cpid it already owns. It must not be treated as a duplicate of itself.
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"cp_id": "cp_self", "entry": entry},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=cp_input
    )
    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert "errors" not in result2 or not result2.get("errors")


async def test_failed_config_flow(hass, error_on_get_data):
    """Test failed config flow scenarios."""
    # Test invalid central system configuration
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    # Test with invalid input data, includes cpids
    invalid_config = MOCK_CONFIG_CS.copy()

    with pytest.raises(InvalidData):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=invalid_config
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM


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


async def test_reconfigure_flow(hass, bypass_get_data):
    """Test reconfiguring an existing entry (e.g. to pin the OCPP version)."""
    from custom_components.ocpp.const import CONF_OCPP_VERSION

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_CS.copy(),
        entry_id="test_reconf",
        version=2,
        minor_version=1,
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    # Change the OCPP version pin, keep everything else
    new_input = MOCK_CONFIG_CS.copy()
    new_input.pop(CONF_CPIDS)
    new_input[CONF_OCPP_VERSION] = "2.0.1"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=new_input
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_OCPP_VERSION] == "2.0.1"
    # cpid settings must be preserved across reconfigure
    assert CONF_CPIDS in entry.data


async def test_reconfigure_does_not_schedule_second_reload(hass, bypass_get_data):
    """Reconfigure must not reload on top of the entry-update listener.

    async_setup_entry registers add_update_listener(async_reload_entry), so
    updating the entry already reloads it. Scheduling another reload from the
    flow overlaps the two: the websocket server is rebound while the first
    setup is still in flight and the platform forwards fail with "config entry
    ... has already been setup".
    """
    from custom_components.ocpp.const import CONF_OCPP_VERSION

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_CS.copy(),
        entry_id="test_reconf_reload",
        version=2,
        minor_version=1,
    )
    entry.add_to_hass(hass)

    scheduled: list[str] = []
    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        side_effect=lambda entry_id: scheduled.append(entry_id),
    ):
        result = await entry.start_reconfigure_flow(hass)
        new_input = MOCK_CONFIG_CS.copy()
        new_input.pop(CONF_CPIDS)
        new_input[CONF_OCPP_VERSION] = "2.0.1"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=new_input
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert entry.data[CONF_OCPP_VERSION] == "2.0.1"
    assert scheduled == [], (
        "reconfigure scheduled its own reload; the update listener already "
        f"reloads the entry (scheduled={scheduled})"
    )
