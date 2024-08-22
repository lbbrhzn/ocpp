"""Common classes for charge points of all OCPP versions."""

import asyncio
from collections import defaultdict
from enum import Enum
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
import websockets.server

from ocpp.charge_point import ChargePoint as cp
from ocpp.v16 import call as callv16
from ocpp.v16 import call_result as call_resultv16
from ocpp.v16.enums import UnitOfMeasure
from ocpp.v201 import call as callv201
from ocpp.v201 import call_result as call_resultv201
from ocpp.messages import CallError

from .enums import (
    HAChargerDetails as cdet,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    Profiles as prof,
)

from .const import (
    DOMAIN,
    UNITS_OCCP_TO_HA,
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

        super().__init__(id, connection)
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

    async def post_connect(self):
        """Logic to be executed right after a charger connects."""
        pass

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
