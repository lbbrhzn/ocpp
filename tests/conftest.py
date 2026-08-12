"""Global fixtures for ocpp integration."""

import asyncio
import copy
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

from .lifecycle_asserts import assert_no_swallowed_lifecycle_errors

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations defined in the test dir."""
    yield


@pytest.fixture(autouse=True)
def fail_on_swallowed_lifecycle_errors(request, caplog):
    """Fail any test whose log carries a swallowed lifecycle failure.

    Home Assistant turns platform-setup exceptions, forward collisions
    and bad unloads into log lines while the calls report success, so a
    regression fails no assertion anywhere - this tripwire makes the
    nearest test fail instead. Tests that provoke these deliberately
    opt out with @pytest.mark.allow_lifecycle_errors.

    Only the synchronous signatures are enforced here: "Task exception
    was never retrieved" surfaces at GC/loop-drain time, so it can miss
    or cross test boundaries depending on environment - dedicated
    lifecycle tests assert it inside controlled windows instead.
    """
    yield
    if request.node.get_closest_marker("allow_lifecycle_errors"):
        return
    assert_no_swallowed_lifecycle_errors(caplog, include_async=False)


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
    future.set_result(websockets.asyncio.server.Server)
    # No StateMachine.get patch here: it used to return a synthetic State
    # for EVERY hass.states.get in the process, which poisoned core's
    # duplicate-entity checks on reload and shadowed the real state the
    # migration test seeds - hiding a genuine migration bug behind it.
    # The migration test provides its own state; nothing else needs one.
    with (
        patch("websockets.asyncio.server.serve", return_value=future),
        patch("websockets.asyncio.server.Server.close"),
        patch("websockets.asyncio.server.Server.wait_closed"),
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
    # deepcopy, not copy: a shallow copy shares the CONF_CPIDS list with
    # the module-level constant, so the append below permanently mutated
    # MOCK_CONFIG_DATA for the rest of the pytest session - every later
    # test asking for "the chargerless config" silently got a charger
    # (order-dependent failures that never reproduce in isolation).
    config_data = copy.deepcopy(MOCK_CONFIG_DATA)
    config_data[CONF_CPIDS].append(
        {request.param["cp_id"]: copy.deepcopy(MOCK_CONFIG_CP_APPEND)}
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
