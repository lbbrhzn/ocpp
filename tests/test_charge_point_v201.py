"""Implement a test by a simulating an OCPP 2.0.1 chargepoint."""

import asyncio
from datetime import datetime, timedelta, UTC

from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.exceptions import HomeAssistantError
from ocpp.v16.enums import Measurand

from custom_components.ocpp import CentralSystem
from custom_components.ocpp.enums import (
    HAChargerDetails as cdet,
    HAChargerServices as csvcs,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    Profiles,
)
from .charge_point_test import (
    set_switch,
    create_configuration,
    run_charge_point_test,
    remove_configuration,
    wait_ready,
)
from .const import MOCK_CONFIG_DATA
from custom_components.ocpp.const import (
    DEFAULT_METER_INTERVAL,
    DOMAIN as OCPP_DOMAIN,
    CONF_CPID,
    CONF_MONITORED_VARIABLES,
    MEASURANDS,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ocpp.routing import on
import ocpp.exceptions
from ocpp.v201 import ChargePoint as cpclass, call, call_result
from ocpp.v201.datatypes import (
    ComponentType,
    EVSEType,
    GetVariableResultType,
    SetVariableResultType,
    VariableType,
    VariableAttributeType,
    VariableCharacteristicsType,
    ReportDataType,
)
from ocpp.v201.enums import (
    Action,
    BootReasonType,
    ChangeAvailabilityStatusType,
    ChargingStateType,
    ConnectorStatusType,
    DataType,
    GenericDeviceModelStatusType,
    GetVariableStatusType,
    IdTokenType,
    MutabilityType,
    OperationalStatusType,
    PhaseType,
    ReadingContextType,
    RegistrationStatusType,
    ReportBaseType,
    RequestStartStopStatusType,
    SetVariableStatusType,
    ReasonType,
    TransactionEventType,
    TriggerMessageStatusType,
    TriggerReasonType,
    UpdateFirmwareStatusType,
)
from ocpp.v16.enums import ChargePointStatus as ChargePointStatusv16


supported_measurands = [
    measurand
    for measurand in MEASURANDS
    if (measurand != Measurand.rpm.value) and (measurand != Measurand.temperature.value)
]


class ChargePoint(cpclass):
    """Representation of real client Charge Point."""

    remote_starts: list[call.RequestStartTransaction] = []
    remote_stops: list[str] = []
    task: asyncio.Task | None = None
    remote_start_tx_id: str = "remotestart"
    operative: bool | None = None
    tx_updated_interval: int | None = None
    tx_updated_measurands: list[str] | None = None
    tx_start_time: datetime | None = None
    component_instance_used: str | None = None
    variable_instance_used: str | None = None

    @on(Action.GetBaseReport)
    def _on_base_report(self, request_id: int, report_base: str, **kwargs):
        assert report_base == ReportBaseType.full_inventory.value
        self.task = asyncio.create_task(self._send_full_inventory(request_id))
        return call_result.GetBaseReport(GenericDeviceModelStatusType.accepted.value)

    @on(Action.RequestStartTransaction)
    def _on_remote_start(
        self, id_token: dict, remote_start_id: int, **kwargs
    ) -> call_result.RequestStartTransaction:
        self.remote_starts.append(
            call.RequestStartTransaction(id_token, remote_start_id, *kwargs)
        )
        self.task = asyncio.create_task(
            self._start_transaction_remote_start(id_token, remote_start_id)
        )
        return call_result.RequestStartTransaction(
            RequestStartStopStatusType.accepted.value
        )

    @on(Action.RequestStopTransaction)
    def _on_remote_stop(self, transaction_id: str, **kwargs):
        assert transaction_id == self.remote_start_tx_id
        self.remote_stops.append(transaction_id)
        return call_result.RequestStopTransaction(
            RequestStartStopStatusType.accepted.value
        )

    @on(Action.SetVariables)
    def _on_set_variables(self, set_variable_data: list[dict], **kwargs):
        result: list[SetVariableResultType] = []
        for input in set_variable_data:
            if (input["component"] == {"name": "SampledDataCtrlr"}) and (
                input["variable"] == {"name": "TxUpdatedInterval"}
            ):
                self.tx_updated_interval = int(input["attribute_value"])
            if (input["component"] == {"name": "SampledDataCtrlr"}) and (
                input["variable"] == {"name": "TxUpdatedMeasurands"}
            ):
                self.tx_updated_measurands = input["attribute_value"].split(",")

            attr_result: SetVariableStatusType
            if input["variable"] == {"name": "RebootRequired"}:
                attr_result = SetVariableStatusType.reboot_required
            elif input["variable"] == {"name": "BadVariable"}:
                attr_result = SetVariableStatusType.unknown_variable
            elif input["variable"] == {"name": "VeryBadVariable"}:
                raise ocpp.exceptions.InternalError()
            else:
                attr_result = SetVariableStatusType.accepted
                self.component_instance_used = input["component"].get("instance", None)
                self.variable_instance_used = input["variable"].get("instance", None)

            result.append(
                SetVariableResultType(
                    attr_result,
                    ComponentType(input["component"]["name"]),
                    VariableType(input["variable"]["name"]),
                )
            )
        return call_result.SetVariables(result)

    @on(Action.GetVariables)
    def _on_get_variables(self, get_variable_data: list[dict], **kwargs):
        result: list[GetVariableResultType] = []
        for input in get_variable_data:
            value: str | None = None
            if (input["component"] == {"name": "SampledDataCtrlr"}) and (
                input["variable"] == {"name": "TxUpdatedInterval"}
            ):
                value = str(self.tx_updated_interval)
            elif input["variable"]["name"] == "TestInstance":
                value = (
                    input["component"]["instance"] + "," + input["variable"]["instance"]
                )
            elif input["variable"] == {"name": "VeryBadVariable"}:
                raise ocpp.exceptions.InternalError()
            result.append(
                GetVariableResultType(
                    GetVariableStatusType.accepted
                    if value is not None
                    else GetVariableStatusType.unknown_variable,
                    ComponentType(input["component"]["name"]),
                    VariableType(input["variable"]["name"]),
                    attribute_value=value,
                )
            )
        return call_result.GetVariables(result)

    @on(Action.ChangeAvailability)
    def _on_change_availability(self, operational_status: str, **kwargs):
        if operational_status == OperationalStatusType.operative.value:
            self.operative = True
        elif operational_status == OperationalStatusType.inoperative.value:
            self.operative = False
        else:
            assert False
        return call_result.ChangeAvailability(
            ChangeAvailabilityStatusType.accepted.value
        )

    async def _start_transaction_remote_start(
        self, id_token: dict, remote_start_id: int
    ):
        self.tx_start_time = datetime.now(tz=UTC)
        request = call.TransactionEvent(
            TransactionEventType.started.value,
            self.tx_start_time.isoformat(),
            TriggerReasonType.remote_start.value,
            0,
            transaction_info={
                "transaction_id": self.remote_start_tx_id,
                "remote_start_id": remote_start_id,
            },
            id_token=id_token,
        )
        await self.call(request)

    async def _send_full_inventory(self, request_id: int):
        # Cannot send all at once because of a bug in python ocpp module
        await self.call(
            call.NotifyReport(
                request_id,
                datetime.now(tz=UTC).isoformat(),
                0,
                [
                    ReportDataType(
                        ComponentType("SmartChargingCtrlr"),
                        VariableType("Available"),
                        [
                            VariableAttributeType(
                                value="true", mutability=MutabilityType.read_only
                            )
                        ],
                    )
                ],
                tbc=True,
            )
        )
        await self.call(
            call.NotifyReport(
                request_id,
                datetime.now(tz=UTC).isoformat(),
                1,
                [
                    ReportDataType(
                        ComponentType("ReservationCtrlr"),
                        VariableType("Available"),
                        [
                            VariableAttributeType(
                                value="true", mutability=MutabilityType.read_only
                            )
                        ],
                    ),
                ],
                tbc=True,
            )
        )
        await self.call(
            call.NotifyReport(
                request_id,
                datetime.now(tz=UTC).isoformat(),
                2,
                [
                    ReportDataType(
                        ComponentType("LocalAuthListCtrlr"),
                        VariableType("Available"),
                        [
                            VariableAttributeType(
                                value="true", mutability=MutabilityType.read_only
                            )
                        ],
                    ),
                ],
                tbc=True,
            )
        )
        await self.call(
            call.NotifyReport(
                request_id,
                datetime.now(tz=UTC).isoformat(),
                3,
                [
                    ReportDataType(
                        ComponentType("EVSE", evse=EVSEType(1)),
                        VariableType("Available"),
                        [
                            VariableAttributeType(
                                value="true", mutability=MutabilityType.read_only
                            )
                        ],
                    ),
                ],
                tbc=True,
            )
        )
        await self.call(
            call.NotifyReport(
                request_id,
                datetime.now(tz=UTC).isoformat(),
                4,
                [
                    ReportDataType(
                        ComponentType("Connector", evse=EVSEType(1, connector_id=1)),
                        VariableType("Available"),
                        [
                            VariableAttributeType(
                                value="true", mutability=MutabilityType.read_only
                            )
                        ],
                    ),
                ],
                tbc=True,
            )
        )
        await self.call(
            call.NotifyReport(
                request_id,
                datetime.now(tz=UTC).isoformat(),
                5,
                [
                    ReportDataType(
                        ComponentType("SampledDataCtrlr"),
                        VariableType("TxUpdatedMeasurands"),
                        [VariableAttributeType(value="", persistent=True)],
                        VariableCharacteristicsType(
                            DataType.member_list,
                            False,
                            values_list=",".join(supported_measurands),
                        ),
                    ),
                ],
            )
        )


class ChargePointAllFeatures(ChargePoint):
    """A charge point which also supports UpdateFirmware and TriggerMessage."""

    @on(Action.UpdateFirmware)
    def _on_update_firmware(self, request_id: int, firmware: dict, **kwargs):
        return call_result.UpdateFirmware(UpdateFirmwareStatusType.rejected.value)

    @on(Action.TriggerMessage)
    def _on_trigger_message(self, requested_message: str, **kwargs):
        return call_result.TriggerMessage(TriggerMessageStatusType.rejected.value)


async def _test_transaction(hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint):
    cpid: str = MOCK_CONFIG_DATA[CONF_CPID]

    await set_switch(hass, cs, "charge_control", True)
    assert len(cp.remote_starts) == 1
    assert cp.remote_starts[0].id_token == {
        "id_token": cs.charge_points[cpid]._remote_id_tag,
        "type": IdTokenType.central.value,
    }
    while cs.get_metric(cpid, csess.transaction_id.value) is None:
        await asyncio.sleep(0.1)
    assert cs.get_metric(cpid, csess.transaction_id.value) == cp.remote_start_tx_id

    tx_start_time = cp.tx_start_time
    await cp.call(
        call.StatusNotification(
            tx_start_time.isoformat(), ConnectorStatusType.occupied, 1, 1
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.preparing
    )

    await cp.call(
        call.TransactionEvent(
            TransactionEventType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonType.cable_plugged_in.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
            },
        )
    )
    await cp.call(
        call.TransactionEvent(
            TransactionEventType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonType.charging_state_changed.value,
            2,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.charging.value,
            },
            meter_value=[
                {
                    "timestamp": tx_start_time.isoformat(),
                    "sampled_value": [
                        {
                            "value": 0,
                            "measurand": Measurand.current_export.value,
                            "phase": PhaseType.l1.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.current_export.value,
                            "phase": PhaseType.l2.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.current_export.value,
                            "phase": PhaseType.l3.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 1.1,
                            "measurand": Measurand.current_import.value,
                            "phase": PhaseType.l1.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 2.2,
                            "measurand": Measurand.current_import.value,
                            "phase": PhaseType.l2.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 3.3,
                            "measurand": Measurand.current_import.value,
                            "phase": PhaseType.l3.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 12.1,
                            "measurand": Measurand.current_offered.value,
                            "phase": PhaseType.l1.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 12.2,
                            "measurand": Measurand.current_offered.value,
                            "phase": PhaseType.l2.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 12.3,
                            "measurand": Measurand.current_offered.value,
                            "phase": PhaseType.l3.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.energy_active_export_register.value,
                            "unit_of_measure": {"unit": "Wh"},
                        },
                        {
                            "value": 100,
                            "measurand": Measurand.energy_active_import_register.value,
                            "unit_of_measure": {"unit": "Wh"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.energy_reactive_export_register.value,
                            "unit_of_measure": {"unit": "Wh"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.energy_reactive_import_register.value,
                            "unit_of_measure": {"unit": "Wh"},
                        },
                        {
                            "value": 50,
                            "measurand": Measurand.frequency.value,
                            "unit_of_measure": {"unit": "Hz"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.power_active_export.value,
                            "unit_of_measure": {"unit": "W"},
                        },
                        {
                            "value": 1518,
                            "measurand": Measurand.power_active_import.value,
                            "unit_of_measure": {"unit": "W"},
                        },
                        {
                            "value": 8418,
                            "measurand": Measurand.power_offered.value,
                            "unit_of_measure": {"unit": "W"},
                        },
                        {
                            "value": 1,
                            "measurand": Measurand.power_factor.value,
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.power_reactive_export.value,
                            "unit_of_measure": {"unit": "W"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.power_reactive_import.value,
                            "unit_of_measure": {"unit": "W"},
                        },
                        {
                            "value": 69,
                            "measurand": Measurand.soc.value,
                            "unit_of_measure": {"unit": "percent"},
                        },
                        {
                            "value": 229.9,
                            "measurand": Measurand.voltage.value,
                            "phase": PhaseType.l1_n.value,
                            "unit_of_measure": {"unit": "V"},
                        },
                        {
                            "value": 230,
                            "measurand": Measurand.voltage.value,
                            "phase": PhaseType.l2_n.value,
                            "unit_of_measure": {"unit": "V"},
                        },
                        {
                            "value": 230.4,
                            "measurand": Measurand.voltage.value,
                            "phase": PhaseType.l3_n.value,
                            "unit_of_measure": {"unit": "V"},
                        },
                    ],
                }
            ],
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.charging
    )
    assert cs.get_metric(cpid, Measurand.current_export.value) == 0
    assert abs(cs.get_metric(cpid, Measurand.current_import.value) - 6.6) < 1e-6
    assert abs(cs.get_metric(cpid, Measurand.current_offered.value) - 36.6) < 1e-6
    assert cs.get_metric(cpid, Measurand.energy_active_export_register.value) == 0
    assert cs.get_metric(cpid, Measurand.energy_active_import_register.value) == 0.1
    assert cs.get_metric(cpid, Measurand.energy_reactive_export_register.value) == 0
    assert cs.get_metric(cpid, Measurand.energy_reactive_import_register.value) == 0
    assert cs.get_metric(cpid, Measurand.frequency.value) == 50
    assert cs.get_metric(cpid, Measurand.power_active_export.value) == 0
    assert abs(cs.get_metric(cpid, Measurand.power_active_import.value) - 1.518) < 1e-6
    assert abs(cs.get_metric(cpid, Measurand.power_offered.value) - 8.418) < 1e-6
    assert cs.get_metric(cpid, Measurand.power_reactive_export.value) == 0
    assert cs.get_metric(cpid, Measurand.power_reactive_import.value) == 0
    assert cs.get_metric(cpid, Measurand.soc.value) == 69
    assert abs(cs.get_metric(cpid, Measurand.voltage.value) - 230.1) < 1e-6
    assert cs.get_metric(cpid, csess.session_energy) == 0
    assert cs.get_metric(cpid, csess.session_time) == 0

    await cp.call(
        call.TransactionEvent(
            TransactionEventType.updated.value,
            (tx_start_time + timedelta(seconds=60)).isoformat(),
            TriggerReasonType.meter_value_periodic.value,
            3,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.charging.value,
            },
            meter_value=[
                {
                    "timestamp": (tx_start_time + timedelta(seconds=60)).isoformat(),
                    "sampled_value": [
                        {
                            "value": 256,
                            "measurand": Measurand.energy_active_import_register.value,
                            "unit_of_measure": {"unit": "Wh"},
                        },
                    ],
                }
            ],
        )
    )
    assert cs.get_metric(cpid, csess.session_energy) == 0.156
    assert cs.get_metric(cpid, csess.session_time) == 1

    await set_switch(hass, cs, "charge_control", False)
    assert len(cp.remote_stops) == 1

    await cp.call(
        call.TransactionEvent(
            TransactionEventType.ended.value,
            (tx_start_time + timedelta(seconds=120)).isoformat(),
            TriggerReasonType.remote_stop.value,
            4,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.ev_connected.value,
                "stopped_reason": ReasonType.remote.value,
            },
            meter_value=[
                {
                    "timestamp": (tx_start_time + timedelta(seconds=120)).isoformat(),
                    "sampled_value": [
                        {
                            "value": 333,
                            "context": ReadingContextType.transaction_end,
                            "measurand": Measurand.energy_active_import_register.value,
                            "unit_of_measure": {"unit": "Wh"},
                        },
                    ],
                }
            ],
        )
    )
    assert cs.get_metric(cpid, Measurand.current_import.value) == 0
    assert cs.get_metric(cpid, Measurand.current_offered.value) == 0
    assert cs.get_metric(cpid, Measurand.energy_active_import_register.value) == 0.333
    assert cs.get_metric(cpid, Measurand.frequency.value) == 0
    assert cs.get_metric(cpid, Measurand.power_active_import.value) == 0
    assert cs.get_metric(cpid, Measurand.power_offered.value) == 0
    assert cs.get_metric(cpid, Measurand.power_reactive_import.value) == 0
    assert cs.get_metric(cpid, Measurand.soc.value) == 0
    assert cs.get_metric(cpid, Measurand.voltage.value) == 0
    assert cs.get_metric(cpid, csess.session_energy) == 0.233
    assert cs.get_metric(cpid, csess.session_time) == 2


async def _set_variable(
    hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint, key: str, value: str
) -> tuple[ServiceResponse, HomeAssistantError]:
    response: ServiceResponse | None = None
    error: HomeAssistantError | None = None
    try:
        response = await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_configure_v201,
            service_data={"ocpp_key": key, "value": value},
            blocking=True,
            return_response=True,
        )
    except HomeAssistantError as e:
        error = e
    return response, error


async def _get_variable(
    hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint, key: str
) -> tuple[ServiceResponse, HomeAssistantError]:
    response: ServiceResponse | None = None
    error: HomeAssistantError | None = None
    try:
        response = await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_get_configuration_v201,
            service_data={"ocpp_key": key},
            blocking=True,
            return_response=True,
        )
    except HomeAssistantError as e:
        error = e
    return response, error


async def _test_services(hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint):
    service_response: ServiceResponse
    error: HomeAssistantError

    service_response, error = await _set_variable(
        hass, cs, cp, "SampledDataCtrlr/TxUpdatedInterval", "17"
    )
    assert service_response == {"reboot_required": False}
    assert cp.tx_updated_interval == 17

    service_response, error = await _set_variable(
        hass, cs, cp, "SampledDataCtrlr/RebootRequired", "17"
    )
    assert service_response == {"reboot_required": True}

    service_response, error = await _set_variable(
        hass, cs, cp, "TestComponent(CompInstance)/TestVariable(VarInstance)", "17"
    )
    assert service_response == {"reboot_required": False}
    assert cp.component_instance_used == "CompInstance"
    assert cp.variable_instance_used == "VarInstance"

    service_response, error = await _set_variable(
        hass, cs, cp, "SampledDataCtrlr/BadVariable", "17"
    )
    assert error is not None
    assert str(error).startswith("Failed to set variable")

    service_response, error = await _set_variable(
        hass, cs, cp, "SampledDataCtrlr/VeryBadVariable", "17"
    )
    assert error is not None
    assert str(error).startswith("OCPP call failed: InternalError")

    service_response, error = await _set_variable(
        hass, cs, cp, "does not compute", "17"
    )
    assert error is not None
    assert str(error) == "Invalid OCPP key"

    service_response, error = await _get_variable(
        hass, cs, cp, "SampledDataCtrlr/TxUpdatedInterval"
    )
    assert service_response == {"value": "17"}

    service_response, error = await _get_variable(
        hass, cs, cp, "TestComponent(CompInstance)/TestInstance(VarInstance)"
    )
    assert service_response == {"value": "CompInstance,VarInstance"}

    service_response, error = await _get_variable(
        hass, cs, cp, "SampledDataCtrlr/BadVariale"
    )
    assert error is not None
    assert str(error).startswith("Failed to get variable")

    service_response, error = await _get_variable(
        hass, cs, cp, "SampledDataCtrlr/VeryBadVariable"
    )
    assert error is not None
    assert str(error).startswith("OCPP call failed: InternalError")


async def _run_test(hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint):
    boot_res: call_result.BootNotification = await cp.call(
        call.BootNotification(
            {
                "serial_number": "SERIAL",
                "model": "MODEL",
                "vendor_name": "VENDOR",
                "firmware_version": "VERSION",
            },
            BootReasonType.power_up.value,
        )
    )
    assert boot_res.status == RegistrationStatusType.accepted.value
    assert boot_res.status_info is None
    datetime.fromisoformat(boot_res.current_time)
    await cp.call(
        call.StatusNotification(
            datetime.now(tz=UTC).isoformat(), ConnectorStatusType.available, 1, 1
        )
    )
    await wait_ready(hass)
    cpid: str = MOCK_CONFIG_DATA[CONF_CPID]
    assert cs.get_metric(cpid, cdet.serial.value) == "SERIAL"
    assert cs.get_metric(cpid, cdet.model.value) == "MODEL"
    assert cs.get_metric(cpid, cdet.vendor.value) == "VENDOR"
    assert cs.get_metric(cpid, cdet.firmware_version.value) == "VERSION"
    assert (
        cs.get_metric(cpid, cdet.features.value)
        == Profiles.CORE | Profiles.SMART | Profiles.RES | Profiles.AUTH
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ConnectorStatusType.available.value
    )
    assert cp.tx_updated_interval == DEFAULT_METER_INTERVAL
    assert cp.tx_updated_measurands == supported_measurands

    while cp.operative is None:
        await asyncio.sleep(0.1)
    assert cp.operative

    await _test_transaction(hass, cs, cp)
    await _test_services(hass, cs, cp)


@pytest.mark.timeout(90)  # Set timeout for this test
async def test_cms_responses_v201(hass, socket_enabled):
    """Test central system responses to a charger."""

    # Should not have to do this ideally, however web socket in the CSMS
    # restarts if measurands reported by the charger differ from the list
    # from the configuration, which a real charger can deal with but this
    # test cannot
    config_data = MOCK_CONFIG_DATA.copy()
    config_data[CONF_MONITORED_VARIABLES] = ",".join(supported_measurands)

    config_data[CONF_PORT] = 9010
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN, data=config_data, entry_id="test_cms", title="test_cms"
    )
    cs: CentralSystem = await create_configuration(hass, config_entry)
    await run_charge_point_test(
        config_entry,
        "CP_2",
        ["ocpp2.0.1"],
        lambda ws: ChargePoint("CP_2_client", ws),
        [lambda cp: _run_test(hass, cs, cp)],
    )
    await remove_configuration(hass, config_entry)
