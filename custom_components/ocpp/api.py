"""Representation of a OCCP Entities."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import TIME_MINUTES
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
import voluptuous as vol
import websockets

from ocpp.exceptions import NotImplementedError
from ocpp.messages import CallError
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp, call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    Measurand,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnitOfMeasure,
    UnlockStatus,
)

from .const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_PORT,
    CONF_SUBPROTOCOL,
    DEFAULT_CPID,
    DEFAULT_CSID,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_HOST,
    DEFAULT_MEASURAND,
    DEFAULT_PORT,
    DEFAULT_POWER_UNIT,
    DEFAULT_SUBPROTOCOL,
    DOMAIN,
    FEATURE_PROFILE_FW,
    FEATURE_PROFILE_REMOTE,
    FEATURE_PROFILE_SMART,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    SERVICE_AVAILABILITY,
    SERVICE_CHARGE_START,
    SERVICE_CHARGE_STOP,
    SERVICE_RESET,
    SERVICE_UNLOCK,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.DEBUG)


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Instantiate instance of a CentralSystem."""
        self.hass = hass
        self.entry = entry
        self.host = entry.data.get(CONF_HOST) or DEFAULT_HOST
        self.port = entry.data.get(CONF_PORT) or DEFAULT_PORT
        self.csid = entry.data.get(CONF_CSID) or DEFAULT_CSID
        self.cpid = entry.data.get(CONF_CPID) or DEFAULT_CPID

        self.subprotocol = entry.data.get(CONF_SUBPROTOCOL) or DEFAULT_SUBPROTOCOL
        self._server = None
        self.config = entry.data
        self.id = entry.entry_id
        self.charge_points = {}

    @staticmethod
    async def create(hass: HomeAssistant, entry: ConfigEntry):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem(hass, entry)

        server = await websockets.serve(
            self.on_connect, self.host, self.port, subprotocols=self.subprotocol
        )
        self._server = server
        return self

    async def on_connect(self, websocket, path: str):
        """Request handler executed for every new OCPP connection."""

        _LOGGER.info(f"path={path}")
        cp_id = path.strip("/")
        try:
            if self.cpid not in self.charge_points:
                _LOGGER.info(f"Charger {cp_id} connected to {self.host}:{self.port}.")
                cp = ChargePoint(cp_id, websocket, self.hass, self.entry, self)
                self.charge_points[self.cpid] = cp
                await cp.start()
            else:
                _LOGGER.info(f"Charger {cp_id} reconnected to {self.host}:{self.port}.")
                cp = self.charge_points[self.cpid]
                await cp.reconnect(websocket)
        except Exception as e:
            _LOGGER.info(f"Exception occurred:\n{e}")
        finally:
            _LOGGER.info(f"Charger {cp_id} disconnected from {self.host}:{self.port}.")

    def get_metric(self, cp_id: str, measurand: str):
        """Return last known value for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].get_metric(measurand)
        return None

    def get_unit(self, cp_id: str, measurand: str):
        """Return unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].get_unit(measurand)
        return None

    async def set_charger_state(
        self, cp_id: str, service_name: str, state: bool = True
    ):
        """Carry out requested service/state change on connected charger."""
        if cp_id in self.charge_points:
            if service_name == SERVICE_AVAILABILITY:
                resp = await self.charge_points[cp_id].set_availability(state)
            if service_name == SERVICE_CHARGE_START:
                resp = await self.charge_points[cp_id].start_transaction()
            if service_name == SERVICE_CHARGE_STOP:
                resp = await self.charge_points[cp_id].stop_transaction()
            if service_name == SERVICE_RESET:
                resp = await self.charge_points[cp_id].reset()
            if service_name == SERVICE_UNLOCK:
                resp = await self.charge_points[cp_id].unlock()
        else:
            resp = False
        return resp

    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.id)},
        }


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(
        self,
        id: str,
        connection,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystem,
        interval_meter_metrics: int = 10,
    ):
        """Instantiate instance of a ChargePoint."""
        super().__init__(id, connection)
        self.interval_meter_metrics = interval_meter_metrics
        self.hass = hass
        self.entry = entry
        self.central = central
        self.status = "init"
        # Indicates if the charger requires a reboot to apply new
        # configuration.
        self._requires_reboot = False
        self._metrics = {}
        self._units = {}
        self._features_supported = {}
        self.preparing = asyncio.Event()
        self._transactionId = 0
        self._metrics["ID"] = id
        self._units["Session.Time"] = TIME_MINUTES
        self._units["Session.Energy"] = UnitOfMeasure.kwh.value
        self._units["Meter.Start"] = UnitOfMeasure.kwh.value

    async def post_connect(self):
        """Logic to be executed right after a charger connects."""
        try:
            await self.get_supported_features()
            if FEATURE_PROFILE_REMOTE in self._features_supported:
                await self.trigger_boot_notification()
                await self.trigger_status_notification()
            await self.become_operative()
            await self.get_configuration("HeartbeatInterval")
            await self.configure("WebSocketPingInterval", "60")
            await self.configure(
                "MeterValuesSampledData",
                self.entry.data[CONF_MONITORED_VARIABLES],
            )
            await self.configure(
                "MeterValueSampleInterval", str(self.entry.data[CONF_METER_INTERVAL])
            )
            #            await self.configure(
            #                "StopTxnSampledData", ",".join(self.entry.data[CONF_MONITORED_VARIABLES])
            #            )
            resp = await self.get_configuration("NumberOfConnectors")
            self._metrics["Connectors"] = resp.configuration_key[0]["value"]
            #            await self.start_transaction()
        except (NotImplementedError) as e:
            _LOGGER.error("Configuration of the charger failed: %s", e)

    async def get_supported_features(self):
        """Get supported features."""
        req = call.GetConfigurationPayload(key=["SupportedFeatureProfiles"])
        resp = await self.call(req)
        for key_value in resp.configuration_key:
            self._features_supported = key_value["value"]
            self._metrics["Features"] = self._features_supported
            _LOGGER.debug("SupportedFeatureProfiles: %s", self._features_supported)

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        req = call.TriggerMessagePayload(requested_message="BootNotification")
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def trigger_status_notification(self):
        """Trigger a status notification."""
        req = call.TriggerMessagePayload(requested_message="StatusNotification")
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def become_operative(self):
        """Become operative."""
        resp = await self.set_availability()
        return resp

    async def clear_profile(self):
        """Clear profile."""
        req = call.ClearChargingProfilePayload()
        resp = await self.call(req)
        if resp.status == ClearChargingProfileStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def set_charge_rate(self, limit_amps: int = 32, limit_watts: int = 22000):
        """Set a charging profile with defined limit."""
        if FEATURE_PROFILE_SMART in self._features_supported:
            resp = await self.get_configuration(
                "ChargingScheduleAllowedChargingRateUnit"
            )
            _LOGGER.debug(
                "Charger supports setting the following units: %s",
                resp.configuration_key[0]["value"],
            )
            _LOGGER.debug("If more than one unit supported default unit is amps")
            if "current" in resp.configuration_key[0]["value"].lower():
                lim = limit_amps
                units = ChargingRateUnitType.amps
            else:
                lim = limit_watts
                units = ChargingRateUnitType.watts
            req = call.SetChargingProfilePayload(
                connector_id=0,
                cs_charging_profiles={
                    "chargingProfileId": 8,
                    "stackLevel": 999,
                    "chargingProfileKind": ChargingProfileKindType.relative,
                    "chargingProfilePurpose": ChargingProfilePurposeType.tx_profile,
                    "chargingSchedule": {
                        "chargingRateUnit": units,
                        "chargingSchedulePeriod": [{"startPeriod": 0, "limit": lim}],
                    },
                },
            )
        else:
            _LOGGER.debug("Smart charging is not supported by this charger")
            return False
        resp = await self.call(req)
        if resp.status == ChargingProfileStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def set_availability(self, state: bool = True):
        """Become operative."""
        """there could be an ongoing transaction. Terminate it"""
        if (state is False) and self._transactionId > 0:
            await self.stop_transaction()
        """ change availability """
        if state is True:
            typ = AvailabilityType.operative
        else:
            typ = AvailabilityType.inoperative

        req = call.ChangeAvailabilityPayload(connector_id=0, type=typ)
        resp = await self.call(req)
        if resp.status == AvailabilityStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def start_transaction(self, limit_amps: int = 32, limit_watts: int = 22000):
        """Start a Transaction."""
        """Check if authorisation enabled, if it is disable it before remote start"""
        resp = await self.get_configuration("AuthorizeRemoteTxRequests")
        if resp.configuration_key[0]["value"].lower() == "true":
            await self.configure("AuthorizeRemoteTxRequests", "false")
        if FEATURE_PROFILE_SMART in self._features_supported:
            resp = await self.get_configuration(
                "ChargingScheduleAllowedChargingRateUnit"
            )
            _LOGGER.debug(
                "Charger supports setting the following units: %s",
                resp.configuration_key[0]["value"],
            )
            _LOGGER.debug("If more than one unit supported default unit is amps")
            if "current" in resp.configuration_key[0]["value"].lower():
                lim = limit_amps
                units = ChargingRateUnitType.amps
            else:
                lim = limit_watts
                units = ChargingRateUnitType.watts
            req = call.RemoteStartTransactionPayload(
                connector_id=1,
                id_tag=self._metrics["ID"],
                charging_profile={
                    "chargingProfileId": 1,
                    "stackLevel": 999,
                    "chargingProfileKind": ChargingProfileKindType.relative,
                    "chargingProfilePurpose": ChargingProfilePurposeType.tx_profile,
                    "chargingSchedule": {
                        "chargingRateUnit": units,
                        "chargingSchedulePeriod": [{"startPeriod": 0, "limit": lim}],
                    },
                },
            )
        else:
            req = call.RemoteStartTransactionPayload(
                connector_id=1, id_tag=self._metrics["ID"]
            )
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def stop_transaction(self):
        """Request remote stop of current transaction."""
        req = call.RemoteStopTransactionPayload(transaction_id=self._transactionId)
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def reset(self, typ: str = ResetType.soft):
        """Soft reset charger unless hard reset requested."""
        req = call.ResetPayload(typ)
        resp = await self.call(req)
        if resp.status == ResetStatus.accepted:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def unlock(self, connector_id: int = 1):
        """Unlock charger if requested."""
        req = call.UnlockConnectorPayload(connector_id)
        resp = await self.call(req)
        if resp.status == UnlockStatus.unlocked:
            return True
        else:
            _LOGGER.debug("Failed with response: %s", resp.status)
            return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available."""
        """where firmware_url is the http or https url of the new firmware"""
        """and wait_time is hours from now to wait before install"""
        if FEATURE_PROFILE_FW in self._features_supported:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(firmware_url)
                raise AssertionError("Multiple invalid not raised")
            except vol.MultipleInvalid as e:
                _LOGGER.debug("Failed to parse url: %s", e)
            update_time = (
                datetime.now(tz=timezone.utc) + timedelta(hours=wait_time)
            ).isoformat()
            req = call.UpdateFirmwarePayload(location=url, retrieve_date=update_time)
            resp = await self.call(req)
            _LOGGER.debug("Response: %s", resp)
            return True
        else:
            _LOGGER.debug("Charger does not support ocpp firmware updating")
            return False

    async def get_configuration(self, key: str = ""):
        """Get Configuration of charger for supported keys."""
        if key == "":
            req = call.GetConfigurationPayload()
        else:
            req = call.GetConfigurationPayload(key=[key])
        resp = await self.call(req)
        for key_value in resp.configuration_key:
            _LOGGER.debug("Get Configuration for %s: %s", key, key_value["value"])
        return resp

    async def configure(self, key: str, value: str):
        """Configure charger by setting the key to target value.

        First the configuration key is read using GetConfiguration. The key's
        value is compared with the target value. If the key is already set to
        the correct value nothing is done.

        If the key has a different value a ChangeConfiguration request is issued.

        """
        req = call.GetConfigurationPayload(key=[key])

        resp = await self.call(req)

        for key_value in resp.configuration_key:
            # If the key already has the targeted value we don't need to set
            # it.
            if key_value["key"] == key and key_value["value"] == value:
                return

            if key_value.get("readonly", False):
                _LOGGER.warning("%s is a read only setting", key)

        req = call.ChangeConfigurationPayload(key=key, value=value)

        resp = await self.call(req)

        if resp.status in [ConfigurationStatus.rejected, "NotSupported"]:
            _LOGGER.warning("%s while setting %s to %s", resp.status, key, value)

        if resp.status == ConfigurationStatus.reboot_required:
            self._requires_reboot = True

    async def _get_specific_response(self, unique_id, timeout):
        # The ocpp library silences CallErrors by default. See
        # https://github.com/mobilityhouse/ocpp/issues/104.
        # This code 'unsilences' CallErrors by raising them as exception
        # upon receiving.
        resp = await super()._get_specific_response(unique_id, timeout)

        if isinstance(resp, CallError):
            raise resp.to_exception()

        return resp

    async def _handle_call(self, msg):
        try:
            await super()._handle_call(msg)
        except NotImplementedError as e:
            response = msg.create_call_error(e).to_json()
            await self._send(response)

    async def start(self):
        """Start charge point."""
        try:
            await asyncio.gather(super().start(), self.post_connect())
        except websockets.exceptions.ConnectionClosed as e:
            _LOGGER.debug(e)

    async def reconnect(self, connection):
        """Reconnect charge point."""
        self._connection = connection
        await self.start()

    @on(Action.MeterValues)
    def on_meter_values(self, connector_id: int, meter_value: Dict, **kwargs):
        """Request handler for MeterValues Calls."""
        for bucket in meter_value:
            for sampled_value in bucket["sampled_value"]:
                if "measurand" in sampled_value:
                    self._metrics[sampled_value["measurand"]] = sampled_value["value"]
                    self._metrics[sampled_value["measurand"]] = round(
                        float(self._metrics[sampled_value["measurand"]]), 1
                    )
                    if "unit" in sampled_value:
                        self._units[sampled_value["measurand"]] = sampled_value["unit"]
                        if (
                            self._units[sampled_value["measurand"]]
                            == DEFAULT_POWER_UNIT
                        ):
                            self._metrics[sampled_value["measurand"]] = (
                                float(self._metrics[sampled_value["measurand"]]) / 1000
                            )
                            self._units[sampled_value["measurand"]] = HA_POWER_UNIT
                        if (
                            self._units[sampled_value["measurand"]]
                            == DEFAULT_ENERGY_UNIT
                        ):
                            self._metrics[sampled_value["measurand"]] = (
                                float(self._metrics[sampled_value["measurand"]]) / 1000
                            )
                            self._units[sampled_value["measurand"]] = HA_ENERGY_UNIT
                if len(sampled_value.keys()) == 1:  # for backwards compatibility
                    self._metrics[DEFAULT_MEASURAND] = sampled_value["value"]
                    self._units[DEFAULT_MEASURAND] = DEFAULT_ENERGY_UNIT
        if "Meter.Start" not in self._metrics:
            self._metrics["Meter.Start"] = self._metrics[DEFAULT_MEASURAND]
        if "Transaction.Id" not in self._metrics:
            self._metrics["Transaction.Id"] = kwargs.get("transaction_id")
            self._transactionId = kwargs.get("transaction_id")
        self._metrics["Session.Time"] = round(
            (int(time.time()) - float(self._metrics["Transaction.Id"])) / 60
        )
        self._metrics["Session.Energy"] = round(
            float(self._metrics[DEFAULT_MEASURAND])
            - float(self._metrics["Meter.Start"]),
            1,
        )
        return call_result.MeterValuesPayload()

    async def async_update_device_info(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.id, boot_info)

        dr = await device_registry.async_get_registry(self.hass)

        serial = boot_info.get("charge_point_serial_number", None)

        identifiers = {(DOMAIN, self.id)}
        if serial is not None:
            identifiers.add((DOMAIN, serial))

        dr.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=identifiers,
            name=self.id,
            manufacturer=boot_info.get("charge_point_vendor", None),
            model=boot_info.get("charge_point_model", None),
            sw_version=boot_info.get("firmware_version", None),
        )

    @on(Action.BootNotification)
    def on_boot_notification(self, **kwargs):
        """Handle a boot notification."""

        _LOGGER.debug("Received boot notification for %s: %s", self.id, kwargs)

        # update metrics
        self._metrics["Model"] = kwargs.get("charge_point_model", None)
        self._metrics["Vendor"] = kwargs.get("charge_point_vendor", None)
        self._metrics["FW.Version"] = kwargs.get("firmware_version", None)
        self._metrics["Serial"] = kwargs.get("charge_point_serial_number", None)

        asyncio.create_task(self.async_update_device_info(kwargs))

        return call_result.BootNotificationPayload(
            current_time=datetime.now(tz=timezone.utc).isoformat(),
            interval=30,
            status=RegistrationStatus.accepted,
        )

    @on(Action.StatusNotification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Handle a status notification."""
        self._metrics["Status"] = status
        if (
            status == ChargePointStatus.suspended_ev
            or status == ChargePointStatus.suspended_evse
        ):
            if Measurand.current_import in self._metrics:
                self._metrics[Measurand.current_import] = 0
            if Measurand.power_active_import in self._metrics:
                self._metrics[Measurand.power_active_import] = 0
            if Measurand.power_reactive_import in self._metrics:
                self._metrics[Measurand.power_reactive_import] = 0
        self._metrics["Error.Code"] = error_code
        return call_result.StatusNotificationPayload()

    @on(Action.FirmwareStatusNotification)
    def on_firmware_status(self, fwstatus, **kwargs):
        """Handle formware status notification."""
        self._metrics["FW.Status"] = fwstatus
        return call_result.FirmwareStatusNotificationPayload()

    @on(Action.Authorize)
    def on_authorize(self, id_tag, **kwargs):
        """Handle a Authorization request."""
        return call_result.AuthorizePayload(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on(Action.StartTransaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        """Handle a Start Transaction request."""
        self._transactionId = int(time.time())
        self._metrics["Stop.Reason"] = ""
        self._metrics["Transaction.Id"] = self._transactionId
        self._metrics["Meter.Start"] = int(meter_start) / 1000
        return call_result.StartTransactionPayload(
            id_tag_info={"status": AuthorizationStatus.accepted},
            transaction_id=self._transactionId,
        )

    @on(Action.StopTransaction)
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        """Stop the current transaction."""
        self._metrics["Stop.Reason"] = kwargs.get("reason", None)

        if "Meter.Start" in self._metrics:
            self._metrics["Session.Energy"] = round(
                int(meter_stop) / 1000 - float(self._metrics["Meter.Start"]), 1
            )
        if Measurand.current_import in self._metrics:
            self._metrics[Measurand.current_import] = 0
        if Measurand.power_active_import in self._metrics:
            self._metrics[Measurand.power_active_import] = 0
        if Measurand.power_reactive_import in self._metrics:
            self._metrics[Measurand.power_reactive_import] = 0
        return call_result.StopTransactionPayload(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on(Action.DataTransfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        """Handle a Data transfer request."""
        _LOGGER.debug("Datatransfer received from %s: %s", self.id, kwargs)
        return call_result.DataTransferPayload(status=DataTransferStatus.accepted)

    @on(Action.Heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle a Heartbeat."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._metrics["Heartbeat"] = now
        self._units["Heartbeat"] = "time"
        return call_result.HeartbeatPayload(current_time=now)

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        return self._metrics.get(measurand, None)

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        return self._units.get(measurand, None)
