"""Implement a test by a simulating an OCPP 2.0.1 chargepoint."""

import asyncio
from datetime import datetime, UTC

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import CONF_CPIDS, CONF_CPID
from custom_components.ocpp.const import (
    DOMAIN as OCPP_DOMAIN,
)
from custom_components.ocpp.enums import (
    HAChargerServices as csvcs,
)
import ocpp
from ocpp.routing import on
import ocpp.exceptions
from ocpp.v201 import ChargePoint as cpclass, call, call_result
from ocpp.v201.enums import (
    Action,
    BootReasonEnumType,
    ChangeAvailabilityStatusEnumType,
    ChargingStateEnumType,
    ConnectorStatusEnumType,
    DataEnumType,
    GenericDeviceModelStatusEnumType,
    MutabilityEnumType,
    OperationalStatusEnumType,
    RegistrationStatusEnumType,
    ReportBaseEnumType,
    SetVariableStatusEnumType,
    TransactionEventEnumType,
    TriggerMessageStatusEnumType,
    TriggerReasonEnumType,
    UpdateFirmwareStatusEnumType,
)
from ocpp.v201.datatypes import (
    ComponentType,
    EVSEType,
    VariableType,
    VariableAttributeType,
    VariableCharacteristicsType,
    ReportDataType,
    SetVariableResultType,
)

from .const import MOCK_CONFIG_DATA, MOCK_CONFIG_CP_APPEND

from .charge_point_test import (
    create_configuration,
    run_charge_point_test,
    remove_configuration,
    wait_ready,
)


