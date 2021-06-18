"""Open Charge Point Protocol integration."""
import asyncio, time
from datetime import datetime, timedelta
import logging
from typing import Dict, Optional

from ocpp.exceptions import NotImplementedError
from ocpp.messages import CallError
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp, call, call_result
from ocpp.v16.enums import *
import voluptuous as vol
import websockets

from homeassistant.core import ServiceCall, callback
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_MONITORED_VARIABLES, CONF_PORT, CONF_NAME, CONF_MONITORED_CONDITIONS
from homeassistant.helpers import config_validation as cv, entity_platform, service
from homeassistant.helpers.entity import Entity
#from homeassistant.helpers.entity_platform import AddEntitiesCallback

DOMAIN = "ocpp"
LOCAL_HOST = "0.0.0.0"
DEFAULT_PORT = 9000
DEFAULT_SUBPROTOCOL = "ocpp1.6"

_LOGGER = logging.getLogger(__name__)
logging.getLogger(DOMAIN).setLevel(logging.DEBUG)

ICON = "mdi:ev-station"

SLEEP_TIME = 60

SCAN_INTERVAL = timedelta(seconds=1)

#Ocpp SupportedFeatureProfiles
FEATURE_PROFILE_CORE = "Core"
FEATURE_PROFILE_FW = "FirmwareManagement"
FEATURE_PROFILE_SMART = "SmartCharging"
FEATURE_PROFILE_RESERV = "Reservation"
FEATURE_PROFILE_REMOTE = "RemoteTrigger"
FEATURE_PROFILE_AUTH = "LocalAuthListManagement"

#Services to register for use in HA
SERVICE_CHARGE_START = "start_transaction"
SERVICE_CHARGE_STOP = "stop_transaction"
SERVICE_AVAILABILITY = "availability"
SERVICE_SET_CHARGE_RATE = "max_charge_rate"
SERVICE_RESET = "reset"

#Ocpp supported measurands
MEASURANDS = [
    "Current.Export",
    "Current.Import",
    "Current.Offered",
    "Energy.Active.Export.Register",
    "Energy.Active.Import.Register",
    "Energy.Reactive.Export.Register",
    "Energy.Reactive.Import.Register",
    "Energy.Active.Export.Interval",
    "Energy.Active.Import.Interval",
    "Energy.Reactive.Export.Interval",
    "Energy.Reactive.Import.Interval",
    "Frequency",
    "Power.Active.Export",
    "Power.Active.Import",
    "Power.Factor",
    "Power.Offered",
    "Power.Reactive.Export",
    "Power.Reactive.Import",
    "RPM",
    "SoC",
    "Temperature",
    "Voltage"
    ]
DEFAULT_MEASURAND = "Energy.Active.Import.Register"

#Additional conditions/states to monitor
CONDITIONS = [
    "Status",
    "Heartbeat",
    "Error.Code",
    "Stop.Reason",
    "FW.Status",
    "Session.Energy", #in kWh
    "Meter.Start" #in kWh
]

#Additional general information to report
GENERAL = [
    "ID",
    "Vendor",
    "Model",
    "FW.Version",
    "Features",
    "Connectors",
    "Transaction.Id"
]

