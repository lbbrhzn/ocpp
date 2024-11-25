"""Common classes for charge points of all OCPP versions."""

import asyncio
from collections import defaultdict
from enum import Enum
import json
import logging
import secrets
import string
import time
from types import MappingProxyType
from typing import Any

from homeassistant.components.persistent_notification import DOMAIN as PN_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_OK, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.const import UnitOfTime
from homeassistant.helpers import device_registry, entity_component, entity_registry
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
import websockets.server

from ocpp.charge_point import ChargePoint as cp
from ocpp.v16 import call as callv16
from ocpp.v16 import call_result as call_resultv16
from ocpp.v16.enums import UnitOfMeasure, AuthorizationStatus
from ocpp.v201 import call as callv201
from ocpp.v201 import call_result as call_resultv201
from ocpp.messages import CallError
from ocpp.exceptions import NotImplementedError

from .enums import (
    HAChargerDetails as cdet,
    HAChargerServices as csvcs,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    Profiles as prof,
)

from .const import (
    CONF_AUTH_LIST,
    CONF_AUTH_STATUS,
    CONF_DEFAULT_AUTH_STATUS,
    CONF_ID_TAG,
    CONF_MONITORED_VARIABLES,
    CONFIG,
    DOMAIN,
    UNITS_OCCP_TO_HA,
)

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
CHRGR_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("limit_amps"): cv.positive_float,
        vol.Optional("limit_watts"): cv.positive_int,
        vol.Optional("conn_id"): cv.positive_int,
        vol.Optional("custom_profile"): vol.Any(cv.string, dict),
    }
)

TIME_MINUTES = UnitOfTime.MINUTES
_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


class CentralSystemSettings:
    """A subset of CentralSystem properties needed by a ChargePoint."""

    websocket_close_timeout: int
    websocket_ping_interval: int
    websocket_ping_timeout: int
    websocket_ping_tries: int
    csid: str
    cpid: str
    config: MappingProxyType[str, Any]


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


class OcppVersion(str, Enum):
    """OCPP version choice."""

    V16 = "1.6"
    V201 = "2.0.1"


