"""Common classes for charge points of all OCPP versions."""

import asyncio
from collections import defaultdict
from collections.abc import MutableMapping
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
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import WebSocketException
from websockets.protocol import State

from ocpp.charge_point import ChargePoint as cp
from ocpp.v16 import call as callv16
from ocpp.v16 import call_result as call_resultv16
from ocpp.v16.enums import (
    AuthorizationStatus,
    Measurand,
    Phase,
    ReadingContext,
)
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
    CONF_NUM_CONNECTORS,
    CONF_CPIDS,
    CONFIG,
    DATA_UPDATED,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_NUM_CONNECTORS,
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


class _ConnectorAwareMetrics(MutableMapping):
    """Backwards compatible mapping for metrics.

    - m["Power.Active.Import"]         -> Metric for connector 0 (flat access)
    - m[(2, "Power.Active.Import")]    -> Metric for connector 2 (per connector)
    - m[2]                             -> dict[str -> Metric] for connector 2

    Iteration, len, keys(), values(), items() operate on connector 0 (flat view).
    """

    def __init__(self):
        self._by_conn = defaultdict(lambda: defaultdict(lambda: Metric(None, None)))

    def __getitem__(self, key):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            return self._by_conn[conn][meas]
        if isinstance(key, int):
            return self._by_conn[key]
        return self._by_conn[0][key]

    def __setitem__(self, key, value):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            if not isinstance(value, Metric):
                raise TypeError("Metric assignment must be a Metric instance.")
            self._by_conn[conn][meas] = value
            return
        if isinstance(key, int):
            if not isinstance(value, dict):
                raise TypeError("Connector mapping must be dict[str, Metric].")
            self._by_conn[key] = value
            return
        if not isinstance(value, Metric):
            raise TypeError("Metric assignment must be a Metric instance.")
        self._by_conn[0][key] = value

    def __delitem__(self, key):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            del self._by_conn[conn][meas]
            return
        if isinstance(key, int):
            del self._by_conn[key]
            return
        del self._by_conn[0][key]

    def __iter__(self):
        return iter(self._by_conn[0])

    def __len__(self):
        return len(self._by_conn[0])

    def get(self, key, default=None):
        if key in self:
            return self[key]
        return default

    def keys(self):
        return self._by_conn[0].keys()

    def values(self):
        return self._by_conn[0].values()

    def items(self):
        return self._by_conn[0].items()

    def clear(self):
        self._by_conn.clear()

    def __contains__(self, key):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            return meas in self._by_conn.get(conn, {})
        if isinstance(key, int):
            return key in self._by_conn
        return key in self._by_conn[0]


