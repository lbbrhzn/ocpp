"""Representation of a OCPP 2.0.1 charging station."""

import asyncio
from datetime import datetime, UTC
import logging

import ocpp.exceptions
from ocpp.exceptions import OCPPError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, SupportsResponse, ServiceResponse
from homeassistant.exceptions import ServiceValidationError, HomeAssistantError
from websockets.asyncio.server import ServerConnection

from ocpp.routing import on
from ocpp.v201 import call, call_result
from ocpp.v16.enums import ChargePointStatus as ChargePointStatusv16
from ocpp.v201.enums import (
    Action,
    ConnectorStatusEnumType,
    GetVariableStatusEnumType,
    IdTokenEnumType,
    MeasurandEnumType,
    OperationalStatusEnumType,
    ResetEnumType,
    ResetStatusEnumType,
    SetVariableStatusEnumType,
    AuthorizationStatusEnumType,
    TransactionEventEnumType,
    ReadingContextEnumType,
    RequestStartStopStatusEnumType,
    ChargingStateEnumType,
    ChargingProfilePurposeEnumType,
    ChargingRateUnitEnumType,
    ChargingProfileKindEnumType,
    ChargingProfileStatusEnumType,
)

from .chargepoint import (
    CentralSystemSettings,
    OcppVersion,
    SetVariableResult,
    MeasurandValue,
)
from .chargepoint import ChargePoint as cp
from .chargepoint import CONF_SERVICE_DATA_SCHEMA, GCONF_SERVICE_DATA_SCHEMA

from .enums import Profiles

from .enums import (
    HAChargerStatuses as cstat,
    HAChargerServices as csvcs,
    HAChargerSession as csess,
)

