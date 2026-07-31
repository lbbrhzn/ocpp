"""A stored connector count of zero must not erase a charger's entities."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_CSID,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    DOMAIN as OCPP_DOMAIN,
)

from .charge_point_test import create_configuration, remove_configuration
from .const import MOCK_CONFIG_DATA, MOCK_CONFIG_CP_APPEND


@pytest.mark.timeout(90)
async def test_zero_num_connectors_still_creates_connector_entities(
    hass, socket_enabled
):
    """A charger whose config says 0 connectors must still get its controls.

    post_connect persists whatever get_number_of_connectors() computed, so a
    version whose OCPP 2.0.1 inventory handling yielded no connectors could
    store 0. The per-connector entity loops then ran over an empty range and
    the charger came up with no Charge Control - leaving the integration
    unable to start a charge, with nothing to indicate why. The stored count
    is clamped to at least one so this cannot happen however it got there.
    """
    cp_cfg = {**MOCK_CONFIG_CP_APPEND, CONF_NUM_CONNECTORS: 0}
    entry_data = {
        **MOCK_CONFIG_DATA,
        CONF_CSID: "test_csid_zero_conn",
        CONF_PORT: 9021,
        CONF_CPIDS: [{"CP_zero": cp_cfg}],
    }
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=entry_data,
        entry_id="test_cms_zero_conn",
        title="test_cms_zero_conn",
        version=2,
        minor_version=0,
    )

    await create_configuration(hass, config_entry)
    try:
        cpid = cp_cfg[CONF_CPID]
        assert (
            hass.states.get(f"switch.{cpid}_charge_control") is not None
        ), "a stored count of 0 must not suppress the charge control switch"
        # the other per-connector platforms are built from the same value
        assert hass.states.get(f"number.{cpid}_maximum_current") is not None
        assert hass.states.get(f"button.{cpid}_reset") is not None
    finally:
        await remove_configuration(hass, config_entry)
