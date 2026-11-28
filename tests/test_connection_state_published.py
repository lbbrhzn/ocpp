"""A change in the charger connection must reach Home Assistant on its own."""

import asyncio
import contextlib

import pytest
import websockets
from homeassistant.const import STATE_UNAVAILABLE

from .const import MOCK_CONFIG_CP_APPEND
from .test_charge_point_v16 import ChargePoint, wait_ready

CP_ID = "CP_published"
PORT = 9533


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": PORT, "cp_id": CP_ID, "cms": "cms_published"}],
    indirect=True,
)
async def test_lost_connection_and_silent_reconnect_reach_entities(
    hass, socket_enabled, setup_config_entry
):
    """Entities follow a lost connection and a reconnect nothing else announces."""
    central = setup_config_entry
    cpid = MOCK_CONFIG_CP_APPEND["cpid"]
    entities = [f"sensor.{cpid}_status", f"switch.{cpid}_availability"]

    def states() -> list[str]:
        return [hass.states.get(entity).state for entity in entities]

    async def until(predicate) -> None:
        async with asyncio.timeout(5):
            while not predicate(states()):
                await asyncio.sleep(0.05)

    async with websockets.connect(
        f"ws://127.0.0.1:{PORT}/{CP_ID}", subprotocols=["ocpp1.6"]
    ) as ws:
        charger = ChargePoint(f"{CP_ID}_client", ws)
        task = asyncio.create_task(charger.start())
        try:
            await charger.send_boot_notification()
            await wait_ready(central.charge_points[CP_ID])
            await hass.async_block_till_done()
            assert STATE_UNAVAILABLE not in states()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    await until(lambda now: set(now) == {STATE_UNAVAILABLE})

    async with websockets.connect(
        f"ws://127.0.0.1:{PORT}/{CP_ID}", subprotocols=["ocpp1.6"]
    ) as ws:
        charger = ChargePoint(f"{CP_ID}_client", ws)
        task = asyncio.create_task(charger.start())
        try:
            # The charger sends nothing, so only the reconnect can restore them.
            await until(lambda now: STATE_UNAVAILABLE not in now)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
