"""Representation of a OCCP Charge Point."""
import asyncio
from datetime import datetime
import logging
import time
from typing import Dict

from homeassistant.const import TIME_MINUTES
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
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnitOfMeasure,
)

from .const import (
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_MEASURAND,
    DEFAULT_POWER_UNIT,
    DOMAIN,
    FEATURE_PROFILE_REMOTE,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    SLEEP_TIME,
)
# from .exception import ConfigurationError

_LOGGER = logging.getLogger(__name__)
logging.getLogger(DOMAIN).setLevel(logging.DEBUG)


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(self, id, connection, config, interval_meter_metrics: int = 10):
        """Instantiate instance of a ChargePoint."""
        super().__init__(id, connection)
        self.interval_meter_metrics = interval_meter_metrics
        self.config = config
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
        self._units["Session.Energy"] = UnitOfMeasure.kwh
        self._units["Meter.Start"] = UnitOfMeasure.kwh

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
                self.config[CONF_MONITORED_VARIABLES],
            )
            await self.configure(
                "MeterValueSampleInterval", str(self.config[CONF_METER_INTERVAL])
            )
            #            await self.configure(
            #                "StopTxnSampledData", ",".join(self.config[CONF_MONITORED_VARIABLES])
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
        while True:
            req = call.TriggerMessagePayload(requested_message="BootNotification")
            resp = await self.call(req)
            if resp.status == TriggerMessageStatus.accepted:
                break
            if resp.status == TriggerMessageStatus.not_implemented:
                break
            if resp.status == TriggerMessageStatus.rejected:
                break
            await asyncio.sleep(SLEEP_TIME)

    async def trigger_status_notification(self):
        """Trigger a status notification."""
        while True:
            req = call.TriggerMessagePayload(requested_message="StatusNotification")
            resp = await self.call(req)
            if resp.status == TriggerMessageStatus.accepted:
                break
            await asyncio.sleep(SLEEP_TIME)

    async def become_operative(self):
        """Become operative."""
        while True:
            """there could be an ongoing transaction. Terminate it"""
            #            req = call.RemoteStopTransactionPayload(transaction_id=1234)
            #            resp = await self.call(req)
            """ change availability """
            req = call.ChangeAvailabilityPayload(
                connector_id=0, type=AvailabilityType.operative
            )
            resp = await self.call(req)
            if resp.status == AvailabilityStatus.accepted:
                break
            if resp.status == AvailabilityStatus.scheduled:
                break
            await asyncio.sleep(SLEEP_TIME)

    async def clear_profile(self):
        """Clear profile."""
        while True:
            req = call.ClearChargingProfilePayload()
            resp = await self.call(req)
            if resp.status == ClearChargingProfileStatus.accepted:
                break
            await asyncio.sleep(SLEEP_TIME)

    async def start_transaction(self, limit: int = 22000):
        """Start a Transaction."""
        while True:
            req = call.RemoteStartTransactionPayload(
                connector_id=1,
                id_tag="ID4",
                charging_profile={
                    "chargingProfileId": 1,
                    "stackLevel": 999,
                    "chargingProfileKind": "Relative",
                    "chargingProfilePurpose": "TxProfile",
                    "chargingSchedule": {
                        "duration": 36000,
                        "chargingRateUnit": "W",
                        "chargingSchedulePeriod": [
                            {"startPeriod": 0, "limit": {limit}}
                        ],
                    },
                },
            )
            resp = await self.call(req)
            if resp.status == RemoteStartStopStatus.accepted:
                break
            await asyncio.sleep(SLEEP_TIME)

    async def stop_transaction(self):
        """Request remote stop of current transaction."""
        while True:
            req = call.RemoteStopTransactionPayload(transactionId=self._transactionId)
            resp = await self.call(req)
            if resp.status == RemoteStartStopStatus.accepted:
                break
            await asyncio.sleep(SLEEP_TIME)

    async def reset(self, typ: str = ResetType.soft):
        """Soft reset charger unless hard reset requested."""
        while True:
            req = call.ResetPayload(typ)
            resp = await self.call(req)
            if resp.status == ResetStatus.accepted:
                break
            await asyncio.sleep(SLEEP_TIME)

    async def get_configuration(self, key: str):
        """Get Configuration of charger for supported keys."""
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
            return self._metrics

    async def reconnect(self, last_metrics):
        """Reconnect charge point."""
        try:
            self._metrics = last_metrics
            await asyncio.gather(super().start())
        except websockets.exceptions.ConnectionClosed as e:
            _LOGGER.debug(e)
            return self._metrics

    @on(Action.MeterValues)
    def on_meter_values(self, connector_id: int, meter_value: Dict, **kwargs):
        """Request handler for MeterValues Calls."""
        for bucket in meter_value:
            for sampled_value in bucket["sampled_value"]:
                if "measurand" in sampled_value:
                    self._metrics[sampled_value["measurand"]] = sampled_value["value"]
                if len(sampled_value.keys()) == 1:  # for backwards compatibility
                    self._metrics[DEFAULT_MEASURAND] = sampled_value["value"]
                    self._units[DEFAULT_MEASURAND] = DEFAULT_ENERGY_UNIT
                if "unit" in sampled_value:
                    self._units[sampled_value["measurand"]] = sampled_value["unit"]
                    if self._units[sampled_value["measurand"]] == DEFAULT_POWER_UNIT:
                        self._metrics[sampled_value["measurand"]] = (
                            float(self._metrics[sampled_value["measurand"]]) / 1000
                        )
                        self._units[sampled_value["measurand"]] = HA_POWER_UNIT
                    if self._units[sampled_value["measurand"]] == DEFAULT_ENERGY_UNIT:
                        self._metrics[sampled_value["measurand"]] = (
                            float(self._metrics[sampled_value["measurand"]]) / 1000
                        )
                        self._units[sampled_value["measurand"]] = HA_ENERGY_UNIT
                self._metrics[sampled_value["measurand"]] = round(
                    float(self._metrics[sampled_value["measurand"]]), 1
                )
        if "Meter.Start" not in self._metrics:
            self._metrics["Meter.Start"] = self._metrics[DEFAULT_MEASURAND]
        if "Transaction.Id" not in self._metrics:
            self._metrics["Transaction.Id"] = kwargs.get("transaction_id")
        self._metrics["Session.Time"] = round(
            (int(time.time()) - float(self._metrics["Transaction.Id"])) / 60
        )
        self._metrics["Session.Energy"] = round(
            float(self._metrics[DEFAULT_MEASURAND])
            - float(self._metrics["Meter.Start"]),
            1,
        )
        return call_result.MeterValuesPayload()

    @on(Action.BootNotification)
    def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        """Handle a boot notification."""
        self._metrics["Model"] = charge_point_model
        self._metrics["Vendor"] = charge_point_vendor
        self._metrics["FW.Version"] = kwargs.get("firmware_version")
        self._metrics["Serial"] = kwargs.get("charge_point_serial_number")
        _LOGGER.debug("Additional boot info for %s: %s", self.id, kwargs)
        return call_result.BootNotificationPayload(
            current_time=datetime.utcnow().isoformat(),
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
            if "Current.Import" in self._metrics:
                self._metrics["Current.Import"] = 0
            if "Power.Active.Import" in self._metrics:
                self._metrics["Power.Active.Import"] = 0
            if "Power.Reactive.Import" in self._metrics:
                self._metrics["Power.Reactive.Import"] = 0
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
    def on_stop_transaction(self, meter_stop, transaction_id, reason, **kwargs):
        """Stop the current transaction."""
        self._metrics["Stop.Reason"] = reason
        if "Meter.Start" in self._metrics:
            self._metrics["Session.Energy"] = round(
                int(meter_stop) / 1000 - float(self._metrics["Meter.Start"]), 1
            )
        if "Current.Import" in self._metrics:
            self._metrics["Current.Import"] = 0
        if "Power.Active.Import" in self._metrics:
            self._metrics["Power.Active.Import"] = 0
        if "Power.Reactive.Import" in self._metrics:
            self._metrics["Power.Reactive.Import"] = 0
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
        now = datetime.utcnow().isoformat()
        self._metrics["Heartbeat"] = now
        self._units["Heartbeat"] = "time"
        return call_result.HeartbeatPayload(current_time=now)

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        return self._metrics.get(measurand, None)

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        return self._units.get(measurand, None)
