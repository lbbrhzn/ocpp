"""The options flow for editing an already-configured charge point.

The initial charger form (async_step_cp_user) is reachable only from
integration discovery, and discovery aborts for a charger that is already
configured - so every per-charger setting was write-once (#2047). Anyone
who enabled force_smart_charging to work around missing detection could
never turn it off, and max_current - which since #2044 decides when
set_charge_rate clears the profile instead of applying a limit - could
never follow a supply upgrade.

cpid stays read-only throughout: entity unique_ids derive from it, so
changing it would orphan every existing entity.
"""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant import data_entry_flow

from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_ENABLE_HA_NOTIFICATIONS,
    CONF_FORCE_SMART_CHARGING,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_NUM_CONNECTORS,
    CONF_SKIP_SCHEMA_VALIDATION,
    DOMAIN,
)

from .const import MOCK_CONFIG_CS, MOCK_CONFIG_CP


@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent actual setup of the integration."""
    with (
        patch("custom_components.ocpp.async_setup", return_value=True),
        patch("custom_components.ocpp.async_setup_entry", return_value=True),
    ):
        yield


def _cp_settings(**overrides):
    """Build a stored charge point settings dict, as discovery writes it."""
    settings = {
        **MOCK_CONFIG_CP,
        CONF_NUM_CONNECTORS: 2,
        CONF_MONITORED_VARIABLES: "Power.Active.Import,Voltage",
    }
    settings.update(overrides)
    return settings


def _entry(hass, cpids):
    """Build a configured central system with the given charge points."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_CS, CONF_CPIDS: cpids},
        entry_id="test_options",
        title="test_csid_flow",
    )
    entry.add_to_hass(hass)
    return entry


def _stored(entry, cp_id):
    """Return the stored settings for cp_id after the flow ran."""
    for item in entry.data[CONF_CPIDS]:
        if cp_id in item:
            return item[cp_id]
    raise AssertionError(f"{cp_id} missing from entry data")


def _form_fields(result):
    """Return the field names offered by a form step."""
    return {str(key) for key in result["data_schema"].schema}


async def test_a_single_charger_skips_the_picker(hass):
    """With one charge point there is nothing to choose; go straight to it."""
    entry = _entry(hass, [{"CP_1": _cp_settings()}])

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "cp_settings"


async def test_cpid_is_not_editable(hass):
    """drc38 on #2047: cpid should be read only.

    Entity unique_ids are built from it, so an edit would orphan every
    entity of the charger. It is shown in the description instead.
    """
    entry = _entry(hass, [{"CP_1": _cp_settings()}])

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert CONF_CPID not in _form_fields(result)
    assert result["description_placeholders"] == {
        "cp_id": "CP_1",
        "cpid": "test_cpid",
    }


