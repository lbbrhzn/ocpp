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
import websockets.server

from ocpp.routing import on
from ocpp.v201 import call, call_result
from ocpp.v16.enums import ChargePointStatus as ChargePointStatusv16
from ocpp.v201.enums import (
    ConnectorStatusType,
    GetVariableStatusType,
    IdTokenType,
    MeasurandType,
    OperationalStatusType,
    ResetType,
    ResetStatusType,
    SetVariableStatusType,
    AuthorizationStatusType,
    TransactionEventType,
    ReadingContextType,
    ChargingStateType,
)

from .chargepoint import CentralSystemSettings, OcppVersion, SetVariableResult, Metric
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
    HA_POWER_UNIT,
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
    tx_updated_measurands: list[MeasurandType] = []


class ChargePoint(cp):
    """Server side representation of a charger."""

    _inventory: InventoryReport | None = None
    _wait_inventory: asyncio.Event | None = None
    _connector_status: list[list[ConnectorStatusType | None]] = []
    _tx_start_time: datetime | None = None

    def __init__(
        self,
        id: str,
        connection: websockets.server.WebSocketServerProtocol,
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

    async def set_availability(self, state: bool = True):
        """Change availability."""
        req: call.ChangeAvailability = call.ChangeAvailability(
            OperationalStatusType.operative.value
            if state
            else OperationalStatusType.inoperative.value
        )
        await self.call(req)

    async def reset(self, typ: str = ""):
        """Hard reset charger unless soft reset requested."""
        req: call.Reset = call.Reset(ResetType.immediate)
        resp = await self.call(req)
        if resp.status != ResetStatusType.accepted.value:
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
        if result["attribute_status"] != GetVariableStatusType.accepted:
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
        if result["attribute_status"] == SetVariableStatusType.accepted:
            return SetVariableResult.accepted
        elif result["attribute_status"] == SetVariableStatusType.reboot_required:
            return SetVariableResult.reboot_required
        else:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_variables_error",
                translation_placeholders={"message": str(result)},
            )

    @on("BootNotification")
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

    @on("Heartbeat")
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

    @on("StatusNotification")
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

        evse: list[ConnectorStatusType] = self._connector_status[evse_id - 1]
        evse[connector_id - 1] = ConnectorStatusType(connector_status)
        evse_status: ConnectorStatusType | None = None
        for status in evse:
            if status is None:
                evse_status = status
                break
            else:
                evse_status = status
                if status != ConnectorStatusType.available:
                    break
        evse_status_v16: ChargePointStatusv16 | None
        if evse_status is None:
            evse_status_v16 = None
        elif evse_status == ConnectorStatusType.available:
            evse_status_v16 = ChargePointStatusv16.available
        elif evse_status == ConnectorStatusType.faulted:
            evse_status_v16 = ChargePointStatusv16.faulted
        elif evse_status == ConnectorStatusType.unavailable:
            evse_status_v16 = ChargePointStatusv16.unavailable
        else:
            evse_status_v16 = ChargePointStatusv16.preparing

        if evse_status_v16:
            self._report_evse_status(evse_id, evse_status_v16)

        return call_result.StatusNotification()

    @on("FirmwareStatusNotification")
    @on("MeterValues")
    @on("LogStatusNotification")
    @on("NotifyEvent")
    def ack(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.StatusNotification()

    @on("NotifyReport")
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
                    MeasurandType(s) for s in values.split(",")
                ]

        if not kwargs.get("tbc", False):
            self._wait_inventory.set()
        return call_result.NotifyReport()

    @on("Authorize")
    def on_authorize(self, id_token: dict, **kwargs):
        """Perform OCPP callback."""
        status: str = AuthorizationStatusType.unknown.value
        token_type: str = id_token["type"]
        token: str = id_token["id_token"]
        if (
            (token_type == IdTokenType.iso14443)
            or (token_type == IdTokenType.iso15693)
            or (token_type == IdTokenType.central)
        ):
            status = self.get_authorization_status(token)
        return call_result.Authorize(id_token_info={"status": status})

    @staticmethod
    def _get_value(sampled_value: dict) -> float:
        value: float = sampled_value["value"]
        unit_of_measure: dict = sampled_value.get("unit_of_measure", {})
        multiplier: int = unit_of_measure.get("multiplier", 0)
        if multiplier != 0:
            value *= pow(10, multiplier)
        return value

    @staticmethod
    def _set_energy(metric: Metric, sampled_value: dict):
        metric.unit = HA_ENERGY_UNIT
        value: float = ChargePoint._get_value(sampled_value)
        unit_of_measure: dict = sampled_value.get("unit_of_measure", {})
        unit: str = unit_of_measure.get("unit", "Wh")
        if unit == "Wh":
            value *= 0.001
        metric.value = value

    @staticmethod
    def _set_current(metric: Metric, sampled_values: list[dict]):
        total: float = 0
        for sampled_value in sampled_values:
            unit_of_measure: dict = sampled_value.get("unit_of_measure", {})
            metric.unit = unit_of_measure.get("unit", "A")
            total += ChargePoint._get_value(sampled_value)
        metric.value = total

    @staticmethod
    def _set_power(metric: Metric, sampled_values: list[dict]):
        total: float = 0
        metric.unit = HA_POWER_UNIT
        for sampled_value in sampled_values:
            value: float = ChargePoint._get_value(sampled_value)
            unit: str = sampled_value["unit_of_measure"]["unit"]
            if unit == "W":
                value *= 0.001
            total += value
        metric.value = total

    @staticmethod
    def _set_voltage(metric: Metric, sampled_values: list[dict]):
        average: float = 0
        for sampled_value in sampled_values:
            metric.unit = sampled_value.get("unit_of_measure", {}).get("unit", "V")
            average += ChargePoint._get_value(sampled_value)
        metric.value = average / len(sampled_values)

    @staticmethod
    def _set_frequency(metric: Metric, sampled_values: list[dict]):
        average: float = 0
        for sampled_value in sampled_values:
            metric.unit = sampled_value.get("unit_of_measure", {}).get("unit", "Hz")
            average += ChargePoint._get_value(sampled_value)
        metric.value = average / len(sampled_values)

    @staticmethod
    def _set_soc(metric: Metric, sampled_value: dict):
        metric.value = ChargePoint._get_value(sampled_value)
        metric.unit = sampled_value.get("unit_of_measure", {}).get("unit", "Percent")

    def _set_measurand(self, measurand: str, sampled_values: list[dict]):
        if measurand == MeasurandType.energy_active_import_register.value:
            self._set_energy(self._metrics[csess.session_energy], sampled_values[0])
            if self._metrics[csess.meter_start].value is None:
                self._set_energy(self._metrics[csess.meter_start], sampled_values[0])
            meter_start: float = self._metrics[csess.meter_start].value
            energy_usage: float = (
                self._metrics[csess.session_energy].value - meter_start
            )
            energy_usage_rounded: float = 0.001 * round(energy_usage * 1000)
            self._metrics[csess.session_energy].value = energy_usage_rounded

        if measurand not in self._metrics:
            return
        metric: Metric = self._metrics[measurand]
        if measurand.startswith("Energy"):
            self._set_energy(metric, sampled_values[0])
        elif measurand.startswith("Current"):
            self._set_current(metric, sampled_values)
        elif measurand.startswith("Power") and (
            measurand != MeasurandType.power_factor.value
        ):
            self._set_power(metric, sampled_values)
        elif measurand == MeasurandType.voltage.value:
            self._set_voltage(metric, sampled_values)
        elif measurand == MeasurandType.frequency.value:
            self._set_frequency(metric, sampled_values)
        elif measurand == MeasurandType.soc.value:
            self._set_soc(metric, sampled_values[0])

    @staticmethod
    def _values_by_measurand(meter_value: dict) -> dict:
        result = {}
        for sampled_value in meter_value["sampled_value"]:
            measurand = sampled_value["measurand"]
            samples = result.get(measurand, [])
            samples.append(sampled_value)
            result[measurand] = samples
        return result

    def _set_meter_values(self, tx_event_type: str, meter_values: list[dict]):
        if tx_event_type == TransactionEventType.started.value:
            for meter_value in meter_values:
                measurands = self._values_by_measurand(meter_value)
                for measurand in measurands:
                    sampled_values = measurands[measurand]
                    if measurand == MeasurandType.energy_active_import_register.value:
                        self._set_energy(
                            self._metrics[csess.meter_start], sampled_values[0]
                        )
                    self._set_measurand(measurand, sampled_values)
        elif tx_event_type == TransactionEventType.updated.value:
            for meter_value in meter_values:
                measurands = self._values_by_measurand(meter_value)
                for measurand in measurands:
                    self._set_measurand(measurand, measurands[measurand])
        elif tx_event_type == TransactionEventType.ended.value:
            measurands_in_tx: set = set()
            for meter_value in meter_values:
                measurands = self._values_by_measurand(meter_value)
                for measurand in measurands:
                    sampled_values = measurands[measurand]
                    context: ReadingContextType = sampled_values[0]["context"]
                    if context == ReadingContextType.transaction_end:
                        measurands_in_tx.add(measurand)
                        self._set_measurand(measurand, sampled_values)
            if self._inventory:
                for measurand in self._inventory.tx_updated_measurands:
                    if (
                        (measurand not in measurands_in_tx)
                        and (measurand in self._metrics)
                        and not measurand.startswith("Energy")
                    ):
                        self._metrics[measurand].value = 0

    @on("TransactionEvent")
    def on_transaction_event(
        self, event_type, timestamp, trigger_reason, seq_no, transaction_info, **kwargs
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
            if state == ChargingStateType.idle:
                evse_status_v16 = ChargePointStatusv16.available
            elif state == ChargingStateType.ev_connected:
                evse_status_v16 = ChargePointStatusv16.preparing
            elif state == ChargingStateType.suspended_evse:
                evse_status_v16 = ChargePointStatusv16.suspended_evse
            elif state == ChargingStateType.suspended_ev:
                evse_status_v16 = ChargePointStatusv16.suspended_ev
            elif state == ChargingStateType.charging:
                evse_status_v16 = ChargePointStatusv16.charging
            if evse_status_v16:
                self._report_evse_status(evse_id, evse_status_v16)

        if event_type == TransactionEventType.started.value:
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
            if event_type == TransactionEventType.ended.value:
                self._metrics[csess.transaction_id.value].value = ""

        if not offline:
            self.hass.async_create_task(self.update(self.central.cpid))

        response = call_result.TransactionEvent()
        if "id_token" in kwargs:
            response.id_token_info = {"status": AuthorizationStatusType.accepted}
        return response
