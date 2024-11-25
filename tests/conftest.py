"""Global fixtures for ocpp integration."""

import asyncio
from unittest.mock import patch

import pytest
import websockets

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
@pytest.fixture(name="bypass_get_data")
def bypass_get_data_fixture():
    """Skip calls to get data from API."""
    future = asyncio.Future()
    future.set_result(websockets.WebSocketServer)
    with (
        patch("websockets.server.serve", return_value=future),
        patch("websockets.server.WebSocketServer.close"),
        patch("websockets.server.WebSocketServer.wait_closed"),
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