async def test_editing_settings_preserves_what_the_form_does_not_show(hass):
    """cpid, connector count and the measurand list must ride along unchanged.

    num_connectors is detection state, not preference - dropping it recreates
    the num_connectors=0 class of bug where entities disappear. The measurand
    list holds what detection accepted; resetting it as a side effect of an
    unrelated edit would spawn sensors for every measurand.
    """
    entry = _entry(hass, [{"CP_1": _cp_settings()}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAX_CURRENT: 63,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
            CONF_METER_INTERVAL: 30,
            CONF_IDLE_INTERVAL: 600,
            CONF_SKIP_SCHEMA_VALIDATION: True,
            CONF_FORCE_SMART_CHARGING: False,
            CONF_ENABLE_HA_NOTIFICATIONS: False,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    stored = _stored(entry, "CP_1")
    assert stored[CONF_MAX_CURRENT] == 63
    assert stored[CONF_SKIP_SCHEMA_VALIDATION] is True
    assert stored[CONF_FORCE_SMART_CHARGING] is False
    assert stored[CONF_ENABLE_HA_NOTIFICATIONS] is False
    assert stored[CONF_METER_INTERVAL] == 30
    assert stored[CONF_IDLE_INTERVAL] == 600
    # Not shown by the form, must survive:
    assert stored[CONF_CPID] == "test_cpid"
    assert stored[CONF_NUM_CONNECTORS] == 2
    assert stored[CONF_MONITORED_VARIABLES] == "Power.Active.Import,Voltage"
    # Settings stay in entry.data; nothing moves into entry.options.
    assert entry.options == {}


async def test_the_entry_is_updated_exactly_once(hass):
    """One write, one reload - the double-reload bug must stay dead.

    async_setup_entry registers async_reload_entry as the update listener,
    so every async_update_entry costs a reload. The reconfigure step was
    fixed to update once and let the listener do the single reload; the
    options flow has to hold the same line, including the empty
    create_entry at the end which must not count as a second update.
    """
    entry = _entry(hass, [{"CP_1": _cp_settings()}])
    listener = AsyncMock()
    entry.add_update_listener(listener)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAX_CURRENT: 40,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
            CONF_METER_INTERVAL: 60,
            CONF_IDLE_INTERVAL: 900,
            CONF_SKIP_SCHEMA_VALIDATION: False,
            CONF_FORCE_SMART_CHARGING: True,
        },
    )
    await hass.async_block_till_done()

    assert listener.await_count == 1


async def test_autoconfig_left_on_does_not_reseed_measurands(hass):
    """The discovery flow seeds DEFAULT_MONITORED_VARIABLES; this flow must not.

    At discovery there is nothing better to seed with. Here the stored list
    is what detection accepted on a real connect, and re-detection refreshes
    it next connect anyway - reseeding to the full set would create sensors
    for every measurand as a side effect of editing max_current.
    """
    entry = _entry(
        hass,
        [{"CP_1": _cp_settings(**{CONF_MONITORED_VARIABLES: "Power.Active.Import"})}],
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAX_CURRENT: 63,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
            CONF_METER_INTERVAL: 60,
            CONF_IDLE_INTERVAL: 900,
            CONF_SKIP_SCHEMA_VALIDATION: False,
            CONF_FORCE_SMART_CHARGING: True,
        },
    )

    assert _stored(entry, "CP_1")[CONF_MONITORED_VARIABLES] == "Power.Active.Import"