class OcppVersion(str, Enum):
    """OCPP version choice."""

    V16 = "1.6"
    V201 = "2.0.1"
    V21 = "2.1"


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
        elif version == OcppVersion.V21:
            self._call = callv201
            self._call_result = call_resultv201
            self._ocpp_version = "2.1"

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

        # Connector-aware, but backwards compatible:
        self._metrics: _ConnectorAwareMetrics = _ConnectorAwareMetrics()

        # Init standard metrics for connector 0
        self._metrics[(0, cdet.identifier.value)].value = id
        self._metrics[(0, cstat.reconnects.value)].value = 0

        self._attr_supported_features = prof.NONE
        alphabet = string.ascii_uppercase + string.digits
        self._remote_id_tag = "".join(secrets.choice(alphabet) for i in range(20))
        self.num_connectors: int = DEFAULT_NUM_CONNECTORS

    def _init_connector_slots(self, conn_id: int) -> None:
        """Ensure connector-scoped metrics exist and carry the right units."""
        _ = self._metrics[(conn_id, cstat.status_connector.value)]
        _ = self._metrics[(conn_id, cstat.error_code_connector.value)]
        _ = self._metrics[(conn_id, csess.transaction_id.value)]

        self._metrics[(conn_id, csess.session_time.value)].unit = TIME_MINUTES
        self._metrics[(conn_id, csess.session_energy.value)].unit = HA_ENERGY_UNIT
        self._metrics[(conn_id, csess.meter_start.value)].unit = HA_ENERGY_UNIT

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        return self.num_connectors

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
        self._metrics[(0, cdet.features.value)].value = self._attr_supported_features
        _LOGGER.debug(
            "Feature profiles returned: %s", self._attr_supported_features.labels()
        )

    async def post_connect(self):
        """Logic to be executed right after a charger connects."""
        try:
            self.status = STATE_OK
            await self.fetch_supported_features()
            self.num_connectors = await self.get_number_of_connectors()
            for conn in range(1, self.num_connectors + 1):
                self._init_connector_slots(conn)
            self._metrics[(0, cdet.connectors.value)].value = self.num_connectors
            await self.get_heartbeat_interval()

            accepted_measurands: str = await self.get_supported_measurands()
            updated_entry = {**self.entry.data}
            for i in range(len(updated_entry[CONF_CPIDS])):
                if self.id in updated_entry[CONF_CPIDS][i]:
                    s = updated_entry[CONF_CPIDS][i][self.id]
                    if s.get(CONF_MONITORED_VARIABLES) != accepted_measurands or s.get(
                        CONF_NUM_CONNECTORS
                    ) != int(self.num_connectors):
                        s[CONF_MONITORED_VARIABLES] = accepted_measurands
                        s[CONF_NUM_CONNECTORS] = int(self.num_connectors)
                    break
            # if an entry differs this will unload/reload and stop/restart the central system/websocket
            self.hass.config_entries.async_update_entry(self.entry, data=updated_entry)

            await self.set_standard_configuration()

            self.post_connect_success = True
            _LOGGER.debug("'%s' post connection setup completed successfully", self.id)

            # nice to have, but not needed for integration to function
            # and can cause issues with some chargers
            try:
                await self.set_availability()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                _LOGGER.debug("post_connect: set_availability ignored error: %s", ex)

            if prof.REM in self._attr_supported_features:
                if self.received_boot_notification is False:
                    try:
                        await asyncio.wait_for(
                            self.trigger_boot_notification(), timeout=3
                        )
                    except Exception as ex:
                        _LOGGER.debug("trigger_boot_notification ignored: %s", ex)
                try:
                    await asyncio.wait_for(
                        self.trigger_status_notification(), timeout=3
                    )
                except Exception as ex:
                    _LOGGER.debug("trigger_status_notification ignored: %s", ex)

        except Exception as e:
            _LOGGER.debug("post_connect aborted non-fatally: %s", e)

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        pass

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        pass

    async def trigger_custom_message(
        self,
        requested_message: str = "StatusNotification",
    ):
        """Trigger message request with a custom message."""
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

    async def start_transaction(self, connector_id: int = 1) -> bool:
        """Remote start a transaction."""
        return False

    async def stop_transaction(self, connector_id: int | None = None) -> bool:
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
        """Update charger with new firmware if available.

        - firmware_url is the http or https url of the new firmware
        - wait_time is hours from now to wait before install
        """
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
        self._metrics[(0, cstat.latency_ping.value)].unit = "ms"
        self._metrics[(0, cstat.latency_pong.value)].unit = "ms"
        connection = self._connection
        timeout_counter = 0

        # Add backstop to start post connect for non-compliant chargers
        # after 10s to allow for when a boot notification has not been received
        await asyncio.sleep(10)
        if not self.post_connect_success:
            self.hass.async_create_task(self.post_connect())

        while connection.state is State.OPEN:
            try:
                await asyncio.sleep(self.cs_settings.websocket_ping_interval)
                time0 = time.perf_counter()
                latency_ping = self.cs_settings.websocket_ping_timeout * 1000
                latency_pong = self.cs_settings.websocket_ping_timeout * 1000
                pong_waiter = await asyncio.wait_for(
                    connection.ping(), timeout=self.cs_settings.websocket_ping_timeout
                )
                time1 = time.perf_counter()
                latency_ping = round(time1 - time0, 3) * 1000

                await asyncio.wait_for(
                    pong_waiter, timeout=self.cs_settings.websocket_ping_timeout
                )
                timeout_counter = 0
                time2 = time.perf_counter()
                latency_pong = round(time2 - time1, 3) * 1000

                _LOGGER.debug(
                    f"Connection latency from '{self.cs_settings.csid}' to '{self.id}': "
                    f"ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[(0, cstat.latency_ping.value)].value = latency_ping
                self._metrics[(0, cstat.latency_pong.value)].value = latency_pong

            except TimeoutError as timeout_exception:
                timeout_counter += 1
                _LOGGER.debug(
                    f"Connection latency from '{self.cs_settings.csid}' to '{self.id}': "
                    f"ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[(0, cstat.latency_ping.value)].value = latency_ping
                self._metrics[(0, cstat.latency_pong.value)].value = latency_pong

                if timeout_counter > self.cs_settings.websocket_ping_tries:
                    _LOGGER.debug(
                        f"Connection to '{self.id}' timed out after '{self.cs_settings.websocket_ping_tries}' ping tries",
                    )
                    raise timeout_exception
                else:
                    continue
            except Exception as ex:
                _LOGGER.debug(f"monitor_connection stopping due to exception: {ex}")
                break

    async def _handle_call(self, msg):
        try:
            await super()._handle_call(msg)
        except NotImplementedError as e:
            response = msg.create_call_error(e).to_json()
            await self._send(response)

    async def start(self):
        """Start charge point."""
        await self.run([super().start(), self.monitor_connection()])

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
        self._metrics[(0, cstat.reconnects.value)].value += 1
        # post connect now handled on receiving boot notification or with backstop in monitor connection
        await self.run([super().start(), self.monitor_connection()])

    async def async_update_device_info(
        self, serial: str, vendor: str, model: str, firmware_version: str
    ):
        """Update device info asynchronously."""

        self._metrics[(0, cdet.model.value)].value = model
        self._metrics[(0, cdet.vendor.value)].value = vendor
        self._metrics[(0, cdet.firmware_version.value)].value = firmware_version
        self._metrics[(0, cdet.serial.value)].value = serial

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
            if not self.post_connect_success:
                self.hass.async_create_task(self.post_connect())

    async def update(self, cpid: str):
        """Update sensors values in HA (charger + connector child devices)."""
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)

        identifiers = {(DOMAIN, cpid), (DOMAIN, self.id)}
        root_dev = dr.async_get_device(identifiers)
        if root_dev is None:
            return

        to_visit: list[str] = [root_dev.id]
        visited: set[str] = set()
        active_entities: set[str] = set()

        while to_visit:
            dev_id = to_visit.pop(0)
            if dev_id in visited:
                continue
            visited.add(dev_id)

            # Collect enabled and currently loaded entities for this device
            for ent in entity_registry.async_entries_for_device(er, dev_id):
                if getattr(ent, "disabled", False) or getattr(ent, "disabled_by", None):
                    continue
                if self.hass.states.get(ent.entity_id) is None:
                    continue
                active_entities.add(ent.entity_id)

            for dev in dr.devices.values():
                if dev.via_device_id == dev_id and dev.id not in visited:
                    to_visit.append(dev.id)

        async_dispatcher_send(self.hass, DATA_UPDATED, active_entities)

    def get_authorization_status(self, id_tag):
        """Get the authorization status for an id_tag."""
        # authorize if its the tag of this charger used for remote start_transaction
        if id_tag == self._remote_id_tag:
            return AuthorizationStatus.accepted.value
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

    def process_phases(self, data: list[MeasurandValue], connector_id: int = 0):
        """Process per-phase MeterValues and aggregate them into per-connector metrics.

        Rules:
        - Voltage: average (L1-N/L2-N/L3-N or L-L divided by √3); fall back to averaging L1/L2/L3 if needed.
        - Current.*: average of L1/L2/L3 (ignore N).
        - Power.Factor: **average** of L1/L2/L3 (ignore N). *Do not sum; unit is dimensionless and may be missing.*
        - Other (e.g. Power.Active.*): sum of L1/L2/L3 (ignore N).
        """
        # For single-connector chargers, use connector 1.
        n_connectors = getattr(self, CONF_NUM_CONNECTORS, DEFAULT_NUM_CONNECTORS) or 1
        if connector_id in (None, 0):
            target_cid = 1 if n_connectors == 1 else 0
        else:
            try:
                target_cid = int(connector_id)
            except Exception:
                target_cid = 1 if n_connectors == 1 else 0

        def average_of_nonzero(values: list[float]) -> float:
            """Average only non-zero values; return 0.0 if all are zero or list is empty."""
            nonzero = [v for v in values if v != 0.0]
            return (sum(nonzero) / len(nonzero)) if nonzero else 0.0

        measurand_data: dict[str, dict[str, float]] = {}

        for item in data:
            # create ordered Dict for each measurand, eg {"voltage":{"unit":"V","L1-N":"230"...}}
            measurand = item.measurand
            phase = item.phase
            value = item.value
            unit = item.unit
            context = item.context

            if measurand is None or phase is None:
                continue

            if measurand not in measurand_data:
                measurand_data[measurand] = {}

            if unit is not None:
                measurand_data[measurand][om.unit.value] = unit
                self._metrics[(target_cid, measurand)].unit = unit
                self._metrics[(target_cid, measurand)].extra_attr[om.unit.value] = unit

            measurand_data[measurand][phase] = value
            self._metrics[(target_cid, measurand)].extra_attr[phase] = value
            if context is not None:
                self._metrics[(target_cid, measurand)].extra_attr[om.context.value] = (
                    context
                )

        line_phases_all = [
            Phase.l1.value,
            Phase.l2.value,
            Phase.l3.value,
            Phase.n.value,
        ]
        phases_l123 = [Phase.l1.value, Phase.l2.value, Phase.l3.value]
        line_to_neutral_phases = [Phase.l1_n.value, Phase.l2_n.value, Phase.l3_n.value]
        line_to_line_phases = [Phase.l1_l2.value, Phase.l2_l3.value, Phase.l3_l1.value]

        def _avg_l123(phase_info: dict) -> float:
            return average_of_nonzero(
                [phase_info.get(phase, 0.0) for phase in phases_l123]
            )

        def _sum_l123(phase_info: dict) -> float:
            return sum(phase_info.get(phase, 0.0) for phase in phases_l123)

        for metric, phase_info in measurand_data.items():
            metric_value: float | None = None
            mname = str(metric)

            if metric in [Measurand.voltage.value]:
                if not phase_info.keys().isdisjoint(line_to_neutral_phases):
                    # Line to neutral voltages are averaged
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0.0) for phase in line_to_neutral_phases]
                    )
                elif not phase_info.keys().isdisjoint(line_to_line_phases):
                    # Line to line voltages are averaged and converted to line to neutral
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0.0) for phase in line_to_line_phases]
                    ) / sqrt(3)
                elif not phase_info.keys().isdisjoint(line_phases_all):
                    # Workaround for chargers that don't follow engineering convention
                    # Assumes voltages are line to neutral
                    metric_value = _avg_l123(phase_info)

            else:
                is_current = mname.lower().startswith("current")
                if is_current:
                    # Current.* shown per phase -> avg of L1/L2/L3, ignore N
                    if not phase_info.keys().isdisjoint(phases_l123):
                        metric_value = _avg_l123(phase_info)
                    elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
                        # Workaround for some chargers that erroneously use line to neutral for current
                        metric_value = average_of_nonzero(
                            [
                                phase_info.get(phase, 0.0)
                                for phase in line_to_neutral_phases
                            ]
                        )

                # Special-case: Power.Factor must be averaged, never summed
                elif metric == Measurand.power_factor.value:
                    if not phase_info.keys().isdisjoint(phases_l123):
                        metric_value = _avg_l123(phase_info)
                    elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
                        metric_value = average_of_nonzero(
                            [phase_info.get(p, 0.0) for p in line_to_neutral_phases]
                        )
                    # If only a single phase value exists, just pass it through
                    else:
                        metric_value = next(
                            (v for k, v in phase_info.items() if k != om.unit.value),
                            None,
                        )

                else:
                    # Other (e.g. Power.*): total is sum over phases
                    if not phase_info.keys().isdisjoint(phases_l123):
                        metric_value = _sum_l123(phase_info)
                    elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
                        metric_value = sum(
                            phase_info.get(phase, 0.0)
                            for phase in line_to_neutral_phases
                        )

            if metric_value is not None:
                metric_unit = phase_info.get(om.unit.value)

                if metric_unit == DEFAULT_POWER_UNIT:
                    self._metrics[(target_cid, metric)].value = metric_value / 1000
                    self._metrics[(target_cid, metric)].unit = HA_POWER_UNIT
                elif metric_unit == DEFAULT_ENERGY_UNIT:
                    self._metrics[(target_cid, metric)].value = metric_value / 1000
                    self._metrics[(target_cid, metric)].unit = HA_ENERGY_UNIT
                else:
                    self._metrics[(target_cid, metric)].value = metric_value
                    self._metrics[(target_cid, metric)].unit = metric_unit

    @staticmethod
    def get_energy_kwh(measurand_value: MeasurandValue) -> float:
        """Convert energy value from charger to kWh."""
        if (measurand_value.unit == "Wh") or (measurand_value.unit is None):
            return measurand_value.value / 1000
        return measurand_value.value

    def process_measurands(
        self,
        meter_values: list[list[MeasurandValue]],
        is_transaction: bool,
        connector_id: int = 0,
    ):
        """Process all values from OCPP 1.6 MeterValues or OCPP 2.0.1 TransactionEvent."""

        for bucket in meter_values:
            # --- Preselect best EAIR in this bucket (ignore Transaction.Begin) ---
            best_eair_idx = None
            best_pr = -1
            best_val = None
            for j, sv in enumerate(bucket):
                meas = sv.measurand if sv.measurand is not None else DEFAULT_MEASURAND
                if meas != DEFAULT_MEASURAND:
                    continue
                ctx = sv.context or ReadingContext.sample_periodic.value
                # Always ignore Transaction.Begin for EAIR (prevents resets to 0)
                if ctx == ReadingContext.transaction_begin.value:
                    continue
                try:
                    kwh = float(
                        ChargePoint.get_energy_kwh(
                            MeasurandValue(
                                meas,
                                sv.value,
                                sv.phase,
                                sv.unit,
                                ctx,
                                sv.location,
                            )
                        )
                    )
                except Exception:
                    continue
                if kwh < 0.0 or kwh != kwh:
                    continue
                pr = 0
                if ctx == ReadingContext.transaction_end.value:
                    pr = 3
                elif ctx == ReadingContext.sample_periodic.value:
                    pr = 2
                elif ctx == ReadingContext.sample_clock.value:
                    pr = 1
                if (pr > best_pr) or (
                    pr == best_pr and (best_val is None or kwh > best_val)
                ):
                    best_pr = pr
                    best_val = kwh
                    best_eair_idx = j

            unprocessed: list[MeasurandValue] = []

            for idx, sampled_value in enumerate(bucket):
                measurand = sampled_value.measurand
                value = sampled_value.value
                unit = sampled_value.unit
                phase = sampled_value.phase
                location = sampled_value.location
                context = sampled_value.context or ReadingContext.sample_periodic.value

                # Backwards compatibility
                if sampled_value.measurand is None:
                    measurand = DEFAULT_MEASURAND
                    unit = unit or DEFAULT_ENERGY_UNIT

                if measurand == DEFAULT_MEASURAND and unit is None:
                    unit = DEFAULT_ENERGY_UNIT

                # Normalize units
                if unit == DEFAULT_ENERGY_UNIT:
                    value = ChargePoint.get_energy_kwh(
                        MeasurandValue(measurand, value, phase, unit, context, location)
                    )
                    unit = HA_ENERGY_UNIT

                if unit == DEFAULT_POWER_UNIT:
                    value = value / 1000
                    unit = HA_POWER_UNIT

                if self._metrics[(connector_id, csess.meter_start.value)].value == 0:
                    # Charger reports Energy.Active.Import.Register directly as Session energy for transactions.
                    self._charger_reports_session_energy = True

                if phase is None:
                    is_eair = measurand == DEFAULT_MEASURAND

                    # Determine if this is a single-connector charger (only if explicitly known)
                    try:
                        n_connectors = int(getattr(self, "num_connectors", 1) or 1)
                    except Exception:
                        n_connectors = 1

                    single = n_connectors == 1

                    # Choose target connector id
                    if is_eair:
                        if connector_id and connector_id > 0:
                            # Always honor a positive connector_id for EAIR, even without txId
                            target_cid = connector_id
                        else:
                            # connector_id == 0 or missing → map based on topology
                            target_cid = 1 if single else 0
                    else:
                        target_cid = connector_id

                    # For EAIR: process only the best candidate in this bucket, skip others (incl. Transaction.Begin)
                    if is_eair and idx != best_eair_idx:
                        continue

                    # Determine whether to skip writing EAIR to the main metric:
                    # - Skip only if this is an EAIR reading,
                    # - AND the charger reports session energy (meter_start == 0),
                    # - AND the reading belongs to an active transaction.
                    #
                    # Reason: in this situation, the EAIR value represents **session energy** for the current transaction,
                    # not the lifetime total meter. Writing it to the main metric would overwrite the true cumulative
                    # energy with a session-only value. For all other cases (non-EAIR readings or non-transaction readings),
                    # it is safe to write the metric normally.
                    skip_eair = (
                        is_eair
                        and self._charger_reports_session_energy
                        and is_transaction
                    )

                    if not skip_eair:
                        # Normal write
                        self._metrics[(target_cid, measurand)].value = value
                        self._metrics[(target_cid, measurand)].unit = unit
                        if location is not None:
                            self._metrics[(target_cid, measurand)].extra_attr[
                                om.location.value
                            ] = location
                        self._metrics[(target_cid, measurand)].extra_attr[
                            om.context.value
                        ] = context

                    # Session handling, only for EAIR during a transaction (per-connector)
                    if is_transaction and is_eair:
                        if self._charger_reports_session_energy:
                            # Charger reports session energy directly; ignore Transaction.Begin.
                            if context != ReadingContext.transaction_begin.value:
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].value = value
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].unit = unit
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].extra_attr[cstat.id_tag.name] = self._metrics[
                                    (target_cid, cstat.id_tag.value)
                                ].value
                        else:
                            # Initialize baseline on first tx-bound EAIR; then derive Session = EAIR - meter_start.
                            ms_metric = self._metrics[(target_cid, csess.meter_start)]
                            if ms_metric.value is None:
                                ms_metric.value = value
                                ms_metric.unit = unit
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].value = 0.0
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].unit = unit
                            elif ms_metric.unit == unit:
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].value = round(1000 * (value - ms_metric.value)) / 1000
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].unit = unit
                else:
                    unprocessed.append(sampled_value)

            try:
                self.process_phases(unprocessed, connector_id)
            except TypeError:
                self.process_phases(unprocessed)

    @property
    def supported_features(self) -> int:
        """Flag of Ocpp features that are supported."""
        return self._attr_supported_features

    def get_ha_metric(self, measurand: str, connector_id: int | None = None):
        """Return last known value in HA for given measurand, or None if not available."""
        base = self.settings.cpid.lower()
        meas_slug = measurand.lower().replace(".", "_")

        # Build list of possible sensor entity IDs.
        # Include connector-specific ID if applicable, then the generic one as fallback.
        candidates: list[str] = []
        if connector_id and connector_id > 0:
            candidates.append(f"sensor.{base}_connector_{connector_id}_{meas_slug}")
        candidates.append(f"sensor.{base}_{meas_slug}")

        # Return the first valid state found among candidates.
        for entity_id in candidates:
            try:
                st = self.hass.states.get(entity_id)
            except Exception as e:
                _LOGGER.debug("Error getting entity %s from HA: %s", entity_id, e)
                st = None

            if st and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                return st.state

        return None

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
