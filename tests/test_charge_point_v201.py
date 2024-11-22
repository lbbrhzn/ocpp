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
    set_number,
    press_button,
    create_configuration,
    run_charge_point_test,
    remove_configuration,
    wait_ready,
)
from .const import MOCK_CONFIG_DATA
from custom_components.ocpp.const import (
    DEFAULT_METER_INTERVAL,
    DOMAIN as OCPP_DOMAIN,
    CONF_PORT,
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
    AuthorizationStatusType,
    BootReasonType,
    ChangeAvailabilityStatusType,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ChargingStateType,
    ClearChargingProfileStatusType,
    ConnectorStatusType,
    DataType,
    FirmwareStatusType,
    GenericDeviceModelStatusType,
    GetVariableStatusType,
    IdTokenType,
    MeasurandType,
    MutabilityType,
    OperationalStatusType,
    PhaseType,
    ReadingContextType,
    RegistrationStatusType,
    ReportBaseType,
    RequestStartStopStatusType,
    ResetStatusType,
    ResetType,
    SetVariableStatusType,
    ReasonType,
    TransactionEventType,
    MessageTriggerType,
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
    charge_profiles_set: list[call.SetChargingProfile] = []
    charge_profiles_cleared: list[call.ClearChargingProfile] = []
    accept_reset: bool = True
    resets: list[call.Reset] = []

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

    @on(Action.SetChargingProfile)
    def _on_set_charging_profile(self, evse_id: int, charging_profile: dict, **kwargs):
        self.charge_profiles_set.append(
            call.SetChargingProfile(evse_id, charging_profile)
        )
        unit = charging_profile["charging_schedule"][0]["charging_rate_unit"]
        limit = charging_profile["charging_schedule"][0]["charging_schedule_period"][0][
            "limit"
        ]
        if (unit == ChargingRateUnitType.amps.value) and (limit < 6):
            return call_result.SetChargingProfile(ChargingProfileStatus.rejected.value)
        return call_result.SetChargingProfile(ChargingProfileStatus.accepted.value)

    @on(Action.ClearChargingProfile)
    def _on_clear_charging_profile(self, **kwargs):
        self.charge_profiles_cleared.append(
            call.ClearChargingProfile(
                kwargs.get("charging_profile_id", None),
                kwargs.get("charging_profile_criteria", None),
            )
        )
        return call_result.ClearChargingProfile(
            ClearChargingProfileStatusType.accepted.value
        )

    @on(Action.Reset)
    def _on_reset(self, type: str, **kwargs):
        self.resets.append(call.Reset(type, kwargs.get("evse_id", None)))
        return call_result.Reset(
            ResetStatusType.accepted.value
            if self.accept_reset
            else ResetStatusType.rejected.value
        )

    async def _start_transaction_remote_start(
        self, id_token: dict, remote_start_id: int
    ):
        # As if AuthorizeRemoteStart is set
        authorize_resp: call_result.Authorize = await self.call(
            call.Authorize(id_token)
        )
        assert (
            authorize_resp.id_token_info["status"]
            == AuthorizationStatusType.accepted.value
        )

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
            meter_value=[
                {
                    "timestamp": self.tx_start_time.isoformat(),
                    "sampled_value": [
                        {
                            "value": 0,
                            "measurand": Measurand.power_active_import.value,
                            "unit_of_measure": {"unit": "W"},
                        },
                    ],
                },
            ],
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


async def _test_transaction(hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint):
    cpid: str = cs.settings.cpid

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
                            "value": 0.1,
                            "measurand": Measurand.energy_active_import_register.value,
                            "unit_of_measure": {"unit": "Wh", "multiplier": 3},
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
                        {
                            # Not among enabled measurands, will be ignored
                            "value": 1111,
                            "measurand": MeasurandType.energy_active_net.value,
                            "unit_of_measure": {"unit": "Wh"},
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

    # Now with energy reading in Started transaction event
    await cp.call(
        call.TransactionEvent(
            TransactionEventType.started.value,
            tx_start_time.isoformat(),
            TriggerReasonType.cable_plugged_in.value,
            0,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.ev_connected.value,
            },
            meter_value=[
                {
                    "timestamp": tx_start_time.isoformat(),
                    "sampled_value": [
                        {
                            "value": 1000,
                            "measurand": Measurand.energy_active_import_register.value,
                            "unit_of_measure": {"unit": "kWh", "multiplier": -3},
                        },
                    ],
                },
            ],
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
            TriggerReasonType.charging_state_changed.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.charging.value,
            },
            meter_value=[
                {
                    "timestamp": tx_start_time.isoformat(),
                    "sampled_value": [
                        {
                            "value": 1234,
                            "measurand": Measurand.energy_active_import_register.value,
                            "unit_of_measure": {"unit": "kWh", "multiplier": -3},
                        },
                    ],
                },
            ],
        )
    )
    assert abs(cs.get_metric(cpid, csess.session_energy) - 0.234) < 1e-6

    await cp.call(
        call.TransactionEvent(
            TransactionEventType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonType.charging_state_changed.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.suspended_ev.value,
            },
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.suspended_ev
    )

    await cp.call(
        call.TransactionEvent(
            TransactionEventType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonType.charging_state_changed.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.suspended_evse.value,
            },
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.suspended_evse
    )

    await cp.call(
        call.TransactionEvent(
            TransactionEventType.ended.value,
            tx_start_time.isoformat(),
            TriggerReasonType.ev_communication_lost.value,
            2,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateType.idle.value,
                "stopped_reason": ReasonType.ev_disconnected.value,
            },
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.available
    )


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