async def test_autoconfig_off_offers_the_stored_measurands(hass):
    """Turning detection off drops into manual selection, pre-filled.

    The pre-fill is the stored list - what detection last accepted - so the
    user starts from the working set rather than from a single default.
    """
    entry = _entry(hass, [{"CP_1": _cp_settings()}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAX_CURRENT: 32,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: False,
            CONF_METER_INTERVAL: 60,
            CONF_IDLE_INTERVAL: 900,
            CONF_SKIP_SCHEMA_VALIDATION: False,
            CONF_FORCE_SMART_CHARGING: True,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "measurands"
    prefilled = {
        str(key) for key in result["data_schema"].schema if key.default() is True
    }
    assert prefilled == {"Power.Active.Import", "Voltage"}

    # Build the submission in schema order so the joined result is
    # deterministic, as it is when the real form drives the order.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            str(key): (str(key) in ("Current.Import", "Voltage"))
            for key in result["data_schema"].schema
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    stored = _stored(entry, "CP_1")
    assert stored[CONF_MONITORED_VARIABLES_AUTOCONFIG] is False
    assert stored[CONF_MONITORED_VARIABLES] == "Current.Import,Voltage"


async def test_selecting_no_measurands_is_rejected(hass):
    """An empty manual selection would silently blank the measurand list.

    That is fault #2033's failure mode arrived at by hand; the form has to
    push back rather than store an empty string.
    """
    entry = _entry(hass, [{"CP_1": _cp_settings()}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAX_CURRENT: 32,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: False,
            CONF_METER_INTERVAL: 60,
            CONF_IDLE_INTERVAL: 900,
            CONF_SKIP_SCHEMA_VALIDATION: False,
            CONF_FORCE_SMART_CHARGING: True,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=dict.fromkeys(_form_fields(result), False),
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "measurands"
    assert result["errors"] == {"base": "no_measurands_selected"}
    assert _stored(entry, "CP_1")[CONF_MONITORED_VARIABLES] == (
        "Power.Active.Import,Voltage"
    )


async def test_multiple_chargers_get_a_picker_and_only_the_picked_one_changes(hass):
    """Editing CP_2 must leave CP_1 byte-for-byte alone."""
    entry = _entry(
        hass,
        [
            {"CP_1": _cp_settings()},
            {"CP_2": _cp_settings(**{CONF_CPID: "second_cpid", CONF_MAX_CURRENT: 16})},
        ],
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"cp_id": "CP_2"}
    )
    assert result["step_id"] == "cp_settings"
    assert result["description_placeholders"]["cpid"] == "second_cpid"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAX_CURRENT: 25,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
            CONF_METER_INTERVAL: 60,
            CONF_IDLE_INTERVAL: 900,
            CONF_SKIP_SCHEMA_VALIDATION: False,
            CONF_FORCE_SMART_CHARGING: True,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert _stored(entry, "CP_2")[CONF_MAX_CURRENT] == 25
    assert _stored(entry, "CP_2")[CONF_CPID] == "second_cpid"
    assert _stored(entry, "CP_1") == _cp_settings()


async def test_updates_landing_between_steps_are_not_clobbered(hass):
    """A snapshot taken at the first form must not be written back later.

    While the user sits on the measurands form, a reconnecting charger's
    post_connect can update the entry - the connector count, the detected
    measurand list. Finalize has to overlay only the edited fields onto
    the entry as it is at write time, so those updates survive; a full
    snapshot taken at the first submit would erase them.
    """
    entry = _entry(hass, [{"CP_1": _cp_settings()}])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAX_CURRENT: 63,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: False,
            CONF_METER_INTERVAL: 60,
            CONF_IDLE_INTERVAL: 900,
            CONF_SKIP_SCHEMA_VALIDATION: False,
            CONF_FORCE_SMART_CHARGING: True,
        },
    )
    assert result["step_id"] == "measurands"

    # post_connect lands while the measurands form is open.
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_CPIDS: [{"CP_1": _cp_settings(**{CONF_NUM_CONNECTORS: 3})}],
        },
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            str(key): (str(key) == "Voltage") for key in result["data_schema"].schema
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    stored = _stored(entry, "CP_1")
    # The detector's update survives...
    assert stored[CONF_NUM_CONNECTORS] == 3
    # ...while the user's explicit edits still win for the edited fields.
    assert stored[CONF_MAX_CURRENT] == 63
    assert stored[CONF_MONITORED_VARIABLES] == "Voltage"
    assert stored[CONF_MONITORED_VARIABLES_AUTOCONFIG] is False


async def test_no_chargers_means_nothing_to_configure(hass):
    """A central system nothing has connected to yet aborts cleanly."""
    entry = _entry(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "no_charge_points"


async def test_the_form_defaults_are_the_stored_values(hass):
    """The form must open showing what is configured, not the global defaults.

    Otherwise every save silently resets any field the user did not
    re-enter - the options-flow version of the clobbering this campaign
    kept finding.
    """
    entry = _entry(
        hass,
        [
            {
                "CP_1": _cp_settings(
                    **{
                        CONF_MAX_CURRENT: 63,
                        CONF_SKIP_SCHEMA_VALIDATION: True,
                        CONF_FORCE_SMART_CHARGING: False,
                    }
                )
            }
        ],
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)

    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults[CONF_MAX_CURRENT] == 63
    assert defaults[CONF_SKIP_SCHEMA_VALIDATION] is True
    assert defaults[CONF_FORCE_SMART_CHARGING] is False
    assert defaults[CONF_MONITORED_VARIABLES_AUTOCONFIG] is True
