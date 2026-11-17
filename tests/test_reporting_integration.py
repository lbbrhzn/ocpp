"""Exercise reporting through HA services and the native OCPP 1.6 controller."""

import asyncio
from contextlib import suppress
import json

from homeassistant.helpers import entity_registry as er
from ocpp.routing import on
from ocpp.v16 import call
from ocpp.v16.enums import Action
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from custom_components.ocpp.const import (
    CONF_CPIDS,
    CONF_HOST,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    CONF_REPORTING_ENABLED,
    CONF_REPORTING_URL,
    DOMAIN,
)

from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_CS
from .test_charge_point_v16 import ChargePoint as VirtualChargePoint


class ObservedChargePoint(VirtualChargePoint):
    """Capture the transaction addressed by HA's actual remote-stop command."""

    last_stopped_transaction = None

    @on(Action.remote_stop_transaction)
    def on_remote_stop_transaction(self, transaction_id, **kwargs):
        """Respond as the existing virtual charger and record the wire ID."""
        self.last_stopped_transaction = transaction_id
        return super().on_remote_stop_transaction(**kwargs)


async def test_ha_charge_control_uses_local_ids_while_provider_gets_its_own(hass, socket_enabled):
    """HA automations retain authority while real session data reaches the provider."""
    reports = asyncio.Queue()

    async def provider(ws):
        async for raw in ws:
            frame = json.loads(raw)
            if frame[0] != 2:
                continue
            await reports.put(frame)
            response = {}
            if frame[2] == "BootNotification":
                response = {
                    "status": "Accepted", "interval": 300,
                    "currentTime": "2026-02-03T04:05:06Z",
                }
            elif frame[2] == "StartTransaction":
                response = {"transactionId": 84721, "idTagInfo": {"status": "Accepted"}}
            await ws.send(json.dumps([3, frame[1], response]))

    async def next_report(action):
        async with asyncio.timeout(5):
            while True:
                frame = await reports.get()
                if frame[2] == action:
                    return frame[3]

    async with serve(provider, "127.0.0.1", 0, subprotocols=["ocpp1.6"]) as backend:
        entry = MockConfigEntry(
            domain=DOMAIN, version=2, minor_version=3,
            data={
                **MOCK_CONFIG_CS,
                CONF_HOST: "127.0.0.1", CONF_PORT: 0,
                CONF_CPIDS: [{"reporting-charger": {
                    **MOCK_CONFIG_CP_APPEND, CONF_NUM_CONNECTORS: 1,
                    CONF_REPORTING_ENABLED: True,
                    CONF_REPORTING_URL: f"ws://127.0.0.1:{backend.sockets[0].getsockname()[1]}/ocpp",
                }}],
            },
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        central = hass.data[DOMAIN][entry.entry_id]
        port = central._server.sockets[0].getsockname()[1]
        try:
            async with connect(f"ws://127.0.0.1:{port}/reporting-charger", subprotocols=["ocpp1.6"]) as socket:
                charger = ObservedChargePoint("reporting-charger", socket)
                reader = asyncio.create_task(charger.start())
                try:
                    await charger.call(call.BootNotification(
                        charge_point_vendor="Example", charge_point_model="Virtual EVSE",
                    ))
                    async with asyncio.timeout(10):
                        while not central.charge_points["reporting-charger"].post_connect_success:
                            await asyncio.sleep(0.01)
                    await next_report("BootNotification")
                    await hass.services.async_call(
                        "switch", "turn_on", {"entity_id": "switch.test_cpid_charge_control"},
                        blocking=True,
                    )
                    async with asyncio.timeout(5):
                        while not charger.active_transactionId:
                            await asyncio.sleep(0.01)
                    local_id = charger.active_transactionId
                    assert local_id != 84721
                    started = await next_report("StartTransaction")
                    assert started["meterStart"] == 12345
                    assert started["connectorId"] == 1
                    assert started["idTag"] == "test_cp"

                    values = [{
                        "timestamp": "2026-02-03T04:05:06Z",
                        "sampledValue": [{
                            "value": "12.75", "unit": "kWh",
                            "measurand": "Energy.Active.Import.Register",
                        }],
                    }]
                    await charger.call(call.MeterValues(
                        connector_id=1, transaction_id=local_id, meter_value=values,
                    ))
                    delivered = await next_report("MeterValues")
                    assert delivered == {
                        "connectorId": 1, "transactionId": 84721, "meterValue": values,
                    }
                    await hass.services.async_call(
                        "switch", "turn_off", {"entity_id": "switch.test_cpid_charge_control"},
                        blocking=True,
                    )
                    assert charger.last_stopped_transaction == local_id
                    await charger.send_stop_transaction()
                    stopped = await next_report("StopTransaction")
                    assert stopped["transactionId"] == 84721
                    assert stopped["meterStop"] == 54321
                    registry = er.async_get(hass)
                    entity_id = registry.async_get_entity_id(
                        "sensor", DOMAIN, "ocpp.test_cpid.reporting_status",
                    )
                    assert hass.states.get(entity_id).state == "connected"
                finally:
                    if charger.task is not None:
                        await charger.task
                    reader.cancel()
                    with suppress(asyncio.CancelledError):
                        await reader
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
