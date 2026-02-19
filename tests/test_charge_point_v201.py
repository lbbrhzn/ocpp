"""Implement a test by a simulating an OCPP 2.0.1 chargepoint."""

import asyncio
from datetime import datetime, timedelta, UTC

from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.exceptions import HomeAssistantError
from ocpp.v16.enums import Measurand

from custom_components.ocpp.const import CONF_CPIDS, CONF_CPID, DOMAIN
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
from .const import MOCK_CONFIG_DATA, MOCK_CONFIG_DATA_3, MOCK_CONFIG_CP_APPEND
from custom_components.ocpp.const import (
    DEFAULT_METER_INTERVAL,
    DOMAIN as OCPP_DOMAIN,
    CONF_PORT,
    MEASURANDS,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import ocpp
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
    AuthorizationStatusEnumType,
    BootReasonEnumType,
    ChangeAvailabilityStatusEnumType,
    ChargingProfileKindEnumType,
    ChargingProfilePurposeEnumType,
    ChargingProfileStatusEnumType,
    ChargingRateUnitEnumType,
    ChargingStateEnumType,
    ClearChargingProfileStatusEnumType,
    ConnectorStatusEnumType,
    DataEnumType,
    FirmwareStatusEnumType,
    GenericDeviceModelStatusEnumType,
    GetVariableStatusEnumType,
    IdTokenEnumType,
    MeasurandEnumType,
    MutabilityEnumType,
    OperationalStatusEnumType,
    PhaseEnumType,
    ReadingContextEnumType,
    RegistrationStatusEnumType,
    ReportBaseEnumType,
    RequestStartStopStatusEnumType,
    ResetStatusEnumType,
    ResetEnumType,
    SetVariableStatusEnumType,
    ReasonEnumType,
    TransactionEventEnumType,
    MessageTriggerEnumType,
    TriggerMessageStatusEnumType,
    TriggerReasonEnumType,
    UpdateFirmwareStatusEnumType,
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

    @on(Action.get_base_report)
    def _on_base_report(self, request_id: int, report_base: str, **kwargs):
        assert report_base == ReportBaseEnumType.full_inventory.value
        self.task = asyncio.create_task(self._send_full_inventory(request_id))
        return call_result.GetBaseReport(
            GenericDeviceModelStatusEnumType.accepted.value
        )

    @on(Action.request_start_transaction)
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
            RequestStartStopStatusEnumType.accepted.value
        )

    @on(Action.request_stop_transaction)
    def _on_remote_stop(self, transaction_id: str, **kwargs):
        assert transaction_id == self.remote_start_tx_id
        self.remote_stops.append(transaction_id)
        return call_result.RequestStopTransaction(
            RequestStartStopStatusEnumType.accepted.value
        )

    @on(Action.set_variables)
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

            attr_result: SetVariableStatusEnumType
            if input["variable"] == {"name": "RebootRequired"}:
                attr_result = SetVariableStatusEnumType.reboot_required
            elif input["variable"] == {"name": "BadVariable"}:
                attr_result = SetVariableStatusEnumType.unknown_variable
            elif input["variable"] == {"name": "VeryBadVariable"}:
                raise ocpp.exceptions.InternalError()
            else:
                attr_result = SetVariableStatusEnumType.accepted
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

    @on(Action.get_variables)
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
                    GetVariableStatusEnumType.accepted
                    if value is not None
                    else GetVariableStatusEnumType.unknown_variable,
                    ComponentType(input["component"]["name"]),
                    VariableType(input["variable"]["name"]),
                    attribute_value=value,
                )
            )
        return call_result.GetVariables(result)

    @on(Action.change_availability)
    def _on_change_availability(self, operational_status: str, **kwargs):
        if operational_status == OperationalStatusEnumType.operative.value:
            self.operative = True
        elif operational_status == OperationalStatusEnumType.inoperative.value:
            self.operative = False
        else:
            assert False
        return call_result.ChangeAvailability(
            ChangeAvailabilityStatusEnumType.accepted.value
        )

    @on(Action.set_charging_profile)
    def _on_set_charging_profile(self, evse_id: int, charging_profile: dict, **kwargs):
        self.charge_profiles_set.append(
            call.SetChargingProfile(evse_id, charging_profile)
        )
        unit = charging_profile["charging_schedule"][0]["charging_rate_unit"]
        limit = charging_profile["charging_schedule"][0]["charging_schedule_period"][0][
            "limit"
        ]
        if (unit == ChargingRateUnitEnumType.amps.value) and (limit < 6):
            return call_result.SetChargingProfile(
                ChargingProfileStatusEnumType.rejected.value
            )
        return call_result.SetChargingProfile(
            ChargingProfileStatusEnumType.accepted.value
        )

    @on(Action.clear_charging_profile)
    def _on_clear_charging_profile(self, **kwargs):
        self.charge_profiles_cleared.append(
            call.ClearChargingProfile(
                kwargs.get("charging_profile_id", None),
                kwargs.get("charging_profile_criteria", None),
            )
        )
        return call_result.ClearChargingProfile(
            ClearChargingProfileStatusEnumType.accepted.value
        )

    @on(Action.reset)
    def _on_reset(self, type: str, **kwargs):
        self.resets.append(call.Reset(type, kwargs.get("evse_id", None)))
        return call_result.Reset(
            ResetStatusEnumType.accepted.value
            if self.accept_reset
            else ResetStatusEnumType.rejected.value
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
            == AuthorizationStatusEnumType.accepted.value
        )

        self.tx_start_time = datetime.now(tz=UTC)
        request = call.TransactionEvent(
            TransactionEventEnumType.started.value,
            self.tx_start_time.isoformat(),
            TriggerReasonEnumType.remote_start.value,
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
                                value="true", mutability=MutabilityEnumType.read_only
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
                                value="true", mutability=MutabilityEnumType.read_only
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
                                value="true", mutability=MutabilityEnumType.read_only
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
                                value="true", mutability=MutabilityEnumType.read_only
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
                                value="true", mutability=MutabilityEnumType.read_only
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
                            DataEnumType.member_list,
                            False,
                            values_list=",".join(supported_measurands),
                        ),
                    ),
                ],
            )
        )