from .const import (
    DEFAULT_METER_INTERVAL,
    DOMAIN,
    HA_ENERGY_UNIT,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


class InventoryReport:
    """Cached full inventory report for a charger."""

    evse_count: int = 0
    connector_count: list[int] = []
    smart_charging_available: bool = False
    reservation_available: bool = False
    local_auth_available: bool = False
    tx_updated_measurands: list[MeasurandEnumType] = []


class ChargePoint(cp):
    """Server side representation of a charger."""

    _inventory: InventoryReport | None = None
    _wait_inventory: asyncio.Event | None = None
    _connector_status: list[list[ConnectorStatusEnumType | None]] = []
    _tx_start_time: datetime | None = None

    def __init__(
        self,
        id: str,
        connection: ServerConnection,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        interval_meter_metrics: int = 10,
        skip_schema_validation: bool = False,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(
            id,
            connection,
            OcppVersion.V201,
            hass,
            entry,
            central,
            interval_meter_metrics,
            skip_schema_validation,
        )

    async def async_update_device_info_v201(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.central.cpid, boot_info)
        await self.async_update_device_info(
            boot_info.get("serial_number", None),
            boot_info.get("vendor_name", None),
            boot_info.get("model", None),
            boot_info.get("firmware_version", None),
        )

    async def _get_inventory(self):
        if self._inventory is not None:
            return
        self._wait_inventory = asyncio.Event()
        req = call.GetBaseReport(1, "FullInventory")
        resp: call_result.GetBaseReport | None = None
        try:
            resp: call_result.GetBaseReport = await self.call(req)
        except ocpp.exceptions.NotImplementedError:
            self._inventory = InventoryReport()
        except OCPPError:
            self._inventory = None
        if (resp is not None) and (resp.status == "Accepted"):
            await asyncio.wait_for(self._wait_inventory.wait(), self._response_timeout)
        self._wait_inventory = None

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        await self._get_inventory()
        return self._inventory.evse_count if self._inventory else 0

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        req = call.SetVariables(
            [
                {
                    "component": {"name": "SampledDataCtrlr"},
                    "variable": {"name": "TxUpdatedInterval"},
                    "attribute_value": str(DEFAULT_METER_INTERVAL),
                }
            ]
        )
        await self.call(req)

    def register_version_specific_services(self):
        """Register HA services that differ depending on OCPP version."""

        async def handle_configure(call) -> ServiceResponse:
            """Handle the configure service call."""
            key = call.data.get("ocpp_key")
            value = call.data.get("value")
            result: SetVariableResult = await self.configure(key, value)
            return {"reboot_required": result == SetVariableResult.reboot_required}

        async def handle_get_configuration(call) -> ServiceResponse:
            """Handle the get configuration service call."""
            key = call.data.get("ocpp_key")
            value = await self.get_configuration(key)
            return {"value": value}

        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_configure_v201.value,
            handle_configure,
            CONF_SERVICE_DATA_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_get_configuration_v201.value,
            handle_get_configuration,
            GCONF_SERVICE_DATA_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""
        await self._get_inventory()
        if self._inventory:
            measurands: str = ",".join(
                measurand.value for measurand in self._inventory.tx_updated_measurands
            )
            req = call.SetVariables(
                [
                    {
                        "component": {"name": "SampledDataCtrlr"},
                        "variable": {"name": "TxUpdatedMeasurands"},
                        "attribute_value": measurands,
                    }
                ]
            )
            await self.call(req)
            return measurands
        return ""

    async def get_supported_features(self) -> Profiles:
        """Get comma-separated list of measurands supported by the charger."""
        await self._get_inventory()
        features = Profiles.CORE
        if self._inventory and self._inventory.smart_charging_available:
            features |= Profiles.SMART
        if self._inventory and self._inventory.reservation_available:
            features |= Profiles.RES
        if self._inventory and self._inventory.local_auth_available:
            features |= Profiles.AUTH

        fw_req = call.UpdateFirmware(
            1,
            {
                "location": "dummy://dummy",
                "retrieveDateTime": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "signature": "☺",
            },
        )
        try:
            await self.call(fw_req)
            features |= Profiles.FW
        except OCPPError as e:
            _LOGGER.info("Firmware update not supported: %s", e)

        trigger_req = call.TriggerMessage("StatusNotification")
        try:
            await self.call(trigger_req)
            features |= Profiles.REM
        except OCPPError as e:
            _LOGGER.info("TriggerMessage not supported: %s", e)

        return features

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        if not self._inventory:
            return
        for evse_id in range(1, self._inventory.evse_count + 1):
            for connector_id in range(
                1, self._inventory.connector_count[evse_id - 1] + 1
            ):
                req = call.TriggerMessage(
                    "StatusNotification",
                    evse={"id": evse_id, "connector_id": connector_id},
                )
                await self.call(req)

    async def clear_profile(self):
        """Clear all charging profiles."""
        req: call.ClearChargingProfile = call.ClearChargingProfile(
            None,
            {
                "charging_profile_Purpose": ChargingProfilePurposeEnumType.charging_station_max_profile.value
            },
        )
        await self.call(req)

    async def set_charge_rate(
        self,
        limit_amps: int = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
    ):
        """Set a charging profile with defined limit."""
        req: call.SetChargingProfile
        if profile:
            req = call.SetChargingProfile(0, profile)
        else:
            period: dict = {"start_period": 0}
            schedule: dict = {"id": 1}
            if limit_amps < 32:
                period["limit"] = limit_amps
                schedule["charging_rate_unit"] = ChargingRateUnitEnumType.amps.value
            elif limit_watts < 22000:
                period["limit"] = limit_watts
                schedule["charging_rate_unit"] = ChargingRateUnitEnumType.watts.value
            else:
                await self.clear_profile()
                return

            schedule["charging_schedule_period"] = [period]
            req = call.SetChargingProfile(
                0,
                {
                    "id": 1,
                    "stack_level": 0,
                    "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile,
                    "charging_profile_kind": ChargingProfileKindEnumType.relative.value,
                    "charging_schedule": [schedule],
                },
            )

        resp: call_result.SetChargingProfile = await self.call(req)
        if resp.status != ChargingProfileStatusEnumType.accepted:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_variables_error",
                translation_placeholders={
                    "message": f"{str(resp.status)}: {str(resp.status_info)}"
                },
            )

    async def set_availability(self, state: bool = True):
        """Change availability."""
        req: call.ChangeAvailability = call.ChangeAvailability(
            OperationalStatusEnumType.operative.value
            if state
            else OperationalStatusEnumType.inoperative.value
        )
        await self.call(req)

    async def start_transaction(self) -> bool:
        """Remote start a transaction."""
        req: call.RequestStartTransaction = call.RequestStartTransaction(
            id_token={
                "id_token": self._remote_id_tag,
                "type": IdTokenEnumType.central.value,
            },
            remote_start_id=1,
        )
        resp: call_result.RequestStartTransaction = await self.call(req)
        return resp.status == RequestStartStopStatusEnumType.accepted.value

    async def stop_transaction(self) -> bool:
        """Request remote stop of current transaction."""
        req: call.RequestStopTransaction = call.RequestStopTransaction(
            transaction_id=self._metrics[csess.transaction_id.value].value
        )
        resp: call_result.RequestStopTransaction = await self.call(req)
        return resp.status == RequestStartStopStatusEnumType.accepted.value

    async def reset(self, typ: str = ""):
        """Hard reset charger unless soft reset requested."""
        req: call.Reset = call.Reset(ResetEnumType.immediate)
        resp = await self.call(req)
        if resp.status != ResetStatusEnumType.accepted.value:
            status_suffix: str = f": {resp.status_info}" if resp.status_info else ""
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ocpp_call_error",
                translation_placeholders={"message": resp.status + status_suffix},
            )

    @staticmethod
    def _parse_ocpp_key(key: str) -> tuple:
        try:
            [c, v] = key.split("/")
        except ValueError:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_ocpp_key",
            )
        [cname, paren, cinstance] = c.partition("(")
        cinstance = cinstance.partition(")")[0]
        [vname, paren, vinstance] = v.partition("(")
        vinstance = vinstance.partition(")")[0]
        component: dict = {"name": cname}
        if cinstance:
            component["instance"] = cinstance
        variable: dict = {"name": vname}
        if vinstance:
            variable["instance"] = vinstance
        return component, variable

    async def get_configuration(self, key: str = "") -> str | None:
        """Get Configuration of charger for supported keys else return None."""
        component, variable = self._parse_ocpp_key(key)
        req: call.GetVariables = call.GetVariables(
            [{"component": component, "variable": variable}]
        )
        try:
            resp: call_result.GetVariables = await self.call(req)
        except Exception as e:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ocpp_call_error",
                translation_placeholders={"message": str(e)},
            )
        result: dict = resp.get_variable_result[0]
        if result["attribute_status"] != GetVariableStatusEnumType.accepted:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="get_variables_error",
                translation_placeholders={"message": str(result)},
            )
        return result["attribute_value"]

    async def configure(self, key: str, value: str) -> SetVariableResult:
        """Configure charger by setting the key to target value."""
        component, variable = self._parse_ocpp_key(key)
        req: call.SetVariables = call.SetVariables(
            [{"component": component, "variable": variable, "attribute_value": value}]
        )
        try:
            resp: call_result.SetVariables = await self.call(req)
        except Exception as e:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ocpp_call_error",
                translation_placeholders={"message": str(e)},
            )
        result: dict = resp.set_variable_result[0]
        if result["attribute_status"] == SetVariableStatusEnumType.accepted:
            return SetVariableResult.accepted
        elif result["attribute_status"] == SetVariableStatusEnumType.reboot_required:
            return SetVariableResult.reboot_required
        else:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_variables_error",
                translation_placeholders={"message": str(result)},
            )

    @on(Action.boot_notification)
    def on_boot_notification(self, charging_station, reason, **kwargs):
        """Perform OCPP callback."""
        resp = call_result.BootNotification(
            current_time=datetime.now(tz=UTC).isoformat(),
            interval=10,
            status="Accepted",
        )

        self.hass.async_create_task(
            self.async_update_device_info_v201(charging_station)
        )
        self._inventory = None
        self._register_boot_notification()
        return resp

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.Heartbeat(current_time=datetime.now(tz=UTC).isoformat())

    def _report_evse_status(self, evse_id: int, evse_status_v16: ChargePointStatusv16):
        evse_status_str: str = evse_status_v16.value

        if evse_id == 1:
            self._metrics[cstat.status_connector.value].value = evse_status_str
        else:
            self._metrics[cstat.status_connector.value].extra_attr[evse_id] = (
                evse_status_str
            )
        self.hass.async_create_task(self.update(self.central.cpid))

    @on(Action.status_notification)
    def on_status_notification(
        self, timestamp: str, connector_status: str, evse_id: int, connector_id: int
    ):
        """Perform OCPP callback."""
        if evse_id > len(self._connector_status):
            self._connector_status += [[]] * (evse_id - len(self._connector_status))
        if connector_id > len(self._connector_status[evse_id - 1]):
            self._connector_status[evse_id - 1] += [None] * (
                connector_id - len(self._connector_status[evse_id - 1])
            )

        evse: list[ConnectorStatusEnumType] = self._connector_status[evse_id - 1]
        evse[connector_id - 1] = ConnectorStatusEnumType(connector_status)
        evse_status: ConnectorStatusEnumType | None = None
        for status in evse:
            if status is None:
                evse_status = status
                break
            else:
                evse_status = status
                if status != ConnectorStatusEnumType.available:
                    break
        evse_status_v16: ChargePointStatusv16 | None
        if evse_status is None:
            evse_status_v16 = None
        elif evse_status == ConnectorStatusEnumType.available:
            evse_status_v16 = ChargePointStatusv16.available
        elif evse_status == ConnectorStatusEnumType.faulted:
            evse_status_v16 = ChargePointStatusv16.faulted
        elif evse_status == ConnectorStatusEnumType.unavailable:
            evse_status_v16 = ChargePointStatusv16.unavailable
        else:
            evse_status_v16 = ChargePointStatusv16.preparing

        if evse_status_v16:
            self._report_evse_status(evse_id, evse_status_v16)

        return call_result.StatusNotification()

    @on(Action.firmware_status_notification)
    @on(Action.meter_values)
    @on(Action.log_status_notification)
    @on(Action.notify_event)
    def ack(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.StatusNotification()

    @on(Action.notify_report)
    def on_report(self, request_id: int, generated_at: str, seq_no: int, **kwargs):
        """Perform OCPP callback."""
        if self._wait_inventory is None:
            return call_result.NotifyReport()
        if self._inventory is None:
            self._inventory = InventoryReport()
        reports: list[dict] = kwargs.get("report_data", [])
        for report_data in reports:
            component: dict = report_data["component"]
            variable: dict = report_data["variable"]
            component_name = component["name"]
            variable_name = variable["name"]
            value: str | None = None
            for attribute in report_data["variable_attribute"]:
                if (("type" not in attribute) or (attribute["type"] == "Actual")) and (
                    "value" in attribute
                ):
                    value = attribute["value"]
                    break
            bool_value: bool = value and (value.casefold() == "true".casefold())

            if (component_name == "SmartChargingCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.smart_charging_available = bool_value
            elif (component_name == "ReservationCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.reservation_available = bool_value
            elif (component_name == "LocalAuthListCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.local_auth_available = bool_value
            elif (component_name == "EVSE") and ("evse" in component):
                self._inventory.evse_count = max(
                    self._inventory.evse_count, component["evse"]["id"]
                )
                self._inventory.connector_count += [0] * (
                    self._inventory.evse_count - len(self._inventory.connector_count)
                )
            elif (
                (component_name == "Connector")
                and ("evse" in component)
                and ("connector_id" in component["evse"])
            ):
                evse_id = component["evse"]["id"]
                self._inventory.evse_count = max(self._inventory.evse_count, evse_id)
                self._inventory.connector_count += [0] * (
                    self._inventory.evse_count - len(self._inventory.connector_count)
                )
                self._inventory.connector_count[evse_id - 1] = max(
                    self._inventory.connector_count[evse_id - 1],
                    component["evse"]["connector_id"],
                )
            elif (
                (component_name == "SampledDataCtrlr")
                and (variable_name == "TxUpdatedMeasurands")
                and ("variable_characteristics" in report_data)
            ):
                characteristics: dict = report_data["variable_characteristics"]
                values: str = characteristics.get("values_list", "")
                self._inventory.tx_updated_measurands = [
                    MeasurandEnumType(s) for s in values.split(",")
                ]

        if not kwargs.get("tbc", False):
            self._wait_inventory.set()
        return call_result.NotifyReport()

    @on(Action.authorize)
    def on_authorize(self, id_token: dict, **kwargs):
        """Perform OCPP callback."""
        status: str = AuthorizationStatusEnumType.unknown.value
        token_type: str = id_token["type"]
        token: str = id_token["id_token"]
        if (
            (token_type == IdTokenEnumType.iso14443)
            or (token_type == IdTokenEnumType.iso15693)
            or (token_type == IdTokenEnumType.central)
        ):
            status = self.get_authorization_status(token)
        return call_result.Authorize(id_token_info={"status": status})

    def _set_meter_values(self, tx_event_type: str, meter_values: list[dict]):
        converted_values: list[list[MeasurandValue]] = []
        for meter_value in meter_values:
            measurands: list[MeasurandValue] = []
            for sampled_value in meter_value["sampled_value"]:
                measurand: str = sampled_value.get(
                    "measurand", MeasurandEnumType.energy_active_import_register.value
                )
                value: float = sampled_value["value"]
                context: str = sampled_value.get("context", None)
                phase: str = sampled_value.get("phase", None)
                location: str = sampled_value.get("location", None)
                unit_struct: dict = sampled_value.get("unit_of_measure", {})
                unit: str = unit_struct.get("unit", None)
                multiplier: int = unit_struct.get("multiplier", 0)
                if multiplier != 0:
                    value *= pow(10, multiplier)
                measurands.append(
                    MeasurandValue(measurand, value, phase, unit, context, location)
                )
            converted_values.append(measurands)

        if (tx_event_type == TransactionEventEnumType.started.value) or (
            (tx_event_type == TransactionEventEnumType.updated.value)
            and (self._metrics[csess.meter_start].value is None)
        ):
            energy_measurand = MeasurandEnumType.energy_active_import_register.value
            for meter_value in converted_values:
                for measurand_item in meter_value:
                    if measurand_item.measurand == energy_measurand:
                        energy_value = ChargePoint.get_energy_kwh(measurand_item)
                        energy_unit = HA_ENERGY_UNIT if measurand_item.unit else None
                        self._metrics[csess.meter_start].value = energy_value
                        self._metrics[csess.meter_start].unit = energy_unit

        self.process_measurands(converted_values, True)

        if tx_event_type == TransactionEventEnumType.ended.value:
            measurands_in_tx: set[str] = set()
            tx_end_context = ReadingContextEnumType.transaction_end.value
            for meter_value in converted_values:
                for measurand_item in meter_value:
                    if measurand_item.context == tx_end_context:
                        measurands_in_tx.add(measurand_item.measurand)
            if self._inventory:
                for measurand in self._inventory.tx_updated_measurands:
                    if (
                        (measurand not in measurands_in_tx)
                        and (measurand in self._metrics)
                        and not measurand.startswith("Energy")
                    ):
                        self._metrics[measurand].value = 0

    @on(Action.transaction_event)
    def on_transaction_event(
        self,
        event_type,
        timestamp,
        trigger_reason,
        seq_no,
        transaction_info,
        **kwargs,
    ):
        """Perform OCPP callback."""
        offline: bool = kwargs.get("offline", False)
        meter_values: list[dict] = kwargs.get("meter_value", [])
        self._set_meter_values(event_type, meter_values)
        t = datetime.fromisoformat(timestamp)

        if "charging_state" in transaction_info:
            state = transaction_info["charging_state"]
            evse_id: int = kwargs["evse"]["id"] if "evse" in kwargs else 1
            evse_status_v16: ChargePointStatusv16 | None = None
            if state == ChargingStateEnumType.idle:
                evse_status_v16 = ChargePointStatusv16.available
            elif state == ChargingStateEnumType.ev_connected:
                evse_status_v16 = ChargePointStatusv16.preparing
            elif state == ChargingStateEnumType.suspended_evse:
                evse_status_v16 = ChargePointStatusv16.suspended_evse
            elif state == ChargingStateEnumType.suspended_ev:
                evse_status_v16 = ChargePointStatusv16.suspended_ev
            elif state == ChargingStateEnumType.charging:
                evse_status_v16 = ChargePointStatusv16.charging
            if evse_status_v16:
                self._report_evse_status(evse_id, evse_status_v16)

        response = call_result.TransactionEvent()
        id_token = kwargs.get("id_token")
        if id_token:
            response.id_token_info = {"status": AuthorizationStatusEnumType.accepted}
            id_tag_string: str = id_token["type"] + ":" + id_token["id_token"]
            self._metrics[cstat.id_tag.value].value = id_tag_string

        if event_type == TransactionEventEnumType.started.value:
            self._tx_start_time = t
            tx_id: str = transaction_info["transaction_id"]
            self._metrics[csess.transaction_id.value].value = tx_id
            self._metrics[csess.session_time].value = 0
            self._metrics[csess.session_time].unit = UnitOfTime.MINUTES
        else:
            if self._tx_start_time:
                duration_minutes: int = ((t - self._tx_start_time).seconds + 59) // 60
                self._metrics[csess.session_time].value = duration_minutes
                self._metrics[csess.session_time].unit = UnitOfTime.MINUTES
            if event_type == TransactionEventEnumType.ended.value:
                self._metrics[csess.transaction_id.value].value = ""
                self._metrics[cstat.id_tag.value].value = ""

        if not offline:
            self.hass.async_create_task(self.update(self.central.cpid))

        return response
