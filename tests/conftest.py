"""Global fixtures for ocpp integration."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import patch
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest
import websockets

from custom_components.ocpp.api import CentralSystem
from custom_components.ocpp.const import CONF_CPIDS, CONF_PORT, DOMAIN as OCPP_DOMAIN
from tests.const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA
from .charge_point_test import (
    create_configuration,
    remove_configuration,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations defined in the test dir."""
    yield


# This fixture is used to prevent HomeAssistant from attempting to create and dismiss persistent
# notifications. These calls would fail without this fixture since the persistent_notification
# integration is never loaded during a test.
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with (
        patch("homeassistant.components.persistent_notification.async_create"),
        patch("homeassistant.components.persistent_notification.async_dismiss"),
        patch("custom_components.ocpp.chargepoint.ChargePoint.notify_ha"),
    ):
        yield


# This fixture, when used, will result in calls to websockets to be bypassed. To have the call
# return a value, we would add the `return_value=<VALUE_TO_RETURN>` parameter to the patch call.
# include patch for hass.states.get for use with migration to return cp_id
@pytest.fixture(name="bypass_get_data")
def bypass_get_data_fixture():
    """Skip calls to get data from API."""
    future = asyncio.Future()
    future.set_result(websockets.asyncio.server.Server)
    with (
        patch("websockets.asyncio.server.serve", return_value=future),
        patch("websockets.asyncio.server.Server.close"),
        patch("websockets.asyncio.server.Server.wait_closed"),
        patch("homeassistant.core.StateMachine.get", return_value="test_cp_id"),
    ):
        yield


# In this fixture, we are forcing calls to async_get_data to raise an Exception. This is useful
# for exception handling.
@pytest.fixture(name="error_on_get_data")
def error_get_data_fixture():
    """Simulate error when retrieving data from API."""
    # with patch(
    #    "custom_components.ocpp.ocppApiClient.async_get_data",
    #    side_effect=Exception,
    # ):
    yield


@pytest.fixture
async def setup_config_entry(hass, request) -> AsyncGenerator[CentralSystem, None]:
    """Setup/teardown mock config entry and central system."""
    # Create a mock entry so we don't have to go through config flow
    # Both version and minor need to match config flow so as not to trigger migration flow
    config_data = MOCK_CONFIG_DATA.copy()
    config_data[CONF_CPIDS].append(
        {request.param["cp_id"]: MOCK_CONFIG_CP_APPEND.copy()}
    )
    config_data[CONF_PORT] = request.param["port"]
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=config_data,
        entry_id=request.param["cms"],
        title=request.param["cms"],
        version=2,
        minor_version=0,
    )
    yield await create_configuration(hass, config_entry)
    # tear down
    await remove_configuration(hass, config_entry)
