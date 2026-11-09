"""A Faulted connector must keep showing its fault while the charger is connected."""

import asyncio
import contextlib
import copy
from datetime import UTC, datetime

import pytest
import websockets
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from ocpp.routing import on
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    ChargePointErrorCode,
    ChargePointStatus,
    UnlockStatus,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import (
    CONF_CPIDS,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    DOMAIN,
)

from .charge_point_test import create_configuration, remove_configuration
from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA
from .test_charge_point_v16 import ChargePoint, wait_ready


class FaultingCharger(ChargePoint):
    """Charger that charges on a chosen connector, then faults mid-charge."""

    def __init__(self, id, connection, no_connectors):
        """Track the unlocks that reach the charger."""
        super().__init__(id, connection, no_connectors=no_connectors)
        self.unlocked = []

    @on(Action.unlock_connector)
    def on_unlock_connector(self, connector_id, **kwargs):
        """Record the unlock."""
        self.unlocked.append(connector_id)
        return call_result.UnlockConnector(UnlockStatus.unlocked)

    async def send_status(self, connector_id, status, error_code):
        """Send one connector status."""
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=error_code,
                status=status,
                timestamp=datetime.now(tz=UTC).isoformat(),
            )
        )

    async def start_charging(self, connector_id):
        """Start a transaction and report 7 kW flowing."""
        await self.send_status(
            connector_id, ChargePointStatus.charging, ChargePointErrorCode.no_error
        )
        started = await self.call(
            call.StartTransaction(
                connector_id=connector_id,
                id_tag="test_cp",
                meter_start=1000,
                timestamp=datetime.now(tz=UTC).isoformat(),
            )
        )
        await self.call(
            call.MeterValues(
                connector_id=connector_id,
                transaction_id=started.transaction_id,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "5000",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                            },
                            {
                                "value": "7000",
                                "measurand": "Power.Active.Import",
                                "unit": "W",
                            },
                        ],
                    }
                ],
            )
        )


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    ("num_connectors", "port"), [(2, 9531), (1, 9532)], ids=["two", "one"]
)
async def test_fault_mid_charge_stays_visible_until_the_connection_is_lost(
    hass, socket_enabled, num_connectors, port
):
    """A real fault shows on the entities, and current stops being reported."""
    cp_id = "CP_faulted"
    cpid = MOCK_CONFIG_CP_APPEND["cpid"]
    connector_id = num_connectors
    prefix = f"{cpid}_connector_2_" if num_connectors == 2 else f"{cpid}_"

    data = copy.deepcopy(MOCK_CONFIG_DATA)
    data[CONF_PORT] = port
    data[CONF_CPIDS] = [
        {
            cp_id: {
                **copy.deepcopy(MOCK_CONFIG_CP_APPEND),
                CONF_NUM_CONNECTORS: num_connectors,
            }
        }
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        entry_id=f"cms_faulted_{num_connectors}",
        title="cms_faulted",
        version=2,
        minor_version=3,
    )
    central = await create_configuration(hass, entry)

    def state(entity: str) -> str:
        return hass.states.get(entity).state

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        charger = FaultingCharger("client", ws, num_connectors)
        task = asyncio.create_task(charger.start())
        try:
            await charger.send_boot_notification()
            await wait_ready(central.charge_points[cp_id])

            await charger.start_charging(connector_id)
            await hass.async_block_till_done()
            assert float(state(f"sensor.{prefix}power_active_import")) == 7.0

            await charger.send_status(
                connector_id,
                ChargePointStatus.faulted,
                ChargePointErrorCode.other_error,
            )
            await hass.async_block_till_done()

            assert state(f"sensor.{prefix}status_connector") == "Faulted"
            assert state(f"sensor.{prefix}error_code_connector") == "OtherError"
            # Nothing flows on a fault, and the charger may not say so again.
            assert float(state(f"sensor.{prefix}power_active_import")) == 0
            assert float(state(f"sensor.{prefix}energy_active_import_register")) == 5.0
            assert state(f"switch.{prefix}charge_control") != STATE_UNAVAILABLE
            if num_connectors == 2:
                assert state(f"switch.{prefix}connnector_availability") == STATE_ON

            button = f"button.{prefix}unlock"
            assert state(button) == STATE_UNKNOWN
            await hass.services.async_call(
                "button", "press", {"entity_id": button}, blocking=True
            )
            assert charger.unlocked == [connector_id]
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    server = central.charge_points[cp_id]
    async with asyncio.timeout(5):
        while server.status != STATE_UNAVAILABLE:
            await asyncio.sleep(0.05)
    assert central.get_available(cpid, connector_id) is False

    await remove_configuration(hass, entry)