#Interval between measurand meters being sent
CONF_METER_INTERVAL = "meter_interval"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.positive_int,
        vol.Optional(CONF_MONITORED_VARIABLES, default = MEASURANDS): vol.All(
            cv.ensure_list, [vol.In(MEASURANDS)]
        ),
        vol.Optional(CONF_NAME, default="ocpp"): cv.string,
        vol.Optional(CONF_METER_INTERVAL, default=300): cv.positive_int
    },
)

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the OCPP sensors & make config available globally."""
    global yaml_config
    yaml_config = config
    
    cfg_port = config[CONF_PORT]
    _LOGGER.info(config)

    central_sys = await CentralSystem.create(port=cfg_port)

    metric = []
    for measurand in config[CONF_MONITORED_VARIABLES]:
        metric.append(ChargePointMetric(measurand, central_sys, "M", config[CONF_NAME]))
    for condition in CONDITIONS:
        metric.append(ChargePointMetric(condition, central_sys, "S", config[CONF_NAME]))
    for gen in GENERAL:
        metric.append(ChargePointMetric(gen, central_sys, "G", config[CONF_NAME]))
        
    async_add_entities(metric, True)
    """Set up services."""
#    platform = entity_platform.async_get_current_platform()
#    platform.async_register_entity_service(SERVICE_CHARGE_STOP, {}, "stop_charge")

class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self):
        """Instantiate instance of a CentralSystem."""
        self._server = None
        self._connected_charger: Optional[ChargePoint] = None
        self._last_connected_id = ""
        self._cp_metrics = {}

    @staticmethod
    async def create(host: str = LOCAL_HOST, port: int = DEFAULT_PORT, proto: str = DEFAULT_SUBPROTOCOL):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem()
        server = await websockets.serve(
            self.on_connect, host, port, subprotocols=[proto]
        )

        self._server = server
        return self

    async def on_connect(self, websocket, path: str):
        """Request handler executed for every new OCPP connection."""
        # For now only 1 charger can connect.
        cp_id = path.strip("/")
        if self._connected_charger is not None:
            return

        try:
            cp = ChargePoint(cp_id, websocket)
            self._connected_charger = cp
            if self._last_connected_id == cp.id:
                _LOGGER.debug(f"Charger {cp_id} reconnected.")
                self._cp_metrics = await cp.reconnect(self._cp_metrics)
            else:
                _LOGGER.info(f"Charger {cp_id} connected.")
                self._last_connected_id = cp.id
                self._cp_metrics = await cp.start()
        finally:
            self._connected_charger = None

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        if self._connected_charger is not None:
            return self._connected_charger.get_metric(measurand)
        return None

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        if self._connected_charger is not None:
            return self._connected_charger.get_unit(measurand)
        return None


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(self, id, connection, interval_meter_metrics: int = 10):
        """Instantiate instance of a ChargePoint."""
        super().__init__(id, connection)
        self.interval_meter_metrics = interval_meter_metrics
        self.status="init"
        # Indicates if the charger requires a reboot to apply new
        # configuration.
        self._requires_reboot = False
        self._metrics = {}
        self._units = {}
        self._features_supported = {}
        self.preparing = asyncio.Event()
        self._transactionId = 0
        self._metrics["ID"] = id
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
            await self.get_configure("HeartbeatInterval")
            await self.configure("WebSocketPingInterval", "60")
            await self.configure(
                "MeterValuesSampledData", ",".join(yaml_config[CONF_MONITORED_VARIABLES])
            )
            await self.configure(
                "MeterValueSampleInterval", str(yaml_config[CONF_METER_INTERVAL])
            )
#            await self.configure(
#                "StopTxnSampledData", ",".join(yaml_config[CONF_MONITORED_VARIABLES])
#            )
            resp = await self.get_configure("NumberOfConnectors")
            self._metrics["Connectors"] = resp.configuration_key[0]["value"]
#            await self.start_transaction()
        except (ConfigurationError, NotImplementedError) as e:
            _LOGGER.error("Configuration of the charger failed: %r", e)

    async def get_supported_features(self):
        req = call.GetConfigurationPayload(key=["SupportedFeatureProfiles"])
        resp = await self.call(req)
        for key_value in resp.configuration_key:
            self._features_supported = key_value["value"]
            self._metrics["Features"] = self._features_supported
            _LOGGER.debug("SupportedFeatureProfiles: %r",self._features_supported)

    async def trigger_boot_notification(self):
        while True:
            req = call.TriggerMessagePayload(requested_message="BootNotification")
            resp = await self.call(req)
            if resp.status == TriggerMessageStatus.accepted: break
            if resp.status == TriggerMessageStatus.not_implemented: break      
            if resp.status == TriggerMessageStatus.rejected: break                        
            await asyncio.sleep(SLEEP_TIME)

    async def trigger_status_notification(self):
        while True:
            req = call.TriggerMessagePayload(requested_message="StatusNotification")
            resp = await self.call(req)
            if resp.status == TriggerMessageStatus.accepted: break
            await asyncio.sleep(SLEEP_TIME)
    
    async def become_operative(self):
        while True:
            """ there could be an ongoing transaction. Terminate it """            
#            req = call.RemoteStopTransactionPayload(transaction_id=1234)
#            resp = await self.call(req) 
            """ change availability """
            req = call.ChangeAvailabilityPayload(connector_id=0, type=AvailabilityType.operative)
            resp = await self.call(req)
            if resp.status == AvailabilityStatus.accepted: break
            if resp.status == AvailabilityStatus.scheduled: break
            await asyncio.sleep(SLEEP_TIME)
    
    async def clear_profile(self):
        while True:
            req = call.ClearChargingProfilePayload()            
            resp = await self.call(req)
            if resp.status == ClearChargingProfileStatus.accepted: break;            
            await asyncio.sleep(SLEEP_TIME)

    async def start_transaction(self, limit: int = 22000):
        while True:
            req = call.RemoteStartTransactionPayload(
                connector_id=1, id_tag="ID4",
                charging_profile = {
                    "chargingProfileId" : 1,
                    "stackLevel" : 999,
                    "chargingProfileKind" : "Relative",    
                    "chargingProfilePurpose" : "TxProfile",
                    "chargingSchedule" : {
                        "duration" : 36000,
                        "chargingRateUnit" : "W",
                        "chargingSchedulePeriod" : [ 
                            {
                                "startPeriod" : 0,
                                "limit" : {limit}
                            }
                        ]
                    }
                }
            )
            resp = await self.call(req)
            if resp.status == RemoteStartStopStatus.accepted: break
            await asyncio.sleep(SLEEP_TIME)

    async def stop_transaction(self):
        """Request remote stop of current transaction."""
        while True:
            req = call.RemoteStopTransactionPayload(
                transactionId=self._transactionId
            )
            resp = await self.call(req)
            if resp.status == RemoteStartStopStatus.accepted: break
            await asyncio.sleep(SLEEP_TIME)

    async def reset(self, typ: str = ResetType.soft):
        """Soft reset charger unless hard reset requested."""
        while True:
            req = call.ResetPayload(typ)
            resp = await self.call(req)
            if resp.status == ResetStatus.accepted: break
            await asyncio.sleep(SLEEP_TIME)

    async def get_configure(self, key: str):
        """Get Configuration of charger for supported keys."""
        req = call.GetConfigurationPayload(key=[key])
        resp = await self.call(req)
        for key_value in resp.configuration_key:
            _LOGGER.debug("Get Configuration for %s: %s",key, key_value["value"])
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
                raise ConfigurationError(f"'{key}' is a read only setting")

        req = call.ChangeConfigurationPayload(key=key, value=value)

        resp = await self.call(req)

        if resp.status in [ConfigurationStatus.rejected, "NotSupported"]:
            raise ConfigurationError(
                f"charger returned '{resp.status}' while setting '{key}' to '{value}'"
            )

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
        """Start charge point"""
        try:
            await asyncio.gather(super().start(), self.post_connect())
        except websockets.exceptions.ConnectionClosed as e:
            _LOGGER.debug(e)
            return self._metrics
        
    async def reconnect(self, last_metrics):
        """Start charge point."""
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
                if ("measurand" in sampled_value):
                    self._metrics[sampled_value["measurand"]] = sampled_value["value"]
                if (len(sampled_value.keys()) == 1): #for backwards compatibility
                    self._metrics[DEFAULT_MEASURAND] = sampled_value["value"]
                if ("unit" in sampled_value):
                    self._units[sampled_value["measurand"]] = sampled_value["unit"]
        if ("Meter.Start" not in self._metrics): self._metrics["Meter.Start"] = self._metrics[DEFAULT_MEASURAND]
        self._metrics["Session.Energy"] = round(float(self._metrics[DEFAULT_MEASURAND]) - float(self._metrics["Meter.Start"]), 1)
        return call_result.MeterValuesPayload()

    @on(Action.BootNotification)
    def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        self._metrics["Model"] = charge_point_model
        self._metrics["Vendor"] = charge_point_vendor
        self._metrics["FW.Version"] = kwargs.get("firmware_version")
        _LOGGER.debug("Additional boot info for %s: %s", self.id, kwargs)
        return call_result.BootNotificationPayload(
            current_time=datetime.utcnow().isoformat(),
            interval=30,
            status=RegistrationStatus.accepted
        )

    @on(Action.StatusNotification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        self._metrics["Status"] = status
        self._metrics["Error.Code"] = error_code
        return call_result.StatusNotificationPayload()

    @on(Action.FirmwareStatusNotification)
    def on_firmware_status(self, fwstatus, **kwargs):
        self._metrics["FW.Status"] = fwstatus
        return call_result.FirmwareStatusNotificationPayload()

    @on(Action.Authorize)
    def on_authorize(self, id_tag, **kwargs):
        return call_result.AuthorizePayload(
            id_tag_info = { "status" : AuthorizationStatus.accepted }
        )

    @on(Action.StartTransaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs): 
        self._transactionId = int(time.time())
        self._metrics["Stop.Reason"] = ""
        self._metrics["Transaction.Id"] = self._transactionId
        self._metrics["Meter.Start"] = int(meter_start) / 1000.0
        return call_result.StartTransactionPayload(
            id_tag_info = { "status" : AuthorizationStatus.accepted },
            transaction_id = self._transactionId
        )
    
    @on(Action.StopTransaction)
    def on_stop_transaction(self, meter_stop, transaction_id, reason, **kwargs):
        self._metrics["Stop.Reason"] = reason
        self._metrics["Session.Energy"] = round(int(meter_stop)/1000.0 - self._metrics["Meter.Start"], 1)
        return call_result.StopTransactionPayload(
            id_tag_info = { "status" : AuthorizationStatus.accepted }            
        )
    
    @on(Action.DataTransfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        _LOGGER.debug("Datatransfer received from %s: %s", self.id, kwargs)
        return call_result.DataTransferPayload(
            status = DataTransferStatus.accepted 
        )
    
    @on(Action.Heartbeat)
    def on_heartbeat(self, **kwargs):
        now = datetime.utcnow().isoformat()
        self._metrics["Heartbeat"] = now
        self._units["Heartbeat"] = "time"        
        return call_result.HeartbeatPayload(
            current_time=now
        )

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        return self._metrics.get(measurand, None)

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        return self._units.get(measurand, None)


class ChargePointMetric(Entity):
    """Individual sensor for charge point metrics."""

    def __init__(self, metric, central_sys, genre, prefix):
        """Instantiate instance of a ChargePointMetrics."""
        self.metric = metric
        self.central_sys = central_sys
        self._genre = genre
        self.prefix = prefix
        self._state = None
        self.type = "connected_chargers"

    @property
    def name(self):
        """Return the name of the sensor."""
        return self.prefix + "." + self.metric

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.central_sys.get_metric(self.metric)

    @property
    def genre(self):
        """Return the type of sensor "M"=measurand "S"=status "G"= general info."""
        return self._genre

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self.central_sys.get_unit(self.metric)

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return ICON

    def update(self):
        """Get the latest data and updates the states."""
        pass


class ConfigurationError(Exception):
    """Error used to signal a error while configuring the charger."""

    pass
