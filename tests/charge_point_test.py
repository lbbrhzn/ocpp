"""Implement common functions for simulating charge points of any OCPP version."""

import asyncio

from homeassistant.core import HomeAssistant
from websockets import Subprotocol

from custom_components.ocpp import CentralSystem
from .const import CONF_PORT
import contextlib
from custom_components.ocpp.const import DOMAIN as OCPP_DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import SERVICE_PRESS
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID
from ocpp.charge_point import ChargePoint
from pytest_homeassistant_custom_component.common import MockConfigEntry
from typing import Any
from collections.abc import Callable, Awaitable
from websockets import connect
from websockets.asyncio.client import ClientConnection


async def set_switch(hass: HomeAssistant, cpid: str, key: str, on: bool):
    """Toggle a switch."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        service_data={ATTR_ENTITY_ID: f"{SWITCH_DOMAIN}.{cpid}_{key}"},
        blocking=True,
    )


async def set_number(hass: HomeAssistant, cpid: str, key: str, value: int):
    """Set a numeric slider."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        service_data={"value": value},
        blocking=True,
        target={ATTR_ENTITY_ID: f"{NUMBER_DOMAIN}.{cpid}_{key}"},
    )


set_switch.__test__ = False


async def press_button(hass: HomeAssistant, cpid: str, key: str):
    """Press a button."""
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: f"{BUTTON_DOMAIN}.{cpid}_{key}"},
        blocking=True,
    )


press_button.__test__ = False


async def create_configuration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> CentralSystem:
    """Create an integration."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    return hass.data[OCPP_DOMAIN][config_entry.entry_id]


create_configuration.__test__ = False


async def remove_configuration(hass: HomeAssistant, config_entry: MockConfigEntry):
    """Remove an integration."""
    if entry := hass.config_entries.async_get_entry(config_entry.entry_id):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.entry_id not in hass.data[OCPP_DOMAIN]


remove_configuration.__test__ = False


async def wait_ready(cp: ChargePoint):
    """Wait until charge point is connected and initialised."""
    while not cp.post_connect_success:
        await asyncio.sleep(0.1)


def _check_complete(
    test_routine: Callable[[ChargePoint], Awaitable],
) -> Callable[[ChargePoint, list[bool]], Awaitable]:
    async def extended_routine(cp: ChargePoint, completed: list[bool]):
        await test_routine(cp)
        completed.append(True)

    return extended_routine


async def run_charge_point_test(
    config_entry: MockConfigEntry,
    identity: str,
    subprotocols: list[str] | None,
    charge_point: Callable[[ClientConnection], ChargePoint],
    parallel_tests: list[Callable[[ChargePoint], Awaitable]],
) -> Any:
    """Connect web socket client to the CSMS and run a number of tests in parallel."""
    completed: list[list[bool]] = [[] for _ in parallel_tests]
    async with connect(
        f"ws://127.0.0.1:{config_entry.data[CONF_PORT]}/{identity}",
        subprotocols=[Subprotocol(s) for s in subprotocols]
        if subprotocols is not None
        else None,
    ) as ws:
        cp = charge_point(ws)
        with contextlib.suppress(asyncio.TimeoutError):
            test_results = [
                _check_complete(parallel_tests[i])(cp, completed[i])
                for i in range(len(parallel_tests))
            ]
            await asyncio.wait_for(
                asyncio.gather(*([cp.start()] + test_results)),
                timeout=30,
            )
        await ws.close()
    for test_completed in completed:
        assert test_completed == [True]


run_charge_point_test.__test__ = False