class MultiConnectorChargePoint(cpclass):
    """Minimal OCPP 2.0.1 client som rapporterar 2 EVSE (1: två connectors, 2: en)."""

    def __init__(self, cp_id, ws):
        """Initialize."""
        super().__init__(cp_id, ws)
        self.inventory_done = asyncio.Event()
        self.last_start_evse_id = None

    @on(Action.get_base_report)
    async def on_get_base_report(self, request_id: int, report_base: str, **kwargs):
        """Get base report."""
        assert report_base in (ReportBaseEnumType.full_inventory, "FullInventory")
        asyncio.create_task(self._send_full_inventory(request_id))  # noqa: RUF006
        return call_result.GetBaseReport(
            GenericDeviceModelStatusEnumType.accepted.value
        )

    @on(Action.trigger_message)
    async def on_trigger_message(self, requested_message: str, **kwargs):
        """Handle trigger message."""
        self.last_trigger = (requested_message, kwargs.get("evse"))
        return call_result.TriggerMessage(TriggerMessageStatusEnumType.accepted.value)

    @on(Action.update_firmware)
    async def on_update_firmware(self, request_id: int, firmware: dict, **kwargs):
        """Handle update firmware."""
        return call_result.UpdateFirmware(UpdateFirmwareStatusEnumType.rejected.value)

    @on(Action.set_variables)
    async def on_set_variables(self, set_variable_data: list[dict], **kwargs):
        """Handle SetVariables."""
        results: list[SetVariableResultType] = []
        for item in set_variable_data:
            comp = item.get("component", {})
            var = item.get("variable", {})
            results.append(
                SetVariableResultType(
                    SetVariableStatusEnumType.accepted,
                    ComponentType(
                        comp.get("name"),
                        instance=comp.get("instance"),
                        evse=comp.get("evse"),
                    ),
                    VariableType(
                        var.get("name"),
                        instance=var.get("instance"),
                    ),
                )
            )
        return call_result.SetVariables(results)

    async def _send_full_inventory(self, request_id: int):
        ts = datetime.now(UTC).isoformat()
        report_data = [
            # EVSE 1
            ReportDataType(
                ComponentType("EVSE", evse=EVSEType(1)),
                VariableType("Status"),
                [VariableAttributeType(value="OK")],
            ),
            # EVSE 2
            ReportDataType(
                ComponentType("EVSE", evse=EVSEType(2)),
                VariableType("Status"),
                [VariableAttributeType(value="OK")],
            ),
            # Connector(1,1)
            ReportDataType(
                ComponentType("Connector", evse=EVSEType(1, connector_id=1)),
                VariableType("Enabled"),
                [
                    VariableAttributeType(
                        value="true", mutability=MutabilityEnumType.read_only
                    )
                ],
            ),
            # Connector(1,2)
            ReportDataType(
                ComponentType("Connector", evse=EVSEType(1, connector_id=2)),
                VariableType("Enabled"),
                [
                    VariableAttributeType(
                        value="true", mutability=MutabilityEnumType.read_only
                    )
                ],
            ),
            # Connector(2,1)
            ReportDataType(
                ComponentType("Connector", evse=EVSEType(2, connector_id=1)),
                VariableType("Enabled"),
                [
                    VariableAttributeType(
                        value="true", mutability=MutabilityEnumType.read_only
                    )
                ],
            ),
            # SmartChargingCtrlr.Available
            ReportDataType(
                ComponentType("SmartChargingCtrlr"),
                VariableType("Available"),
                [
                    VariableAttributeType(
                        value="true", mutability=MutabilityEnumType.read_only
                    )
                ],
            ),
            # SampledDataCtrlr.TxUpdatedMeasurands
            ReportDataType(
                ComponentType("SampledDataCtrlr"),
                VariableType("TxUpdatedMeasurands"),
                [VariableAttributeType(value="", persistent=True)],
                VariableCharacteristicsType(
                    DataEnumType.member_list,
                    False,
                    values_list="Energy.Active.Import.Register,Current.Import,Voltage",
                ),
            ),
        ]

        req = call.NotifyReport(
            request_id=request_id,
            generated_at=ts,
            seq_no=0,
            report_data=report_data,
            tbc=False,
        )
        await self.call(req)
        self.inventory_done.set()

    @on(Action.change_availability)
    async def on_change_availability(self, operational_status: str, **kwargs):
        """Handle change availability."""
        self.operative = operational_status == OperationalStatusEnumType.operative.value
        return call_result.ChangeAvailability(
            ChangeAvailabilityStatusEnumType.accepted.value
        )

    @on(Action.request_start_transaction)
    async def on_request_start_transaction(
        self, evse_id: int, id_token: dict, **kwargs
    ):
        """Handle request for start transaction."""
        self.last_start_evse_id = evse_id
        return call_result.RequestStartTransaction(status="Accepted")

    @on(Action.request_stop_transaction)
    async def on_request_stop_transaction(self, transaction_id: str, **kwargs):
        """Handle request for stop transaction."""
        return call_result.RequestStopTransaction(status="Accepted")

    async def send_status(
        self, evse_id: int, connector_id: int, status: ConnectorStatusEnumType
    ):
        """Send status."""
        await self.call(
            call.StatusNotification(
                timestamp=datetime.now(UTC).isoformat(),
                connector_status=status.value,
                evse_id=evse_id,
                connector_id=connector_id,
            )
        )

    async def send_tx_started_eair_wh(
        self, evse_id: int, connector_id: int, tx_id: str, eair_wh: int
    ):
        """Send EAIR on transaction started."""
        await self.call(
            call.TransactionEvent(
                event_type=TransactionEventEnumType.started,
                timestamp=datetime.now(UTC).isoformat(),
                trigger_reason=TriggerReasonEnumType.authorized,
                seq_no=0,
                transaction_info={
                    "transaction_id": tx_id,
                    "charging_state": ChargingStateEnumType.charging,
                },
                evse={"id": evse_id, "connector_id": connector_id},
                meter_value=[
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "sampled_value": [
                            {
                                "measurand": "Energy.Active.Import.Register",
                                "value": eair_wh,
                                "unit_of_measure": {"unit": "Wh"},
                            }
                        ],
                    }
                ],
            )
        )

    async def send_tx_updated_eair_wh(
        self, evse_id: int, connector_id: int, tx_id: str, eair_wh: int
    ):
        """Send EAIR on transaction updated."""
        await self.call(
            call.TransactionEvent(
                event_type=TransactionEventEnumType.updated,
                timestamp=datetime.now(UTC).isoformat(),
                trigger_reason=TriggerReasonEnumType.meter_value_periodic,
                seq_no=1,
                transaction_info={
                    "transaction_id": tx_id,
                    "charging_state": ChargingStateEnumType.charging,
                },
                evse={"id": evse_id, "connector_id": connector_id},
                meter_value=[
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "sampled_value": [
                            {
                                "measurand": "Energy.Active.Import.Register",
                                "value": eair_wh,
                                "unit_of_measure": {"unit": "Wh"},
                            }
                        ],
                    }
                ],
            )
        )


