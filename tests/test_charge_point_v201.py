"""Implement a test by a simulating an OCPP 2.0.1 chargepoint."""

import asyncio
from datetime import datetime, UTC

from homeassistant.core import HomeAssistant
from ocpp.v16.enums import Measurand

from custom_components.ocpp import CentralSystem
from custom_components.ocpp.enums import (
    HAChargerDetails as cdet,
    HAChargerSession as csess,
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
    DOMAIN as OCPP_DOMAIN,
    CONF_CPID,
    CONF_MONITORED_VARIABLES,
    MEASURANDS,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ocpp.routing import on
from ocpp.v201 import ChargePoint as cpclass, call, call_result
from ocpp.v201.datatypes import (
    ComponentType,
    EVSEType,
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
    ConnectorStatusType,
    DataType,
    GenericDeviceModelStatusType,
    IdTokenType,
    MutabilityType,
    OperationalStatusType,
    RegistrationStatusType,
    ReportBaseType,
    RequestStartStopStatusType,
    SetVariableStatusType,
    TransactionEventType,
    TriggerMessageStatusType,
    TriggerReasonType,
    UpdateFirmwareStatusType,
)


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
            result.append(
                SetVariableResultType(
                    SetVariableStatusType.accepted,
                    ComponentType(input["component"]["name"]),
                    VariableType(input["variable"]["name"]),
                )
            )
        return call_result.SetVariables(result)

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
        request = call.TransactionEvent(
            TransactionEventType.started.value,
            datetime.now(tz=UTC).isoformat(),
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
    assert cs.get_metric(MOCK_CONFIG_DATA[CONF_CPID], cdet.serial.value) == "SERIAL"
    assert cs.get_metric(MOCK_CONFIG_DATA[CONF_CPID], cdet.model.value) == "MODEL"
    assert cs.get_metric(MOCK_CONFIG_DATA[CONF_CPID], cdet.vendor.value) == "VENDOR"
    assert (
        cs.get_metric(MOCK_CONFIG_DATA[CONF_CPID], cdet.firmware_version.value)
        == "VERSION"
    )
    assert (
        cs.get_metric(MOCK_CONFIG_DATA[CONF_CPID], cdet.features.value)
        == Profiles.CORE | Profiles.SMART | Profiles.RES | Profiles.AUTH
    )

    while cp.operative is None:
        await asyncio.sleep(0.1)
    assert cp.operative

    await set_switch(hass, "charge_control", True)
    assert len(cp.remote_starts) == 1
    assert cp.remote_starts[0].id_token == {
        "id_token": "HomeAssistantStart",
        "type": IdTokenType.central.value,
    }
    assert (
        cs.get_metric(MOCK_CONFIG_DATA[CONF_CPID], csess.transaction_id.value)
        == cp.remote_start_tx_id
    )

    await set_switch(hass, "charge_control", False)
    assert len(cp.remote_stops) == 1


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
