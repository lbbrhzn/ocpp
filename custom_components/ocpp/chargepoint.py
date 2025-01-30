"""Common classes for charge points of all OCPP versions."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
import logging
from math import sqrt
import secrets
import string
import time

from homeassistant.components.persistent_notification import DOMAIN as PN_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_OK, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.const import UnitOfTime
from homeassistant.helpers import device_registry, entity_component, entity_registry
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import WebSocketException
from websockets.protocol import State

from ocpp.charge_point import ChargePoint as cp
from ocpp.v16 import call as callv16
from ocpp.v16 import call_result as call_resultv16
from ocpp.v16.enums import UnitOfMeasure, AuthorizationStatus, Measurand, Phase
from ocpp.v201 import call as callv201
from ocpp.v201 import call_result as call_resultv201
from ocpp.messages import CallError
from ocpp.exceptions import NotImplementedError

from .enums import (
    HAChargerDetails as cdet,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    OcppMisc as om,
    Profiles as prof,
)

from .const import (
    CentralSystemSettings,
    ChargerSystemSettings,
    CONF_AUTH_LIST,
    CONF_AUTH_STATUS,
    CONF_DEFAULT_AUTH_STATUS,
    CONF_ID_TAG,
    CONF_MONITORED_VARIABLES,
    CONF_CPIDS,
    CONFIG,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_POWER_UNIT,
    DEFAULT_MEASURAND,
    DOMAIN,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    UNITS_OCCP_TO_HA,
)

TIME_MINUTES = UnitOfTime.MINUTES
_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


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


@dataclass
class MeasurandValue:
    """Version-independent representation of a measurand."""

    measurand: str
    value: float
    phase: str | None
    unit: str | None
    context: str | None
    location: str | None


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(
        self,
        id,  # is charger cp_id not HA cpid
        connection,
        version: OcppVersion,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        charger: ChargerSystemSettings,
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
            self.route_map[action]["_skip_schema_validation"] = (
                charger.skip_schema_validation
            )

        self.hass = hass
        self.entry = entry
        self.cs_settings = central
        self.settings = charger
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

        try:
            self.status = STATE_OK
            await asyncio.sleep(2)
            await self.fetch_supported_features()
            num_connectors: int = await self.get_number_of_connectors()
            self._metrics[cdet.connectors.value].value = num_connectors
            await self.get_heartbeat_interval()

            accepted_measurands: str = await self.get_supported_measurands()
            updated_entry = {**self.entry.data}
            for i in range(len(updated_entry[CONF_CPIDS])):
                if self.id in updated_entry[CONF_CPIDS][i]:
                    updated_entry[CONF_CPIDS][i][self.id][CONF_MONITORED_VARIABLES] = (
                        accepted_measurands
                    )
                    break
            # if an entry differs this will unload/reload and stop/restart the central system/websocket
            self.hass.config_entries.async_update_entry(self.entry, data=updated_entry)

            await self.set_standard_configuration()

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
        while connection.state is State.OPEN:
            try:
                await asyncio.sleep(self.cs_settings.websocket_ping_interval)
                time0 = time.perf_counter()
                latency_ping = self.cs_settings.websocket_ping_timeout * 1000
                pong_waiter = await asyncio.wait_for(
                    connection.ping(), timeout=self.cs_settings.websocket_ping_timeout
                )
                time1 = time.perf_counter()
                latency_ping = round(time1 - time0, 3) * 1000
                latency_pong = self.cs_settings.websocket_ping_timeout * 1000
                await asyncio.wait_for(
                    pong_waiter, timeout=self.cs_settings.websocket_ping_timeout
                )
                timeout_counter = 0
                time2 = time.perf_counter()
                latency_pong = round(time2 - time1, 3) * 1000
                _LOGGER.debug(
                    f"Connection latency from '{self.cs_settings.csid}' to '{self.id}': ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[cstat.latency_ping.value].value = latency_ping
                self._metrics[cstat.latency_pong.value].value = latency_pong

            except TimeoutError as timeout_exception:
                _LOGGER.debug(
                    f"Connection latency from '{self.cs_settings.csid}' to '{self.id}': ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[cstat.latency_ping.value].value = latency_ping
                self._metrics[cstat.latency_pong.value].value = latency_pong
                timeout_counter += 1
                if timeout_counter > self.cs_settings.websocket_ping_tries:
                    _LOGGER.debug(
                        f"Connection to '{self.id}' timed out after '{self.cs_settings.websocket_ping_tries}' ping tries",
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
        except WebSocketException as websocket_exception:
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
        if self._connection.state is State.OPEN:
            _LOGGER.debug(f"Closing websocket to '{self.id}'")
            await self._connection.close()
        for task in self.tasks:
            task.cancel()

    async def reconnect(self, connection: ServerConnection):
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

        identifiers = {(DOMAIN, self.id), (DOMAIN, self.settings.cpid)}

        registry = device_registry.async_get(self.hass)
        registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=identifiers,
            manufacturer=vendor,
            model=model,
            sw_version=firmware_version,
        )

    def _register_boot_notification(self):
        if self.triggered_boot_notification is False:
            self.hass.async_create_task(self.notify_ha(f"Charger {self.id} rebooted"))
            self.hass.async_create_task(self.post_connect())

    async def update(self, cpid: str):
        """Update sensors values in HA."""
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)
        identifiers = {(DOMAIN, cpid), (DOMAIN, self.id)}
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

    def process_phases(self, data: list[MeasurandValue]):
        """Process phase data from meter values ."""

        def average_of_nonzero(values):
            nonzero_values: list = [v for v in values if v != 0.0]
            nof_values: int = len(nonzero_values)
            average = sum(nonzero_values) / nof_values if nof_values > 0 else 0
            return average

        measurand_data = {}
        for item in data:
            # create ordered Dict for each measurand, eg {"voltage":{"unit":"V","L1-N":"230"...}}
            measurand = item.measurand
            phase = item.phase
            value = item.value
            unit = item.unit
            context = item.context
            if measurand is not None and phase is not None and unit is not None:
                if measurand not in measurand_data:
                    measurand_data[measurand] = {}
                measurand_data[measurand][om.unit.value] = unit
                measurand_data[measurand][phase] = value
                self._metrics[measurand].unit = unit
                self._metrics[measurand].extra_attr[om.unit.value] = unit
                self._metrics[measurand].extra_attr[phase] = value
                self._metrics[measurand].extra_attr[om.context.value] = context

        line_phases = [Phase.l1.value, Phase.l2.value, Phase.l3.value, Phase.n.value]
        line_to_neutral_phases = [Phase.l1_n.value, Phase.l2_n.value, Phase.l3_n.value]
        line_to_line_phases = [Phase.l1_l2.value, Phase.l2_l3.value, Phase.l3_l1.value]

        for metric, phase_info in measurand_data.items():
            metric_value = None
            if metric in [Measurand.voltage.value]:
                if not phase_info.keys().isdisjoint(line_to_neutral_phases):
                    # Line to neutral voltages are averaged
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_to_neutral_phases]
                    )
                elif not phase_info.keys().isdisjoint(line_to_line_phases):
                    # Line to line voltages are averaged and converted to line to neutral
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_to_line_phases]
                    ) / sqrt(3)
                elif not phase_info.keys().isdisjoint(line_phases):
                    # Workaround for chargers that don't follow engineering convention
                    # Assumes voltages are line to neutral
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0) for phase in line_phases]
                    )
            else:
                if not phase_info.keys().isdisjoint(line_phases):
                    metric_value = sum(
                        phase_info.get(phase, 0) for phase in line_phases
                    )
                elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
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
                    self._metrics[metric].value = metric_value / 1000
                    self._metrics[metric].unit = HA_POWER_UNIT
                elif metric_unit == DEFAULT_ENERGY_UNIT:
                    self._metrics[metric].value = metric_value / 1000
                    self._metrics[metric].unit = HA_ENERGY_UNIT
                else:
                    self._metrics[metric].value = metric_value
                    self._metrics[metric].unit = metric_unit

    @staticmethod
    def get_energy_kwh(measurand_value: MeasurandValue) -> float:
        """Convert energy value from charger to kWh."""
        if (measurand_value.unit == "Wh") or (measurand_value.unit is None):
            return measurand_value.value / 1000
        return measurand_value.value

    def process_measurands(
        self, meter_values: list[list[MeasurandValue]], is_transaction: bool
    ):
        """Process all value from OCPP 1.6 MeterValues or OCPP 2.0.1 TransactionEvent."""
        for bucket in meter_values:
            unprocessed: list[MeasurandValue] = []
            for idx in range(len(bucket)):
                sampled_value: MeasurandValue = bucket[idx]
                measurand = sampled_value.measurand
                value = sampled_value.value
                unit = sampled_value.unit
                phase = sampled_value.phase
                location = sampled_value.location
                context = sampled_value.context
                # where an empty string is supplied convert to 0

                if sampled_value.measurand is None:  # Backwards compatibility
                    measurand = DEFAULT_MEASURAND
                    unit = DEFAULT_ENERGY_UNIT

                if measurand == DEFAULT_MEASURAND and unit is None:
                    unit = DEFAULT_ENERGY_UNIT

                if unit == DEFAULT_ENERGY_UNIT:
                    value = ChargePoint.get_energy_kwh(sampled_value)
                    unit = HA_ENERGY_UNIT

                if unit == DEFAULT_POWER_UNIT:
                    value = value / 1000
                    unit = HA_POWER_UNIT

                if self._metrics[csess.meter_start.value].value == 0:
                    # Charger reports Energy.Active.Import.Register directly as Session energy for transactions.
                    self._charger_reports_session_energy = True

                if phase is None:
                    if (
                        measurand == DEFAULT_MEASURAND
                        and self._charger_reports_session_energy
                    ):
                        if is_transaction:
                            self._metrics[csess.session_energy.value].value = value
                            self._metrics[csess.session_energy.value].unit = unit
                            self._metrics[csess.session_energy.value].extra_attr[
                                cstat.id_tag.name
                            ] = self._metrics[cstat.id_tag.value].value
                        else:
                            self._metrics[measurand].value = value
                            self._metrics[measurand].unit = unit
                    else:
                        self._metrics[measurand].value = value
                        self._metrics[measurand].unit = unit
                        if (
                            is_transaction
                            and (measurand == DEFAULT_MEASURAND)
                            and (self._metrics[csess.meter_start].value is not None)
                            and (self._metrics[csess.meter_start].unit == unit)
                        ):
                            meter_start = self._metrics[csess.meter_start].value
                            self._metrics[csess.session_energy.value].value = (
                                round(1000 * (value - meter_start)) / 1000
                            )
                            self._metrics[csess.session_energy.value].unit = unit
                    if location is not None:
                        self._metrics[measurand].extra_attr[om.location.value] = (
                            location
                        )
                    if context is not None:
                        self._metrics[measurand].extra_attr[om.context.value] = context
                else:
                    unprocessed.append(sampled_value)
            self.process_phases(unprocessed)

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
            [self.settings.cpid.lower(), measurand.lower().replace(".", "_")]
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