@pytest.mark.timeout(150)
async def test_v201_multi_connectors_per_evse(hass, socket_enabled):
    """Test multi connector per EVSE functionality."""
    cp_id = "CP_v201_multi"

    config_data = MOCK_CONFIG_DATA.copy()
    config_data[CONF_CPIDS].append({cp_id: MOCK_CONFIG_CP_APPEND.copy()})
    config_data[CONF_CPIDS][-1][cp_id][CONF_CPID] = "test_v201_cpid"

    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=config_data,
        entry_id="test_v201_multi",
        title="test_v201_multi",
        version=2,
        minor_version=0,
    )

    cs = await create_configuration(hass, config_entry)
    ocpp.messages.ASYNC_VALIDATION = False

    async def _scenario(hass, cs, cp: MultiConnectorChargePoint):
        boot = await cp.call(
            call.BootNotification(
                {
                    "serial_number": "SERIAL",
                    "model": "MODEL",
                    "vendor_name": "VENDOR",
                    "firmware_version": "VERSION",
                },
                BootReasonEnumType.power_up.value,
            )
        )
        assert boot.status == RegistrationStatusEnumType.accepted.value

        await wait_ready(cs.charge_points["CP_v201_multi"])

        await asyncio.wait_for(cp.inventory_done.wait(), timeout=5)

        cp_srv = cs.charge_points["CP_v201_multi"]
        for _ in range(50):
            if getattr(cp_srv, "num_connectors", 0) == 3:
                break
            await asyncio.sleep(0.1)
        assert cp_srv.num_connectors == 3

        cpid = cp_srv.settings.cpid

        await cp.send_status(1, 1, ConnectorStatusEnumType.available)
        await cp.send_status(1, 2, ConnectorStatusEnumType.occupied)
        await cp.send_status(2, 1, ConnectorStatusEnumType.unavailable)
        await asyncio.sleep(0.05)

        assert cs.get_metric(cpid, "Status.Connector", connector_id=1) == "Available"
        assert cs.get_metric(cpid, "Status.Connector", connector_id=2) == "Occupied"
        assert cs.get_metric(cpid, "Status.Connector", connector_id=3) == "Unavailable"

        await cp.send_tx_started_eair_wh(1, 2, "TX-1", 10_000)
        await cp.send_tx_updated_eair_wh(1, 2, "TX-1", 10_500)
        await asyncio.sleep(0.05)

        assert cs.get_metric(
            cpid, "Energy.Active.Import.Register", connector_id=2
        ) == pytest.approx(10.5)
        assert cs.get_metric(cpid, "Energy.Session", connector_id=2) == pytest.approx(
            0.5
        )
        assert (
            cs.get_unit(cpid, "Energy.Active.Import.Register", connector_id=2) == "kWh"
        )
        assert cs.get_unit(cpid, "Energy.Session", connector_id=2) == "kWh"

        ok = await cs.set_charger_state(
            cpid, csvcs.service_charge_start.name, True, connector_id=3
        )
        assert ok is True
        await asyncio.sleep(0.05)
        assert cp.last_start_evse_id == 2

    await run_charge_point_test(
        config_entry,
        "CP_v201_multi",
        ["ocpp2.0.1"],
        lambda ws: MultiConnectorChargePoint("CP_v201_multi_client", ws),
        [lambda cp: _scenario(hass, cs, cp)],
    )

    await remove_configuration(hass, config_entry)