async def _test_transaction(hass: HomeAssistant, cs: CentralSystem, cp: ChargePoint):
    cp_id = cp.id[:-7]
    cpid = cs.charge_points[cp_id].settings.cpid

    await set_switch(hass, cpid, "charge_control", True)
    assert len(cp.remote_starts) == 1
    assert cp.remote_starts[0].id_token == {
        "id_token": cs.charge_points[cs.cpids[cpid]]._remote_id_tag,
        "type": IdTokenEnumType.central.value,
    }
    while cs.get_metric(cpid, csess.transaction_id.value) is None:
        await asyncio.sleep(0.1)
    assert cs.get_metric(cpid, csess.transaction_id.value) == cp.remote_start_tx_id

    tx_start_time = cp.tx_start_time
    await cp.call(
        call.StatusNotification(
            tx_start_time.isoformat(), ConnectorStatusEnumType.occupied, 1, 1
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.preparing
    )

    await cp.call(
        call.TransactionEvent(
            TransactionEventEnumType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonEnumType.cable_plugged_in.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
            },
        )
    )
    await cp.call(
        call.TransactionEvent(
            TransactionEventEnumType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonEnumType.charging_state_changed.value,
            2,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.charging.value,
            },
            meter_value=[
                {
                    "timestamp": tx_start_time.isoformat(),
                    "sampled_value": [
                        {
                            "value": 0,
                            "measurand": Measurand.current_export.value,
                            "phase": PhaseEnumType.l1.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.current_export.value,
                            "phase": PhaseEnumType.l2.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 0,
                            "measurand": Measurand.current_export.value,
                            "phase": PhaseEnumType.l3.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 1.1,
                            "measurand": Measurand.current_import.value,
                            "phase": PhaseEnumType.l1.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 2.2,
                            "measurand": Measurand.current_import.value,
                            "phase": PhaseEnumType.l2.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 3.3,
                            "measurand": Measurand.current_import.value,
                            "phase": PhaseEnumType.l3.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 12.1,
                            "measurand": Measurand.current_offered.value,
                            "phase": PhaseEnumType.l1.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 12.2,
                            "measurand": Measurand.current_offered.value,
                            "phase": PhaseEnumType.l2.value,
                            "unit_of_measure": {"unit": "A"},
                        },
                        {
                            "value": 12.3,
                            "measurand": Measurand.current_offered.value,
                            "phase": PhaseEnumType.l3.value,
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
                            "phase": PhaseEnumType.l1_n.value,
                            "unit_of_measure": {"unit": "V"},
                        },
                        {
                            "value": 230,
                            "measurand": Measurand.voltage.value,
                            "phase": PhaseEnumType.l2_n.value,
                            "unit_of_measure": {"unit": "V"},
                        },
                        {
                            "value": 230.4,
                            "measurand": Measurand.voltage.value,
                            "phase": PhaseEnumType.l3_n.value,
                            "unit_of_measure": {"unit": "V"},
                        },
                        {
                            # Not among enabled measurands, will be ignored
                            "value": 1111,
                            "measurand": MeasurandEnumType.energy_active_net.value,
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
    assert cs.get_metric(cpid, Measurand.current_import.value) == pytest.approx(2.2)
    assert cs.get_metric(cpid, Measurand.current_offered.value) == pytest.approx(12.2)
    assert cs.get_metric(cpid, Measurand.energy_active_export_register.value) == 0
    assert cs.get_metric(cpid, Measurand.energy_active_import_register.value) == 0.1
    assert cs.get_metric(cpid, Measurand.energy_reactive_export_register.value) == 0
    assert cs.get_metric(cpid, Measurand.energy_reactive_import_register.value) == 0
    assert cs.get_metric(cpid, Measurand.frequency.value) == 50
    assert cs.get_metric(cpid, Measurand.power_active_export.value) == 0
    assert cs.get_metric(cpid, Measurand.power_active_import.value) == 1.518
    assert cs.get_metric(cpid, Measurand.power_offered.value) == 8.418
    assert cs.get_metric(cpid, Measurand.power_reactive_export.value) == 0
    assert cs.get_metric(cpid, Measurand.power_reactive_import.value) == 0
    assert cs.get_metric(cpid, Measurand.soc.value) == 69
    assert cs.get_metric(cpid, Measurand.voltage.value) == 230.1
    assert cs.get_metric(cpid, csess.session_energy) == 0
    assert cs.get_metric(cpid, csess.session_time) == 0

    await cp.call(
        call.TransactionEvent(
            TransactionEventEnumType.updated.value,
            (tx_start_time + timedelta(seconds=60)).isoformat(),
            TriggerReasonEnumType.meter_value_periodic.value,
            3,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.charging.value,
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

    await set_switch(hass, cpid, "charge_control", False)
    assert len(cp.remote_stops) == 1

    await cp.call(
        call.TransactionEvent(
            TransactionEventEnumType.ended.value,
            (tx_start_time + timedelta(seconds=120)).isoformat(),
            TriggerReasonEnumType.remote_stop.value,
            4,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.ev_connected.value,
                "stopped_reason": ReasonEnumType.remote.value,
            },
            meter_value=[
                {
                    "timestamp": (tx_start_time + timedelta(seconds=120)).isoformat(),
                    "sampled_value": [
                        {
                            "value": 333,
                            "context": ReadingContextEnumType.transaction_end,
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
            TransactionEventEnumType.started.value,
            tx_start_time.isoformat(),
            TriggerReasonEnumType.cable_plugged_in.value,
            0,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.ev_connected.value,
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
            TransactionEventEnumType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonEnumType.charging_state_changed.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.charging.value,
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
    assert cs.get_metric(cpid, csess.session_energy) == 0.234

    await cp.call(
        call.TransactionEvent(
            TransactionEventEnumType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonEnumType.charging_state_changed.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.suspended_ev.value,
            },
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.suspended_ev
    )

    await cp.call(
        call.TransactionEvent(
            TransactionEventEnumType.updated.value,
            tx_start_time.isoformat(),
            TriggerReasonEnumType.charging_state_changed.value,
            1,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.suspended_evse.value,
            },
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ChargePointStatusv16.suspended_evse
    )

    await cp.call(
        call.TransactionEvent(
            TransactionEventEnumType.ended.value,
            tx_start_time.isoformat(),
            TriggerReasonEnumType.ev_communication_lost.value,
            2,
            transaction_info={
                "transaction_id": cp.remote_start_tx_id,
                "charging_state": ChargingStateEnumType.idle.value,
                "stopped_reason": ReasonEnumType.ev_disconnected.value,
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
    cp_id = cp.id[:-7]
    cpid = cs.charge_points[cp_id].settings.cpid
    try:
        response = await hass.services.async_call(
            DOMAIN,
            csvcs.service_configure,
            service_data={"devid": cpid, "ocpp_key": key, "value": value},
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
    cp_id = cp.id[:-7]
    cpid = cs.charge_points[cp_id].settings.cpid
    try:
        response = await hass.services.async_call(
            DOMAIN,
            csvcs.service_get_configuration,
            service_data={"devid": cpid, "ocpp_key": key},
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
            DOMAIN,
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
    cp_id = cp.id[:-7]
    cpid = cs.charge_points[cp_id].settings.cpid

    error: HomeAssistantError = await _set_charge_rate_service(
        hass, {"devid": cpid, "limit_watts": 3000}
    )

    assert error is None
    assert len(cp.charge_profiles_set) == 1
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 1,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile,
        "charging_profile_kind": ChargingProfileKindEnumType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 3000}],
                "charging_rate_unit": ChargingRateUnitEnumType.watts.value,
            },
        ],
    }

    error = await _set_charge_rate_service(hass, {"devid": cpid, "limit_amps": 16})
    assert error is None
    assert len(cp.charge_profiles_set) == 2
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 1,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile,
        "charging_profile_kind": ChargingProfileKindEnumType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 16}],
                "charging_rate_unit": ChargingRateUnitEnumType.amps.value,
            },
        ],
    }

    error = await _set_charge_rate_service(
        hass,
        {
            "devid": cpid,
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
        }""",
        },
    )
    assert error is None
    assert len(cp.charge_profiles_set) == 3
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 2,
        "stack_level": 1,
        "charging_profile_purpose": ChargingProfilePurposeEnumType.tx_profile.value,
        "charging_profile_kind": ChargingProfileKindEnumType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 6}],
                "charging_rate_unit": ChargingRateUnitEnumType.amps.value,
            },
        ],
    }

    await set_number(hass, cpid, "maximum_current", 12)
    assert len(cp.charge_profiles_set) == 4
    assert cp.charge_profiles_set[-1].evse_id == 0
    assert cp.charge_profiles_set[-1].charging_profile == {
        "id": 1,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile.value,
        "charging_profile_kind": ChargingProfileKindEnumType.relative.value,
        "charging_schedule": [
            {
                "id": 1,
                "charging_schedule_period": [{"start_period": 0, "limit": 12}],
                "charging_rate_unit": ChargingRateUnitEnumType.amps.value,
            },
        ],
    }

    error = await _set_charge_rate_service(hass, {"devid": cpid, "limit_amps": 5})
    assert error is not None
    assert str(error).startswith("Failed to set variable: Rejected")

    assert len(cp.charge_profiles_cleared) == 0
    await set_number(hass, cpid, "maximum_current", 32)
    assert len(cp.charge_profiles_cleared) == 1
    assert cp.charge_profiles_cleared[-1].charging_profile_id is None
    assert cp.charge_profiles_cleared[-1].charging_profile_criteria == {
        "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile.value
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
            BootReasonEnumType.power_up.value,
        )
    )
    assert boot_res.status == RegistrationStatusEnumType.accepted.value
    assert boot_res.status_info is None
    datetime.fromisoformat(boot_res.current_time)
    await cp.call(
        call.StatusNotification(
            datetime.now(tz=UTC).isoformat(), ConnectorStatusEnumType.available, 1, 1
        )
    )

    heartbeat_resp: call_result.Heartbeat = await cp.call(call.Heartbeat())
    datetime.fromisoformat(heartbeat_resp.current_time)

    cp_id = cp.id[:-7]
    cpid = cs.charge_points[cp_id].settings.cpid

    await wait_ready(cs.charge_points[cp_id])

    # Junk report to be ignored
    await cp.call(call.NotifyReport(2, datetime.now(tz=UTC).isoformat(), 0))

    assert cs.get_metric(cpid, cdet.serial.value, connector_id=0) == "SERIAL"
    assert cs.get_metric(cpid, cdet.model.value, connector_id=0) == "MODEL"
    assert cs.get_metric(cpid, cdet.vendor.value, connector_id=0) == "VENDOR"
    assert cs.get_metric(cpid, cdet.firmware_version.value, connector_id=0) == "VERSION"
    assert (
        cs.get_metric(cpid, cdet.features.value, connector_id=0)
        == Profiles.CORE | Profiles.SMART | Profiles.RES | Profiles.AUTH
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ConnectorStatusEnumType.available.value
    )
    assert cp.tx_updated_interval == DEFAULT_METER_INTERVAL
    assert cp.tx_updated_measurands == supported_measurands

    while cp.operative is None:
        await asyncio.sleep(0.1)
    assert cp.operative

    await _test_transaction(hass, cs, cp)
    await _test_services(hass, cs, cp)
    await _test_charge_profiles(hass, cs, cp)

    await press_button(hass, cpid, "reset")
    assert len(cp.resets) == 1
    assert cp.resets[0].type == ResetEnumType.immediate.value
    assert cp.resets[0].evse_id is None

    error: HomeAssistantError = None
    cp.accept_reset = False
    try:
        await press_button(hass, cpid, "reset")
    except HomeAssistantError as e:
        error = e
    assert error is not None
    assert str(error) == "OCPP call failed: Rejected"

    await set_switch(hass, cpid, "availability", False)
    assert not cp.operative
    await cp.call(
        call.StatusNotification(
            datetime.now(tz=UTC).isoformat(), ConnectorStatusEnumType.unavailable, 1, 1
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ConnectorStatusEnumType.unavailable.value
    )

    await cp.call(
        call.StatusNotification(
            datetime.now(tz=UTC).isoformat(), ConnectorStatusEnumType.faulted, 1, 1
        )
    )
    assert (
        cs.get_metric(cpid, cstat.status_connector.value)
        == ConnectorStatusEnumType.faulted.value
    )

    await cp.call(
        call.FirmwareStatusNotification(FirmwareStatusEnumType.installed.value)
    )


class ChargePointAllFeatures(ChargePoint):
    """A charge point which also supports UpdateFirmware and TriggerMessage."""

    triggered_status_notification: list[EVSEType] = []

    @on(Action.update_firmware)
    def _on_update_firmware(self, request_id: int, firmware: dict, **kwargs):
        return call_result.UpdateFirmware(UpdateFirmwareStatusEnumType.rejected.value)

    @on(Action.trigger_message)
    def _on_trigger_message(self, requested_message: str, **kwargs):
        if (requested_message == MessageTriggerEnumType.status_notification) and (
            "evse" in kwargs
        ):
            self.triggered_status_notification.append(
                EVSEType(kwargs["evse"]["id"], kwargs["evse"]["connector_id"])
            )
        return call_result.TriggerMessage(TriggerMessageStatusEnumType.rejected.value)


async def _extra_features_test(
    hass: HomeAssistant,
    cs: CentralSystem,
    cp: ChargePointAllFeatures,
):
    cp_id = cp.id[:-7]
    cpid = cs.charge_points[cp_id].settings.cpid

    await cp.call(
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
    await wait_ready(cs.charge_points[cp_id])

    assert (
        cs.get_metric(cpid, cdet.features.value, connector_id=0)
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

    @on(Action.get_base_report)
    def _on_base_report(self, request_id: int, report_base: str, **kwargs):
        raise ocpp.exceptions.NotImplementedError("This is not implemented")


class ChargePointReportFailing(ChargePointAllFeatures):
    """A charge point which keeps failing GetBaseReport."""

    @on(Action.get_base_report)
    def _on_base_report(self, request_id: int, report_base: str, **kwargs):
        raise ocpp.exceptions.InternalError("Test failure")


async def _unsupported_base_report_test(
    hass: HomeAssistant,
    cs: CentralSystem,
    cp: ChargePoint,
):
    cp_id = cp.id[:-7]
    cpid = cs.charge_points[cp_id].settings.cpid

    await cp.call(
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
    await wait_ready(cs.charge_points[cp_id])
    assert (
        cs.get_metric(cpid, cdet.features.value, connector_id=0)
        == Profiles.CORE | Profiles.REM | Profiles.FW
    )


@pytest.mark.timeout(150)
async def test_cms_responses_v201(hass, socket_enabled):
    """Test central system responses to a charger."""

    # Should not have to do this ideally, however web socket in the CSMS
    # restarts if measurands reported by the charger differ from the list
    # from the configuration, which a real charger can deal with but this
    # test cannot
    # config_data[CONF_MONITORED_VARIABLES] = ",".join(supported_measurands)
    cp_id = "CP_2"
    config_data = MOCK_CONFIG_DATA.copy()
    config_data[CONF_CPIDS].append({cp_id: MOCK_CONFIG_CP_APPEND.copy()})
    config_data[CONF_CPIDS][-1][cp_id][CONF_CPID] = "test_v201_cpid"

    config_data[CONF_PORT] = 9080

    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=config_data,
        entry_id="test_v201_cms",
        title="test_v201_cms",
        version=2,
        minor_version=0,
    )
    cs: CentralSystem = await create_configuration(hass, config_entry)
    # threading in async validation causes tests to fail
    ocpp.messages.ASYNC_VALIDATION = False
    await run_charge_point_test(
        config_entry,
        cp_id,
        ["ocpp2.0.1"],
        lambda ws: ChargePoint("CP_2_client", ws),
        [lambda cp: _run_test(hass, cs, cp)],
    )

    # add v2.1 charger to config entry
    entry = hass.config_entries._entries.get_entries_for_domain(OCPP_DOMAIN)[0]
    cp_id3 = "CP_2_1_allfeatures"
    entry.data[CONF_CPIDS].append({cp_id3: MOCK_CONFIG_CP_APPEND.copy()})
    entry.data[CONF_CPIDS][-1][cp_id3][CONF_CPID] = "test_v21_cpid3"
    # need to reload to setup sensors etc for new charger
    await hass.config_entries.async_reload(entry.entry_id)
    cs = hass.data[DOMAIN][entry.entry_id]

    await run_charge_point_test(
        config_entry,
        cp_id3,
        ["ocpp2.1"],
        lambda ws: ChargePointAllFeatures("CP_2_1_allfeatures_client", ws),
        [lambda cp: _extra_features_test(hass, cs, cp)],
    )

    await remove_configuration(hass, config_entry)

    cp_id = "CP_2_noreport"
    config_data = MOCK_CONFIG_DATA_3.copy()
    config_data[CONF_CPIDS].append({cp_id: MOCK_CONFIG_CP_APPEND.copy()})
    config_data[CONF_CPIDS][-1][cp_id][CONF_CPID] = "test_v201_cpid"

    config_data[CONF_PORT] = 9011

    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=config_data,
        entry_id="test_v201_cms",
        title="test_v201_cms",
        version=2,
        minor_version=0,
    )
    cs = await create_configuration(hass, config_entry)

    await run_charge_point_test(
        config_entry,
        cp_id,
        ["ocpp2.0.1"],
        lambda ws: ChargePointReportUnsupported("CP_2_noreport_client", ws),
        [lambda cp: _unsupported_base_report_test(hass, cs, cp)],
    )

    cp_id2 = "CP_2_report_fail"
    entry = hass.config_entries._entries.get_entries_for_domain(OCPP_DOMAIN)[0]
    entry.data[CONF_CPIDS].append({cp_id2: MOCK_CONFIG_CP_APPEND.copy()})
    entry.data[CONF_CPIDS][-1][cp_id2][CONF_CPID] = "test_v201_cpid2"
    # need to reload to setup sensors etc for new charger
    await hass.config_entries.async_reload(entry.entry_id)
    cs = hass.data[DOMAIN][entry.entry_id]

    await run_charge_point_test(
        config_entry,
        cp_id2,
        ["ocpp2.0.1"],
        lambda ws: ChargePointReportFailing("CP_2_report_fail_client", ws),
        [lambda cp: _unsupported_base_report_test(hass, cs, cp)],
    )

    await remove_configuration(hass, config_entry)