async def _set_charge_rate_service(
    hass: HomeAssistant, data: dict
) -> HomeAssistantError:
    try:
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_set_charge_rate,
            service_data=data,
            blocking=True,
        )
    except HomeAssistantError as e:
        return e
    return None


async def _test_charge_profiles(
    hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint
):
    error: HomeAssistantError = await _set_charge_rate_service(
        hass, {"limit_watts": 3000}
    )
    assert error is None
    assert len(cp.charge_profiles_set) == 1
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 1,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeType.charging_station_max_profile,
        "charging_profile_kind": ChargingProfileKindType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 3000}],
                "charging_rate_unit": ChargingRateUnitType.watts.value,
            },
        ],
    }

    error = await _set_charge_rate_service(hass, {"limit_amps": 16})
    assert error is None
    assert len(cp.charge_profiles_set) == 2
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 1,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeType.charging_station_max_profile,
        "charging_profile_kind": ChargingProfileKindType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 16}],
                "charging_rate_unit": ChargingRateUnitType.amps.value,
            },
        ],
    }

    error = await _set_charge_rate_service(
        hass,
        {
            "custom_profile": """{
            'id': 2,
            'stack_level': 1,
            'charging_profile_purpose': 'TxProfile',
            'charging_profile_kind': 'Relative',
            'charging_schedule': [{
                'id': 1,
                'charging_rate_unit': 'A',
                'charging_schedule_period': [{'start_period': 0, 'limit': 6}]
            }]
        }"""
        },
    )
    assert error is None
    assert len(cp.charge_profiles_set) == 3
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 2,
        "stack_level": 1,
        "charging_profile_purpose": ChargingProfilePurposeType.tx_profile.value,
        "charging_profile_kind": ChargingProfileKindType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 6}],
                "charging_rate_unit": ChargingRateUnitType.amps.value,
            },
        ],
    }

    await set_number(hass, cs, "maximum_current", 12)
    assert len(cp.charge_profiles_set) == 4
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 1,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeType.charging_station_max_profile.value,
        "charging_profile_kind": ChargingProfileKindType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 12}],
                "charging_rate_unit": ChargingRateUnitType.amps.value,
            },
        ],
    }

    error = await _set_charge_rate_service(hass, {"limit_amps": 5})
    assert error is not None
    assert str(error).startswith("Failed to set variable: Rejected")

    assert len(cp.charge_profiles_cleared) == 0
    await set_number(hass, cs, "maximum_current", 32)
    assert len(cp.charge_profiles_cleared) == 1
    assert cp.charge_profiles_cleared[-1].charging_profile_id is None
    assert cp.charge_profiles_cleared[-1].charging_profile_criteria == {
        "charging_profile_purpose": ChargingProfilePurposeType.charging_station_max_profile.value
    }


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

    heartbeat_resp: call_result.Heartbeat = await cp.call(call.Heartbeat())
    datetime.fromisoformat(heartbeat_resp.current_time)

    await wait_ready(hass)

    # Junk report to be ignored
    await cp.call(call.NotifyReport(2, datetime.now(tz=UTC).isoformat(), 0))

    cpid: str = cs.settings.cpid
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
    await _test_charge_profiles(hass, cs, cp)

    await press_button(hass, cs, "reset")
    assert len(cp.resets) == 1
    assert cp.resets[0].type == ResetType.immediate.value
    assert cp.resets[0].evse_id is None

    error: HomeAssistantError = None
    cp.accept_reset = False
    try:
        await press_button(hass, cs, "reset")
    except HomeAssistantError as e:
        error = e
    assert error is not None
    assert str(error) == "OCPP call failed: Rejected"

    await set_switch(hass, cs, "availability", False)
    assert not cp.operative
    await cp.call(
        call.StatusNotification(
            datetime.now(tz=UTC).isoformat(), ConnectorStatusType.unavailable, 1, 1
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ConnectorStatusType.unavailable.value
    )

    await cp.call(
        call.StatusNotification(
            datetime.now(tz=UTC).isoformat(), ConnectorStatusType.faulted, 1, 1
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ConnectorStatusType.faulted.value
    )

    await cp.call(call.FirmwareStatusNotification(FirmwareStatusType.installed.value))


class ChargePointAllFeatures(ChargePoint):
    """A charge point which also supports UpdateFirmware and TriggerMessage."""

    triggered_status_notification: list[EVSEType] = []

    @on(Action.UpdateFirmware)
    def _on_update_firmware(self, request_id: int, firmware: dict, **kwargs):
        return call_result.UpdateFirmware(UpdateFirmwareStatusType.rejected.value)

    @on(Action.TriggerMessage)
    def _on_trigger_message(self, requested_message: str, **kwargs):
        if (requested_message == MessageTriggerType.status_notification) and (
            "evse" in kwargs
        ):
            self.triggered_status_notification.append(
                EVSEType(kwargs["evse"]["id"], kwargs["evse"]["connector_id"])
            )
        return call_result.TriggerMessage(TriggerMessageStatusType.rejected.value)


async def _extra_features_test(
    hass: HomeAssistant,
    cs: CentralSystem,
    cp: ChargePointAllFeatures,
):
    await cp.call(
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
    await wait_ready(hass)

    assert (
        cs.get_metric(cs.settings.cpid, cdet.features.value)
        == Profiles.CORE
        | Profiles.SMART
        | Profiles.RES
        | Profiles.AUTH
        | Profiles.FW
        | Profiles.REM
    )

    while len(cp.triggered_status_notification) < 1:
        await asyncio.sleep(0.1)
    assert cp.triggered_status_notification[0].id == 1
    assert cp.triggered_status_notification[0].connector_id == 1


class ChargePointReportUnsupported(ChargePointAllFeatures):
    """A charge point which does not support GetBaseReport."""

    @on(Action.GetBaseReport)
    def _on_base_report(self, request_id: int, report_base: str, **kwargs):
        raise ocpp.exceptions.NotImplementedError("This is not implemented")


class ChargePointReportFailing(ChargePointAllFeatures):
    """A charge point which keeps failing GetBaseReport."""

    @on(Action.GetBaseReport)
    def _on_base_report(self, request_id: int, report_base: str, **kwargs):
        raise ocpp.exceptions.InternalError("Test failure")


async def _unsupported_base_report_test(
    hass: HomeAssistant,
    cs: CentralSystem,
    cp: ChargePoint,
):
    await cp.call(
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
    await wait_ready(hass)
    assert (
        cs.get_metric(cs.settings.cpid, cdet.features.value)
        == Profiles.CORE | Profiles.REM | Profiles.FW
    )


@pytest.mark.timeout(90)
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

    await run_charge_point_test(
        config_entry,
        "CP_2_allfeatures",
        ["ocpp2.0.1"],
        lambda ws: ChargePointAllFeatures("CP_2_allfeatures_client", ws),
        [lambda cp: _extra_features_test(hass, cs, cp)],
    )

    await remove_configuration(hass, config_entry)
    config_data[CONF_MONITORED_VARIABLES] = ""
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN, data=config_data, entry_id="test_cms", title="test_cms"
    )
    cs = await create_configuration(hass, config_entry)

    await run_charge_point_test(
        config_entry,
        "CP_2_noreport",
        ["ocpp2.0.1"],
        lambda ws: ChargePointReportUnsupported("CP_2_noreport_client", ws),
        [lambda cp: _unsupported_base_report_test(hass, cs, cp)],
    )

    await run_charge_point_test(
        config_entry,
        "CP_2_report_fail",
        ["ocpp2.0.1"],
        lambda ws: ChargePointReportFailing("CP_2_report_fail_client", ws),
        [lambda cp: _unsupported_base_report_test(hass, cs, cp)],
    )

    await remove_configuration(hass, config_entry)
