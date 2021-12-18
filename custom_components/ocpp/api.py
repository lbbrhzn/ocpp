"""Representation of a OCCP Entities."""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
from math import sqrt
import time
from typing import Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OK, STATE_UNAVAILABLE, TIME_MINUTES
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_component, entity_registry
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
import websockets.server

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
    MessageTrigger,
    Phase,
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
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    UNITS_OCCP_TO_HA,
)
from .enums import (
    ConfigurationKey as ckey,
    HAChargerDetails as cdet,
    HAChargerServices as csvcs,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    OcppMisc as om,
    Profiles as prof,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.DEBUG)
# Uncomment these when Debugging
# logging.getLogger("asyncio").setLevel(logging.DEBUG)
# logging.getLogger("websockets").setLevel(logging.DEBUG)

UFW_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("firmware_url"): cv.string,
        vol.Optional("delay_hours"): cv.positive_int,
    }
)
CONF_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("ocpp_key"): cv.string,
        vol.Required("value"): cv.string,
    }
)
GCONF_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("ocpp_key"): cv.string,
    }
)
GDIAG_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("upload_url"): cv.string,
    }
)
TRANS_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("vendor_id"): cv.string,
        vol.Optional("message_id"): cv.string,
        vol.Optional("data"): cv.string,
    }
)


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

        server = await websockets.server.serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=[self.subprotocol],
            ping_timeout=None,
            close_timeout=10,
        )
        self._server = server
        return self

    async def on_connect(self, websocket, path: str):
        """Request handler executed for every new OCPP connection."""
        try:
            requested_protocols = websocket.request_headers["Sec-WebSocket-Protocol"]
        except KeyError:
            _LOGGER.error("Client hasn't requested any Subprotocol. Closing connection")
            return await websocket.close()
        if requested_protocols in websocket.available_subprotocols:
            _LOGGER.info("Websocket Subprotocol matched: %s", requested_protocols)
        else:
            # In the websockets lib if no subprotocols are supported by the
            # client and the server, it proceeds without a subprotocol,
            # so we have to manually close the connection.
            _LOGGER.warning(
                "Protocols mismatched | expected Subprotocols: %s,"
                " but client supports  %s | Closing connection",
                websocket.available_subprotocols,
                requested_protocols,
            )
            return await websocket.close()

        _LOGGER.info(f"Charger websocket path={path}")
        cp_id = path.strip("/")
        cp_id = cp_id[cp_id.rfind("/") + 1 :]
        try:
            if self.cpid not in self.charge_points:
                _LOGGER.info(f"Charger {cp_id} connected to {self.host}:{self.port}.")
                charge_point = ChargePoint(
                    cp_id, websocket, self.hass, self.entry, self
                )
                self.charge_points[self.cpid] = charge_point
                await charge_point.start()
            else:
                _LOGGER.info(f"Charger {cp_id} reconnected to {self.host}:{self.port}.")
                charge_point: ChargePoint = self.charge_points[self.cpid]
                await charge_point.reconnect(websocket)
        except Exception as e:
            _LOGGER.error(f"Exception occurred:\n{e}", exc_info=True)
        finally:
            _LOGGER.info(f"Charger {cp_id} disconnected from {self.host}:{self.port}.")

    def get_metric(self, cp_id: str, measurand: str):
        """Return last known value for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].value
        return None

    def get_unit(self, cp_id: str, measurand: str):
        """Return unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].unit
        return None

    def get_ha_unit(self, cp_id: str, measurand: str):
        """Return home assistant unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].ha_unit
        return None

    def get_extra_attr(self, cp_id: str, measurand: str):
        """Return last known extra attributes for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].extra_attr
        return None

    def get_available(self, cp_id: str):
        """Return whether the charger is available."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].status == STATE_OK
        return False

    def get_supported_features(self, cp_id: str):
        """Return what profiles the charger supports."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].supported_features
        return 0

    async def set_max_charge_rate_amps(self, cp_id: str, value: float):
        """Set the maximum charge rate in amps."""
        if cp_id in self.charge_points:
            return await self.charge_points[cp_id].set_charge_rate(limit_amps=value)
        return False

    async def set_charger_state(
        self, cp_id: str, service_name: str, state: bool = True
    ):
        """Carry out requested service/state change on connected charger."""
        if cp_id in self.charge_points:
            if service_name == csvcs.service_availability.name:
                resp = await self.charge_points[cp_id].set_availability(state)
            if service_name == csvcs.service_charge_start.name:
                resp = await self.charge_points[cp_id].start_transaction()
            if service_name == csvcs.service_charge_stop.name:
                resp = await self.charge_points[cp_id].stop_transaction()
            if service_name == csvcs.service_reset.name:
                resp = await self.charge_points[cp_id].reset()
            if service_name == csvcs.service_unlock.name:
                resp = await self.charge_points[cp_id].unlock()
        else:
            resp = False
        return resp

    async def update(self, cp_id: str):
        """Update sensors values in HA."""
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)
        identifiers = {(DOMAIN, cp_id)}
        dev = dr.async_get_device(identifiers)
        # _LOGGER.info("Device id: %s updating", dev.name)
        for ent in entity_registry.async_entries_for_device(er, dev.id):
            # _LOGGER.info("Entity id: %s updating", ent.entity_id)
            self.hass.async_create_task(
                entity_component.async_update_entity(self.hass, ent.entity_id)
            )

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
        self.preparing = asyncio.Event()
        self._transactionId = 0
        self._metrics = defaultdict(lambda: Metric(None, None))
        self._metrics[cdet.identifier.value].value = id
        self._metrics[csess.session_time.value].unit = TIME_MINUTES
        self._metrics[csess.session_energy.value].unit = UnitOfMeasure.kwh.value
        self._metrics[csess.meter_start.value].unit = UnitOfMeasure.kwh.value
        self._attr_supported_features: int = 0
        self._metrics[cstat.reconnects.value].value: int = 0

    async def post_connect(self):
        """Logic to be executed right after a charger connects."""
        # Define custom service handles for charge point
        async def handle_clear_profile(call):
            """Handle the clear profile service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            await self.clear_profile()

        async def handle_update_firmware(call):
            """Handle the firmware update service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            url = call.data.get("firmware_url")
            delay = int(call.data.get("delay_hours", 0))
            await self.update_firmware(url, delay)

        async def handle_configure(call):
            """Handle the configure service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            key = call.data.get("ocpp_key")
            value = call.data.get("value")
            await self.configure(key, value)

        async def handle_get_configuration(call):
            """Handle the get configuration service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            key = call.data.get("ocpp_key")
            await self.get_configuration(key)

        async def handle_get_diagnostics(call):
            """Handle the get get diagnostics service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            url = call.data.get("upload_url")
            await self.get_diagnostics(url)

        async def handle_data_transfer(call):
            """Handle the data transfer service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            vendor = call.data.get("vendor_id")
            message = call.data.get("message_id", "")
            data = call.data.get("data", "")
            await self.data_transfer(vendor, message, data)

        try:
            self.status = STATE_OK
            await asyncio.sleep(2)
            await self.get_supported_features()
            if prof.REM in self._attr_supported_features:
                await self.trigger_status_notification()
            await self.become_operative()
            await self.get_configuration(ckey.heartbeat_interval.value)
            await self.configure(ckey.web_socket_ping_interval.value, "60")
            await self.configure(
                ckey.meter_values_sampled_data.value,
                self.entry.data[CONF_MONITORED_VARIABLES],
            )
            await self.configure(
                ckey.meter_value_sample_interval.value,
                str(self.entry.data[CONF_METER_INTERVAL]),
            )
            #            await self.configure(
            #                "StopTxnSampledData", ",".join(self.entry.data[CONF_MONITORED_VARIABLES])
            #            )
            resp = await self.get_configuration(ckey.number_of_connectors.value)
            self._metrics[cdet.connectors.value].value = resp
            #            await self.start_transaction()

            # Register custom services with home assistant
            self.hass.services.async_register(
                DOMAIN,
                csvcs.service_configure.value,
                handle_configure,
                CONF_SERVICE_DATA_SCHEMA,
            )
            self.hass.services.async_register(
                DOMAIN,
                csvcs.service_get_configuration.value,
                handle_get_configuration,
                GCONF_SERVICE_DATA_SCHEMA,
            )
            self.hass.services.async_register(
                DOMAIN,
                csvcs.service_data_transfer.value,
                handle_data_transfer,
                TRANS_SERVICE_DATA_SCHEMA,
            )
            if prof.SMART in self._attr_supported_features:
                self.hass.services.async_register(
                    DOMAIN, csvcs.service_clear_profile.value, handle_clear_profile
                )
            if prof.FW in self._attr_supported_features:
                self.hass.services.async_register(
                    DOMAIN,
                    csvcs.service_update_firmware.value,
                    handle_update_firmware,
                    UFW_SERVICE_DATA_SCHEMA,
                )
                self.hass.services.async_register(
                    DOMAIN,
                    csvcs.service_get_diagnostics.value,
                    handle_get_diagnostics,
                    GDIAG_SERVICE_DATA_SCHEMA,
                )
        except (NotImplementedError) as e:
            _LOGGER.error("Configuration of the charger failed: %s", e)

    async def get_supported_features(self):
        """Get supported features."""
        req = call.GetConfigurationPayload(key=[ckey.supported_feature_profiles.value])
        resp = await self.call(req)
        for key_value in resp.configuration_key:
            if om.feature_profile_core.value in key_value[om.value.value]:
                self._attr_supported_features |= prof.CORE
            if om.feature_profile_firmware.value in key_value[om.value.value]:
                self._attr_supported_features |= prof.FW
            if om.feature_profile_smart.value in key_value[om.value.value]:
                self._attr_supported_features |= prof.SMART
            if om.feature_profile_reservation.value in key_value[om.value.value]:
                self._attr_supported_features |= prof.RES
            if om.feature_profile_remote.value in key_value[om.value.value]:
                self._attr_supported_features |= prof.REM
            if om.feature_profile_auth.value in key_value[om.value.value]:
                self._attr_supported_features |= prof.AUTH
            self._metrics[cdet.features.value].value = self._attr_supported_features
            _LOGGER.info("Supported feature profiles: %s", key_value[om.value.value])

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        req = call.TriggerMessagePayload(
            requested_message=MessageTrigger.boot_notification
        )
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def trigger_status_notification(self):
        """Trigger a status notification."""
        req = call.TriggerMessagePayload(
            requested_message=MessageTrigger.status_notification
        )
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def become_operative(self):
        """Become operative."""
        resp = await self.set_availability()
        return resp

    async def clear_profile(self):
        """Clear all charging profiles."""
        req = call.ClearChargingProfilePayload()
        resp = await self.call(req)
        if resp.status == ClearChargingProfileStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def set_charge_rate(self, limit_amps: int = 32, limit_watts: int = 22000):
        """Set a charging profile with defined limit."""
        if prof.SMART in self._attr_supported_features:
            resp = await self.get_configuration(
                ckey.charging_schedule_allowed_charging_rate_unit.value
            )
            _LOGGER.info(
                "Charger supports setting the following units: %s",
                resp,
            )
            _LOGGER.info("If more than one unit supported default unit is Amps")
            if om.current.value in resp:
                lim = limit_amps
                units = ChargingRateUnitType.amps.value
            else:
                lim = limit_watts
                units = ChargingRateUnitType.watts.value
            resp = await self.get_configuration(
                ckey.charge_profile_max_stack_level.value
            )
            stack_level = int(resp)

            req = call.SetChargingProfilePayload(
                connector_id=0,
                cs_charging_profiles={
                    om.charging_profile_id.value: 8,
                    om.stack_level.value: stack_level,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.charge_point_max_profile.value,
                    om.charging_schedule.value: {
                        om.charging_rate_unit.value: units,
                        om.charging_schedule_period.value: [
                            {om.start_period.value: 0, om.limit.value: lim}
                        ],
                    },
                },
            )
        else:
            _LOGGER.info("Smart charging is not supported by this charger")
            return False
        resp = await self.call(req)
        if resp.status == ChargingProfileStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def set_availability(self, state: bool = True):
        """Become operative."""
        """there could be an ongoing transaction. Terminate it"""
        if (state is False) and self._transactionId > 0:
            await self.stop_transaction()
        """ change availability """
        if state is True:
            typ = AvailabilityType.operative.value
        else:
            typ = AvailabilityType.inoperative.value

        req = call.ChangeAvailabilityPayload(connector_id=0, type=typ)
        resp = await self.call(req)
        if resp.status == AvailabilityStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def start_transaction(self):
        """Remote start a transaction."""
        """Check if authorisation enabled, if it is disable it before remote start"""
        resp = await self.get_configuration(ckey.authorize_remote_tx_requests.value)
        if resp.lower() == "true":
            await self.configure(ckey.authorize_remote_tx_requests.value, "false")
        req = call.RemoteStartTransactionPayload(
            connector_id=1, id_tag=self._metrics[cdet.identifier.value].value
        )
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def stop_transaction(self):
        """Request remote stop of current transaction."""
        """Leaves charger in finishing state until unplugged"""
        """Use reset() to make the charger available again for remote start"""
        req = call.RemoteStopTransactionPayload(transaction_id=self._transactionId)
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def reset(self, typ: str = ResetType.hard):
        """Hard reset charger unless soft reset requested."""
        self._metrics[cstat.reconnects.value].value = 0
        req = call.ResetPayload(typ)
        resp = await self.call(req)
        if resp.status == ResetStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def unlock(self, connector_id: int = 1):
        """Unlock charger if requested."""
        req = call.UnlockConnectorPayload(connector_id)
        resp = await self.call(req)
        if resp.status == UnlockStatus.unlocked:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available."""
        """where firmware_url is the http or https url of the new firmware"""
        """and wait_time is hours from now to wait before install"""
        if prof.FW in self._attr_supported_features:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(firmware_url)
            except vol.MultipleInvalid as e:
                _LOGGER.debug("Failed to parse url: %s", e)
            update_time = (
                datetime.now(tz=timezone.utc) + timedelta(hours=wait_time)
            ).isoformat()
            req = call.UpdateFirmwarePayload(location=url, retrieve_date=update_time)
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.warning("Charger does not support ocpp firmware updating")
            return False

    async def get_diagnostics(self, upload_url: str):
        """Upload diagnostic data to server from charger."""
        if prof.FW in self._attr_supported_features:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(upload_url)
            except vol.MultipleInvalid as e:
                _LOGGER.warning("Failed to parse url: %s", e)
            req = call.GetDiagnosticsPayload(location=url)
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.warning("Charger does not support ocpp diagnostics uploading")
            return False

    async def data_transfer(self, vendor_id: str, message_id: str = "", data: str = ""):
        """Request vendor specific data transfer from charger."""
        req = call.DataTransferPayload(
            vendor_id=vendor_id, message_id=message_id, data=data
        )
        resp = await self.call(req)
        if resp.status == DataTransferStatus.accepted:
            _LOGGER.info(
                "Data transfer [vendorId(%s), messageId(%s), data(%s)] response: %s",
                vendor_id,
                message_id,
                data,
                resp.data,
            )
            self._metrics[cdet.data_response.value].value = datetime.now(
                tz=timezone.utc
            )
            self._metrics[cdet.data_response.value].extra_attr = {message_id: resp.data}
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def get_configuration(self, key: str = ""):
        """Get Configuration of charger for supported keys else return None."""
        if key == "":
            req = call.GetConfigurationPayload()
        else:
            req = call.GetConfigurationPayload(key=[key])
        resp = await self.call(req)
        if resp.configuration_key is not None:
            value = resp.configuration_key[0][om.value.value]
            _LOGGER.debug("Get Configuration for %s: %s", key, value)
            self._metrics[cdet.config_response.value].value = datetime.now(
                tz=timezone.utc
            )
            self._metrics[cdet.config_response.value].extra_attr = {key: value}
            return value
        if resp.unknown_key is not None:
            _LOGGER.warning("Get Configuration returned unknown key for: %s", key)
            return None

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
            if key_value[om.key.value] == key and key_value[om.value.value] == value:
                return

            if key_value.get(om.readonly.name, False):
                _LOGGER.warning("%s is a read only setting", key)

        req = call.ChangeConfigurationPayload(key=key, value=value)

        resp = await self.call(req)

        if resp.status in [
            ConfigurationStatus.rejected,
            ConfigurationStatus.not_supported,
        ]:
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
        except websockets.exceptions.WebSocketException as e:
            _LOGGER.debug("Websockets exception: %s", e)
        finally:
            await self._connection.close()
            self._connection = None
            self.status = STATE_UNAVAILABLE

    async def reconnect(self, connection):
        """Reconnect charge point."""
        self._connection = connection
        self._metrics[cstat.reconnects.value].value += 1
        try:
            self.status = STATE_OK
            await super().start()
        except websockets.exceptions.WebSocketException as e:
            _LOGGER.debug("Websockets exception: %s", e)
        finally:
            await self._connection.close()
            self._connection = None
            self.status = STATE_UNAVAILABLE

    async def async_update_device_info(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.central.cpid, boot_info)

        dr = device_registry.async_get(self.hass)

        serial = boot_info.get(om.charge_point_serial_number.name, None)

        identifiers = {(DOMAIN, self.central.cpid), (DOMAIN, self.id)}
        if serial is not None:
            identifiers.add((DOMAIN, serial))

        dr.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=identifiers,
            name=self.central.cpid,
            manufacturer=boot_info.get(om.charge_point_vendor.name, None),
            model=boot_info.get(om.charge_point_model.name, None),
            sw_version=boot_info.get(om.firmware_version.name, None),
        )

    def process_phases(self, data):
        """Process phase data from meter values payload."""

        def average_of_nonzero(values):
            nonzero_values: list = [v for v in values if float(v) != 0.0]
            nof_values: int = len(nonzero_values)
            average = sum(nonzero_values) / nof_values if nof_values > 0 else 0
            return average

        measurand_data = {}
        for sv in data:
            # create ordered Dict for each measurand, eg {"voltage":{"unit":"V","L1":"230"...}}
            measurand = sv.get(om.measurand.value, None)
            phase = sv.get(om.phase.value, None)
            value = sv.get(om.value.value, None)
            unit = sv.get(om.unit.value, None)
            context = sv.get(om.context.value, None)
            if measurand is not None and phase is not None:
                if measurand not in measurand_data:
                    measurand_data[measurand] = {}
                measurand_data[measurand][om.unit.value] = unit
                measurand_data[measurand][phase] = float(value)
                self._metrics[measurand].unit = unit
                self._metrics[measurand].extra_attr[om.unit.value] = unit
                self._metrics[measurand].extra_attr[phase] = float(value)
                self._metrics[measurand].extra_attr[om.context.value] = context

        line_phases = [Phase.l1.value, Phase.l2.value, Phase.l3.value]
        line_to_neutral_phases = [Phase.l1_n.value, Phase.l2_n.value, Phase.l2_n.value]
        line_to_line_phases = [Phase.l1_l2.value, Phase.l2_l3.value, Phase.l3_l1.value]

        for metric, phase_info in measurand_data.items():
            metric_value = None
            if metric in [Measurand.voltage.value]:
                if (phase_info.keys() & line_to_neutral_phases) is not None:
                    # Line to neutral voltages are averaged
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_to_neutral_phases]
                    )
                elif (phase_info.keys() & line_to_line_phases) is not None:
                    # Line to line voltages are averaged and converted to line to neutral
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_to_line_phases]
                    ) / sqrt(3)
            elif metric in [
                Measurand.current_import.value,
                Measurand.current_export.value,
                Measurand.power_active_import.value,
                Measurand.power_active_export.value,
            ]:
                if (phase_info.keys() & line_phases) is not None:
                    metric_value = sum(
                        phase_info.get(phase, 0) for phase in line_phases
                    )
                elif (phase_info.keys() & line_to_neutral_phases) is not None:
                    # Workaround for some chargers that erroneously use line to neutral for current
                    metric_value = sum(
                        phase_info.get(phase, 0) for phase in line_to_neutral_phases
                    )

            if metric_value is not None:
                metric_unit = phase_info.get(om.unit.value)
                _LOGGER.debug(
                    "process_phases: metric: %s, phase_info: %s value: %f unit :%s",
                    metric,
                    phase_info,
                    metric_value,
                    metric_unit,
                )
                if metric_unit == DEFAULT_POWER_UNIT:
                    self._metrics[metric].value = float(metric_value) / 1000
                    self._metrics[metric].unit = HA_POWER_UNIT
                elif metric_unit == DEFAULT_ENERGY_UNIT:
                    self._metrics[metric].value = float(metric_value) / 1000
                    self._metrics[metric].unit = HA_ENERGY_UNIT
                else:
                    self._metrics[metric].value = float(metric_value)
                    self._metrics[metric].unit = metric_unit

    @on(Action.MeterValues)
    def on_meter_values(self, connector_id: int, meter_value: Dict, **kwargs):
        """Request handler for MeterValues Calls."""
        self._metrics[csess.transaction_id.value].value = kwargs.get(
            om.transaction_id.name, 0
        )
        for bucket in meter_value:
            unprocessed = bucket[om.sampled_value.name]
            processed_keys = []
            for idx, sv in enumerate(bucket[om.sampled_value.name]):
                measurand = sv.get(om.measurand.value, None)
                value = sv.get(om.value.value, None)
                unit = sv.get(om.unit.value, None)
                phase = sv.get(om.phase.value, None)
                location = sv.get(om.location.value, None)
                context = sv.get(om.context.value, None)

                if len(sv.keys()) == 1:  # Backwars compatibility
                    measurand = DEFAULT_MEASURAND
                    unit = DEFAULT_ENERGY_UNIT

                if phase is None:
                    if unit == DEFAULT_POWER_UNIT:
                        self._metrics[measurand].value = float(value) / 1000
                        self._metrics[measurand].unit = HA_POWER_UNIT
                    elif unit == DEFAULT_ENERGY_UNIT:
                        self._metrics[measurand].value = float(value) / 1000
                        self._metrics[measurand].unit = HA_ENERGY_UNIT
                    else:
                        self._metrics[measurand].value = float(value)
                        self._metrics[measurand].unit = unit
                    if location is not None:
                        self._metrics[measurand].extra_attr[
                            om.location.value
                        ] = location
                    if context is not None:
                        self._metrics[measurand].extra_attr[om.context.value] = context
                    processed_keys.append(idx)
            for idx in sorted(processed_keys, reverse=True):
                unprocessed.pop(idx)
            # _LOGGER.debug("Meter data not yet processed: %s", unprocessed)
            if unprocessed is not None:
                self.process_phases(unprocessed)
        if csess.meter_start.value not in self._metrics:
            self._metrics[csess.meter_start.value].value = self._metrics[
                DEFAULT_MEASURAND
            ]
        if csess.transaction_id.value not in self._metrics:
            self._metrics[csess.transaction_id.value].value = kwargs.get(
                om.transaction_id.name
            )
            self._transactionId = kwargs.get(om.transaction_id.name)
        if self._metrics[csess.transaction_id.value].value == 0:
            self._metrics[csess.session_time.value].value = 0
            self._metrics[csess.meter_start.value].value = None
        else:
            self._metrics[csess.session_time.value].value = round(
                (
                    int(time.time())
                    - float(self._metrics[csess.transaction_id.value].value)
                )
                / 60
            )
        if self._metrics[csess.meter_start.value].value is not None:
            self._metrics[csess.session_energy.value].value = float(
                self._metrics[DEFAULT_MEASURAND].value or 0
            ) - float(self._metrics[csess.meter_start.value].value)
        else:
            self._metrics[csess.session_energy.value].value = 0
        self.hass.async_create_task(self.central.update(self.central.cpid))
        return call_result.MeterValuesPayload()

    @on(Action.BootNotification)
    def on_boot_notification(self, **kwargs):
        """Handle a boot notification."""
        resp = call_result.BootNotificationPayload(
            current_time=datetime.now(tz=timezone.utc).isoformat(),
            interval=3600,
            status=RegistrationStatus.accepted.value,
        )
        _LOGGER.debug("Received boot notification for %s: %s", self.id, kwargs)
        # update metrics
        self._metrics[cdet.model.value].value = kwargs.get(
            om.charge_point_model.name, None
        )
        self._metrics[cdet.vendor.value].value = kwargs.get(
            om.charge_point_vendor.name, None
        )
        self._metrics[cdet.firmware_version.value].value = kwargs.get(
            om.firmware_version.name, None
        )
        self._metrics[cdet.serial.value].value = kwargs.get(
            om.charge_point_serial_number.name, None
        )

        self.hass.async_create_task(self.async_update_device_info(kwargs))
        self.hass.async_create_task(self.central.update(self.central.cpid))
        self.hass.async_create_task(self.post_connect())
        return resp

    @on(Action.StatusNotification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Handle a status notification."""
        self._metrics[cstat.status.value].value = status
        if (
            status == ChargePointStatus.suspended_ev.value
            or status == ChargePointStatus.suspended_evse.value
        ):
            if Measurand.current_import.value in self._metrics:
                self._metrics[Measurand.current_import.value].value = 0
            if Measurand.power_active_import.value in self._metrics:
                self._metrics[Measurand.power_active_import.value].value = 0
            if Measurand.power_reactive_import.value in self._metrics:
                self._metrics[Measurand.power_reactive_import.value].value = 0
            if Measurand.current_export.value in self._metrics:
                self._metrics[Measurand.current_export.value].value = 0
            if Measurand.power_active_export.value in self._metrics:
                self._metrics[Measurand.power_active_export.value].value = 0
            if Measurand.power_reactive_export.value in self._metrics:
                self._metrics[Measurand.power_reactive_export.value].value = 0
        self._metrics[cstat.error_code.value].value = error_code
        self.hass.async_create_task(self.central.update(self.central.cpid))
        return call_result.StatusNotificationPayload()

    @on(Action.FirmwareStatusNotification)
    def on_firmware_status(self, status, **kwargs):
        """Handle firmware status notification."""
        self._metrics[cstat.firmware_status.value].value = status
        self.hass.async_create_task(self.central.update(self.central.cpid))
        return call_result.FirmwareStatusNotificationPayload()

    @on(Action.DiagnosticsStatusNotification)
    def on_diagnostics_status(self, status, **kwargs):
        """Handle diagnostics status notification."""
        _LOGGER.info("Diagnostics upload status: %s", status)
        return call_result.DiagnosticsStatusNotificationPayload()

    @on(Action.Authorize)
    def on_authorize(self, id_tag, **kwargs):
        """Handle an Authorization request."""
        return call_result.AuthorizePayload(
            id_tag_info={om.status.value: AuthorizationStatus.accepted.value}
        )

    @on(Action.StartTransaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        """Handle a Start Transaction request."""
        self._transactionId = int(time.time())
        self._metrics[cstat.stop_reason.value].value = ""
        self._metrics[csess.transaction_id.value].value = self._transactionId
        self._metrics[csess.meter_start.value].value = int(meter_start) / 1000
        self.hass.async_create_task(self.central.update(self.central.cpid))
        return call_result.StartTransactionPayload(
            id_tag_info={om.status.value: AuthorizationStatus.accepted.value},
            transaction_id=self._transactionId,
        )

    @on(Action.StopTransaction)
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        """Stop the current transaction."""
        self._metrics[cstat.stop_reason.value].value = kwargs.get(om.reason.name, None)

        if self._metrics[csess.meter_start.value].value is not None:
            self._metrics[csess.session_energy.value].value = int(
                meter_stop
            ) / 1000 - float(self._metrics[csess.meter_start.value].value)
        if Measurand.current_import.value in self._metrics:
            self._metrics[Measurand.current_import.value].value = 0
        if Measurand.power_active_import.value in self._metrics:
            self._metrics[Measurand.power_active_import.value].value = 0
        if Measurand.power_reactive_import.value in self._metrics:
            self._metrics[Measurand.power_reactive_import.value].value = 0
        if Measurand.current_export.value in self._metrics:
            self._metrics[Measurand.current_export.value].value = 0
        if Measurand.power_active_export.value in self._metrics:
            self._metrics[Measurand.power_active_export.value].value = 0
        if Measurand.power_reactive_export.value in self._metrics:
            self._metrics[Measurand.power_reactive_export.value].value = 0
        self.hass.async_create_task(self.central.update(self.central.cpid))
        return call_result.StopTransactionPayload(
            id_tag_info={om.status.value: AuthorizationStatus.accepted.value}
        )

    @on(Action.DataTransfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        """Handle a Data transfer request."""
        _LOGGER.debug("Datatransfer received from %s: %s", self.id, kwargs)
        return call_result.DataTransferPayload(status=DataTransferStatus.accepted.value)

    @on(Action.Heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle a Heartbeat."""
        now = datetime.now(tz=timezone.utc)
        self._metrics[cstat.heartbeat.value].value = now
        self.hass.async_create_task(self.central.update(self.central.cpid))
        return call_result.HeartbeatPayload(current_time=now.isoformat())

    @property
    def supported_features(self) -> int:
        """Flag of Ocpp features that are supported."""
        return self._attr_supported_features

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        return self._metrics[measurand].value

    def get_extra_attr(self, measurand: str):
        """Return last known extra attributes for given measurand."""
        return self._metrics[measurand].extra_attr

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        return self._metrics[measurand].unit

    def get_ha_unit(self, measurand: str):
        """Return home assistant unit of given measurand."""
        return self._metrics[measurand].ha_unit


class Metric:
    """Metric class."""

    def __init__(self, value, unit):
        """Initialize a Metric."""
        self._value = value
        self._unit = unit
        self._extra_attr = {}

    @property
    def value(self):
        """Get the value of the metric."""
        return self._value

    @value.setter
    def value(self, value):
        """Set the value of the metric."""
        self._value = value

    @property
    def unit(self):
        """Get the unit of the metric."""
        return self._unit

    @unit.setter
    def unit(self, unit: str):
        """Set the unit of the metric."""
        self._unit = unit

    @property
    def ha_unit(self):
        """Get the home assistant unit of the metric."""
        return UNITS_OCCP_TO_HA.get(self._unit, self._unit)

    @property
    def extra_attr(self):
        """Get the extra attributes of the metric."""
        return self._extra_attr

    @extra_attr.setter
    def extra_attr(self, extra_attr: dict):
        """Set the unit of the metric."""
        self._extra_attr = extra_attr