class SetVariableResult(Enum):
    """A response to successful SetVariable call."""

    accepted = 0
    reboot_required = 1


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(
        self,
        id,
        connection,
        version: OcppVersion,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        interval_meter_metrics: int,
        skip_schema_validation: bool,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(id, connection, 10)
        if version == OcppVersion.V16:
            self._call = callv16
            self._call_result = call_resultv16
            self._ocpp_version = "1.6"
        elif version == OcppVersion.V201:
            self._call = callv201
            self._call_result = call_resultv201
            self._ocpp_version = "2.0.1"

        for action in self.route_map:
            self.route_map[action]["_skip_schema_validation"] = skip_schema_validation

        self.interval_meter_metrics = interval_meter_metrics
        self.hass = hass
        self.entry = entry
        self.central = central
        self.status = "init"
        # Indicates if the charger requires a reboot to apply new
        # configuration.
        self._requires_reboot = False
        self.preparing = asyncio.Event()
        self.active_transaction_id: int = 0
        self.triggered_boot_notification = False
        self.received_boot_notification = False
        self.post_connect_success = False
        self.tasks = None
        self._charger_reports_session_energy = False
        self._metrics = defaultdict(lambda: Metric(None, None))
        self._metrics[cdet.identifier.value].value = id
        self._metrics[csess.session_time.value].unit = TIME_MINUTES
        self._metrics[csess.session_energy.value].unit = UnitOfMeasure.kwh.value
        self._metrics[csess.meter_start.value].unit = UnitOfMeasure.kwh.value
        self._attr_supported_features = prof.NONE
        self._metrics[cstat.reconnects.value].value = 0
        alphabet = string.ascii_uppercase + string.digits
        self._remote_id_tag = "".join(secrets.choice(alphabet) for i in range(20))

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        return 0

    async def get_heartbeat_interval(self):
        """Retrieve heartbeat interval from the charger and store it."""
        pass

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""
        return ""

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        pass

    def register_version_specific_services(self):
        """Register HA services that differ depending on OCPP version."""
        pass

    async def get_supported_features(self) -> prof:
        """Get features supported by the charger."""
        return prof.NONE

    async def fetch_supported_features(self):
        """Get supported features."""
        self._attr_supported_features = await self.get_supported_features()
        self._metrics[cdet.features.value].value = self._attr_supported_features
        _LOGGER.debug("Feature profiles returned: %s", self._attr_supported_features)

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

        async def handle_set_charge_rate(call):
            """Handle the data transfer service call."""
            if self.status == STATE_UNAVAILABLE:
                _LOGGER.warning("%s charger is currently unavailable", self.id)
                return
            amps = call.data.get("limit_amps", None)
            watts = call.data.get("limit_watts", None)
            id = call.data.get("conn_id", 0)
            custom_profile = call.data.get("custom_profile", None)
            if custom_profile is not None:
                if type(custom_profile) is str:
                    custom_profile = custom_profile.replace("'", '"')
                    custom_profile = json.loads(custom_profile)
                await self.set_charge_rate(profile=custom_profile, conn_id=id)
            elif watts is not None:
                await self.set_charge_rate(limit_watts=watts, conn_id=id)
            elif amps is not None:
                await self.set_charge_rate(limit_amps=amps, conn_id=id)

        """Logic to be executed right after a charger connects."""

        try:
            self.status = STATE_OK
            await asyncio.sleep(2)
            await self.fetch_supported_features()
            num_connectors: int = await self.get_number_of_connectors()
            self._metrics[cdet.connectors.value].value = num_connectors
            await self.get_heartbeat_interval()

            accepted_measurands: str = await self.get_supported_measurands()
            updated_entry = {**self.entry.data}
            updated_entry[CONF_MONITORED_VARIABLES] = accepted_measurands
            self.hass.config_entries.async_update_entry(self.entry, data=updated_entry)

            await self.set_standard_configuration()

            # Register custom services with home assistant
            self.register_version_specific_services()
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
                self.hass.services.async_register(
                    DOMAIN,
                    csvcs.service_set_charge_rate.value,
                    handle_set_charge_rate,
                    CHRGR_SERVICE_DATA_SCHEMA,
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
            self.post_connect_success = True
            _LOGGER.debug(f"'{self.id}' post connection setup completed successfully")

            # nice to have, but not needed for integration to function
            # and can cause issues with some chargers
            await self.set_availability()
            if prof.REM in self._attr_supported_features:
                if self.received_boot_notification is False:
                    await self.trigger_boot_notification()
                await self.trigger_status_notification()
        except NotImplementedError as e:
            _LOGGER.error("Configuration of the charger failed: %s", e)

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        pass

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        pass

    async def clear_profile(self):
        """Clear all charging profiles."""
        pass

    async def set_charge_rate(
        self,
        limit_amps: int = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
    ):
        """Set a charging profile with defined limit."""
        pass

    async def set_availability(self, state: bool = True) -> bool:
        """Change availability."""
        return False

    async def start_transaction(self) -> bool:
        """Remote start a transaction."""
        return False

    async def stop_transaction(self) -> bool:
        """Request remote stop of current transaction.

        Leaves charger in finishing state until unplugged.
        Use reset() to make the charger available again for remote start
        """
        return False

    async def reset(self, typ: str | None = None) -> bool:
        """Hard reset charger unless soft reset requested."""
        return False

    async def unlock(self, connector_id: int = 1) -> bool:
        """Unlock charger if requested."""
        return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available."""
        """where firmware_url is the http or https url of the new firmware"""
        """and wait_time is hours from now to wait before install"""
        pass

    async def get_diagnostics(self, upload_url: str):
        """Upload diagnostic data to server from charger."""
        pass

    async def data_transfer(self, vendor_id: str, message_id: str = "", data: str = ""):
        """Request vendor specific data transfer from charger."""
        pass

    async def get_configuration(self, key: str = "") -> str | None:
        """Get Configuration of charger for supported keys else return None."""
        return None

    async def configure(self, key: str, value: str) -> SetVariableResult | None:
        """Configure charger by setting the key to target value."""
        return None

    async def _get_specific_response(self, unique_id, timeout):
        # The ocpp library silences CallErrors by default. See
        # https://github.com/mobilityhouse/ocpp/issues/104.
        # This code 'unsilences' CallErrors by raising them as exception
        # upon receiving.
        resp = await super()._get_specific_response(unique_id, timeout)

        if isinstance(resp, CallError):
            raise resp.to_exception()

        return resp

    async def monitor_connection(self):
        """Monitor the connection, by measuring the connection latency."""
        self._metrics[cstat.latency_ping.value].unit = "ms"
        self._metrics[cstat.latency_pong.value].unit = "ms"
        connection = self._connection
        timeout_counter = 0
        while connection.open:
            try:
                await asyncio.sleep(self.central.websocket_ping_interval)
                time0 = time.perf_counter()
                latency_ping = self.central.websocket_ping_timeout * 1000
                pong_waiter = await asyncio.wait_for(
                    connection.ping(), timeout=self.central.websocket_ping_timeout
                )
                time1 = time.perf_counter()
                latency_ping = round(time1 - time0, 3) * 1000
                latency_pong = self.central.websocket_ping_timeout * 1000
                await asyncio.wait_for(
                    pong_waiter, timeout=self.central.websocket_ping_timeout
                )
                timeout_counter = 0
                time2 = time.perf_counter()
                latency_pong = round(time2 - time1, 3) * 1000
                _LOGGER.debug(
                    f"Connection latency from '{self.central.csid}' to '{self.id}': ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[cstat.latency_ping.value].value = latency_ping
                self._metrics[cstat.latency_pong.value].value = latency_pong

            except TimeoutError as timeout_exception:
                _LOGGER.debug(
                    f"Connection latency from '{self.central.csid}' to '{self.id}': ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[cstat.latency_ping.value].value = latency_ping
                self._metrics[cstat.latency_pong.value].value = latency_pong
                timeout_counter += 1
                if timeout_counter > self.central.websocket_ping_tries:
                    _LOGGER.debug(
                        f"Connection to '{self.id}' timed out after '{self.central.websocket_ping_tries}' ping tries",
                    )
                    raise timeout_exception
                else:
                    continue

    async def _handle_call(self, msg):
        try:
            await super()._handle_call(msg)
        except NotImplementedError as e:
            response = msg.create_call_error(e).to_json()
            await self._send(response)

    async def start(self):
        """Start charge point."""
        await self.run(
            [super().start(), self.post_connect(), self.monitor_connection()]
        )

    async def run(self, tasks):
        """Run a specified list of tasks."""
        self.tasks = [asyncio.ensure_future(task) for task in tasks]
        try:
            await asyncio.gather(*self.tasks)
        except TimeoutError:
            pass
        except websockets.exceptions.WebSocketException as websocket_exception:
            _LOGGER.debug(f"Connection closed to '{self.id}': {websocket_exception}")
        except Exception as other_exception:
            _LOGGER.error(
                f"Unexpected exception in connection to '{self.id}': '{other_exception}'",
                exc_info=True,
            )
        finally:
            await self.stop()

    async def stop(self):
        """Close connection and cancel ongoing tasks."""
        self.status = STATE_UNAVAILABLE
        if self._connection.open:
            _LOGGER.debug(f"Closing websocket to '{self.id}'")
            await self._connection.close()
        for task in self.tasks:
            task.cancel()

    async def reconnect(self, connection: websockets.server.WebSocketServerProtocol):
        """Reconnect charge point."""
        _LOGGER.debug(f"Reconnect websocket to {self.id}")

        await self.stop()
        self.status = STATE_OK
        self._connection = connection
        self._metrics[cstat.reconnects.value].value += 1
        if self.post_connect_success is True:
            await self.run([super().start(), self.monitor_connection()])
        else:
            await self.run(
                [super().start(), self.post_connect(), self.monitor_connection()]
            )

    async def async_update_device_info(
        self, serial: str, vendor: str, model: str, firmware_version: str
    ):
        """Update device info asynchronuously."""

        self._metrics[cdet.model.value].value = model
        self._metrics[cdet.vendor.value].value = vendor
        self._metrics[cdet.firmware_version.value].value = firmware_version
        self._metrics[cdet.serial.value].value = serial

        identifiers = {
            (DOMAIN, self.central.cpid),
            (DOMAIN, self.id),
        }
        if serial is not None:
            identifiers.add((DOMAIN, serial))

        registry = device_registry.async_get(self.hass)
        registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=identifiers,
            name=self.central.cpid,
            manufacturer=vendor,
            model=model,
            suggested_area="Garage",
            sw_version=firmware_version,
        )

    def _register_boot_notification(self):
        self.hass.async_create_task(self.update(self.central.cpid))
        if self.triggered_boot_notification is False:
            self.hass.async_create_task(self.notify_ha(f"Charger {self.id} rebooted"))
            self.hass.async_create_task(self.post_connect())

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

    def get_authorization_status(self, id_tag):
        """Get the authorization status for an id_tag."""
        # authorize if its the tag of this charger used for remote start_transaction
        if id_tag == self._remote_id_tag:
            return AuthorizationStatus.accepted.value
        # get the domain wide configuration
        config = self.hass.data[DOMAIN].get(CONFIG, {})
        # get the default authorization status. Use accept if not configured
        default_auth_status = config.get(
            CONF_DEFAULT_AUTH_STATUS, AuthorizationStatus.accepted.value
        )
        # get the authorization list
        auth_list = config.get(CONF_AUTH_LIST, {})
        # search for the entry, based on the id_tag
        auth_status = None
        for auth_entry in auth_list:
            id_entry = auth_entry.get(CONF_ID_TAG, None)
            if id_tag == id_entry:
                # get the authorization status, use the default if not configured
                auth_status = auth_entry.get(CONF_AUTH_STATUS, default_auth_status)
                _LOGGER.debug(
                    f"id_tag='{id_tag}' found in auth_list, authorization_status='{auth_status}'"
                )
                break

        if auth_status is None:
            auth_status = default_auth_status
            _LOGGER.debug(
                f"id_tag='{id_tag}' not found in auth_list, default authorization_status='{auth_status}'"
            )
        return auth_status

    @property
    def supported_features(self) -> int:
        """Flag of Ocpp features that are supported."""
        return self._attr_supported_features

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        return self._metrics[measurand].value

    def get_ha_metric(self, measurand: str):
        """Return last known value in HA for given measurand."""
        entity_id = "sensor." + "_".join(
            [self.central.cpid.lower(), measurand.lower().replace(".", "_")]
        )
        try:
            value = self.hass.states.get(entity_id).state
        except Exception as e:
            _LOGGER.debug(f"An error occurred when getting entity state from HA: {e}")
            return None
        if value == STATE_UNAVAILABLE or value == STATE_UNKNOWN:
            return None
        return value

    def get_extra_attr(self, measurand: str):
        """Return last known extra attributes for given measurand."""
        return self._metrics[measurand].extra_attr

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        return self._metrics[measurand].unit

    def get_ha_unit(self, measurand: str):
        """Return home assistant unit of given measurand."""
        return self._metrics[measurand].ha_unit

    async def notify_ha(self, msg: str, title: str = "Ocpp integration"):
        """Notify user via HA web frontend."""
        await self.hass.services.async_call(
            PN_DOMAIN,
            "create",
            service_data={
                "title": title,
                "message": msg,
            },
            blocking=False,
        )
        return True
