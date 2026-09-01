"""Representation of a OCPP 2.0.1 or 2.1 charging station."""

import asyncio
import contextlib
from datetime import datetime, UTC
from dataclasses import dataclass, field
import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError, HomeAssistantError
from websockets.asyncio.server import ServerConnection

import ocpp.exceptions
from ocpp.exceptions import OCPPError
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
    ClearChargingProfileStatusEnumType,
)

from .chargepoint import (
    SetVariableResult,
    MeasurandValue,
)
from .chargepoint import ChargePoint as cp

from .enums import Profiles

from .enums import (
    HAChargerStatuses as cstat,
    HAChargerSession as csess,
)

from .const import (
    CentralSystemSettings,
    ChargerSystemSettings,
    DEFAULT_NUM_CONNECTORS,
    DOMAIN,
    HA_ENERGY_UNIT,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)


@dataclass
class InventoryReport:
    """Cached full inventory report for a charger."""

    evse_count: int = 0
    connector_count: list[int] = field(default_factory=list)
    # None means the charger never reported SmartChargingCtrlr/Available.
    # The variable is optional in OCPP 2.0.1, so its absence is "unknown"
    # rather than "no" - the two are told apart here so that only the
    # unknown case is resolved by probing. An explicit false stands.
    smart_charging_available: bool | None = None
    reservation_available: bool = False
    local_auth_available: bool = False
    tx_updated_measurands: list[MeasurandEnumType] = field(default_factory=list)
    # Precedence of the resolved list, so a later report entry cannot
    # silently narrow a better-scoped one:
    #   2 = advertised valuesList, charging-station level - authoritative
    #   1 = advertised valuesList, EVSE-scoped - may be narrower than the
    #       station, so it is reported but never written back
    #   0 = derived from the current value, or entries were dropped
    tx_updated_measurands_rank: int = 0


class ChargePoint(cp):
    """Server side representation of a charger."""

    _inventory: InventoryReport | None = None
    _wait_inventory: asyncio.Event | None = None
    _connector_status: list[list[ConnectorStatusEnumType | None]]
    _tx_start_time: dict[int, datetime]
    _global_to_evse: dict[int, tuple[int, int]]  # global_idx -> (evse_id, connector_id)
    _evse_to_global: dict[tuple[int, int], int]  # (evse_id, connector_id) -> global_idx
    _evse_status_v16: dict[int, ChargePointStatusv16]
    _pending_status_notifications: list[
        tuple[str, str, int, int]
    ]  # (timestamp, connector_status, evse_id, connector_id)

    def __init__(
        self,
        id: str,
        connection: ServerConnection,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        charger: ChargerSystemSettings,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(
            id,
            connection,
            connection.subprotocol.replace("ocpp", ""),
            hass,
            entry,
            central,
            charger,
        )
        self._tx_start_time = {}
        self._global_to_evse: dict[int, tuple[int, int]] = {}
        self._evse_to_global: dict[tuple[int, int], int] = {}
        self._pending_status_notifications: list[tuple[str, str, int, int]] = []
        self._connector_status = []
        self._evse_status_v16: dict[int, ChargePointStatusv16] = {}

    # --- Connector mapping helpers (EVSE <-> global index) ---
    def _build_connector_map(self) -> bool:
        if not self._inventory or self._inventory.evse_count == 0:
            return False
        if self._evse_to_global and self._global_to_evse:
            return True

        g = 1
        self._evse_to_global.clear()
        self._global_to_evse.clear()
        for evse_id in range(1, self._inventory.evse_count + 1):
            count = 0
            if len(self._inventory.connector_count) >= evse_id:
                count = int(self._inventory.connector_count[evse_id - 1] or 0)
            for conn_id in range(1, count + 1):
                self._evse_to_global[(evse_id, conn_id)] = g
                self._global_to_evse[g] = (evse_id, conn_id)
                g += 1
        return bool(self._evse_to_global)

    def _ensure_connector_map(self) -> bool:
        if self._evse_to_global and self._global_to_evse:
            return True
        return self._build_connector_map()

    def _pair_to_global(self, evse_id: int, conn_id: int) -> int:
        """Return global index for (evse_id, conn_id)."""
        # Exact match available
        idx = self._evse_to_global.get((evse_id, conn_id))
        if idx is not None:
            return idx
        # Build from inventory if we have it
        if self._inventory and not self._evse_to_global:
            self._build_connector_map()
            idx = self._evse_to_global.get(
                (evse_id, conn_id)
            ) or self._evse_to_global.get((evse_id, 1))
            if idx is not None:
                return idx
        # Allocate a unique index to avoid collisions until inventory arrives
        new_idx = max(self._global_to_evse.keys(), default=0) + 1
        self._global_to_evse[new_idx] = (evse_id, conn_id)
        self._evse_to_global[(evse_id, conn_id)] = new_idx
        return new_idx

    def _global_to_pair(self, global_idx: int) -> tuple[int, int]:
        """Return (evse_id, connector_id) for a global index. Fallback: (global_idx,1)."""
        return self._global_to_evse.get(global_idx, (global_idx, 1))

    # Charging states are more specific than Occupied, which only reports that
    # a vehicle is connected. Only TransactionEvent can supply them.
    _CHARGING_STATES = frozenset(
        {
            ChargePointStatusv16.charging.value,
            ChargePointStatusv16.suspended_ev.value,
            ChargePointStatusv16.suspended_evse.value,
        }
    )

    def _aggregate_evse_status(self, evse_id: int):
        """Aggregate an EVSE's connector statuses, or None while any is unknown."""
        if evse_id - 1 >= len(self._connector_status):
            return None
        aggregate = None
        for status in self._connector_status[evse_id - 1]:
            if status is None:
                return None
            aggregate = status
            if status != ConnectorStatusEnumType.available:
                break
        return aggregate

    def _known_occupancy(self, evse_id: int, connector_id: int):
        """Return the last connector status reported for this pair, if any."""
        if evse_id - 1 < len(self._connector_status):
            row = self._connector_status[evse_id - 1]
            if connector_id - 1 < len(row):
                return row[connector_id - 1]
        return None

    def _has_live_transaction(self, evse_id: int, connector_id: int | None = None):
        """Whether a transaction is running on a connector, or on any of an EVSE's.

        _tx_start_time is populated when a transaction starts and dropped when
        it ends, so it is the authority on whether a charging state is still
        meaningful.
        """
        if connector_id is not None:
            # Deliberately not _pair_to_global: that allocates a global index
            # for an unknown pair, and a read-only predicate must not create
            # mappings for callers that merely ask a question.
            idx = self._evse_to_global.get((evse_id, connector_id))
            return idx is not None and idx in self._tx_start_time
        return any(
            idx in self._tx_start_time
            for (e, _c), idx in self._evse_to_global.items()
            if e == evse_id
        )

    # How prominent each state is when several EVSEs disagree. The charging
    # station has one status metric, so a second EVSE going idle must not
    # report the whole station as free while another is still delivering.
    # With a single EVSE the derived value is simply that EVSE's, so this
    # ordering only takes effect on multi-EVSE chargers.
    _STATION_PRECEDENCE: Final[list[str]] = [
        # Faulted first: _apply_status_notification notes that this metric must
        # not mask a faulted connector, and ranking a charge above it would
        # reintroduce that masking on a multi-EVSE charger.
        ChargePointStatusv16.faulted.value,
        ChargePointStatusv16.charging.value,
        ChargePointStatusv16.suspended_ev.value,
        ChargePointStatusv16.suspended_evse.value,
        ChargePointStatusv16.preparing.value,
        ChargePointStatusv16.finishing.value,
        ChargePointStatusv16.reserved.value,
        ChargePointStatusv16.unavailable.value,
        ChargePointStatusv16.available.value,
    ]

    def _derive_station_status(self) -> str | None:
        """Return the most prominent status across every known EVSE."""
        seen = {v.value for v in self._evse_status_v16.values()}
        for candidate in self._STATION_PRECEDENCE:
            if candidate in seen:
                return candidate
        return None

    @staticmethod
    def _charging_state_v16(state) -> ChargePointStatusv16 | None:
        """Map a transaction's chargingState onto the OCPP 1.6 vocabulary."""
        if state == ChargingStateEnumType.idle:
            return ChargePointStatusv16.available
        if state == ChargingStateEnumType.ev_connected:
            return ChargePointStatusv16.preparing
        if state == ChargingStateEnumType.suspended_evse:
            return ChargePointStatusv16.suspended_evse
        if state == ChargingStateEnumType.suspended_ev:
            return ChargePointStatusv16.suspended_ev
        if state == ChargingStateEnumType.charging:
            return ChargePointStatusv16.charging
        return None

    @staticmethod
    def _connector_status_v16(
        status: ConnectorStatusEnumType,
    ) -> ChargePointStatusv16:
        """Map an OCPP 2.0.1 connector status onto the 1.6 vocabulary.

        The status_connector metric is consumed by entities whose conditions
        are written in OCPP 1.6 terms (switch.py), so both the per-connector
        and the charging-station-level metric must speak that vocabulary.
        Occupied has no 1.6 equivalent on its own - it says a vehicle is
        connected, not whether it is charging - so it maps to Preparing and
        TransactionEvent's chargingState refines it from there.
        """
        if status == ConnectorStatusEnumType.available:
            return ChargePointStatusv16.available
        if status == ConnectorStatusEnumType.faulted:
            return ChargePointStatusv16.faulted
        if status == ConnectorStatusEnumType.unavailable:
            return ChargePointStatusv16.unavailable
        if status == ConnectorStatusEnumType.reserved:
            return ChargePointStatusv16.reserved
        return ChargePointStatusv16.preparing

    def _apply_status_notification(
        self, timestamp: str, connector_status: str, evse_id: int, connector_id: int
    ):
        """Update per connector and evse aggregated."""
        # Station-level notifications (evseId=0 / connectorId=0, which the OCPP
        # 2.0.1 spec allows and e.g. the FoxESS A-series sends on every boot)
        # don't belong in the per-connector bookkeeping below: evse_id - 1 == -1
        # would either raise IndexError, on the first such notification when
        # _connector_status is still empty, or silently write the station's
        # status into the LAST EVSE's slot once it is not.
        # Record them as the charger-level Status metric instead - the same key
        # the OCPP 1.6 handler uses for connectorId=0 and that the availability
        # switch reads. (0, Status.Connector) must stay owned by the EVSE
        # aggregation in _report_evse_status, or a station-level 'Available'
        # would mask a faulted connector via the flattened sensor's fallback
        # chain.
        if evse_id == 0 and connector_id == 0:
            self._metrics[(0, cstat.status)].value = ConnectorStatusEnumType(
                connector_status
            ).value
            return
        if evse_id < 1 or connector_id < 1:
            # Degenerate ids that are neither station-level nor a real
            # connector, e.g. (1, 0) or (0, 1). The per-connector bookkeeping
            # below would index them with -1 - the crash this guard exists to
            # prevent - and the charger-level metric would misattribute them,
            # so log and drop.
            _LOGGER.debug(
                "Ignoring malformed StatusNotification "
                "(evse_id=%s, connector_id=%s, status=%s)",
                evse_id,
                connector_id,
                connector_status,
            )
            return

        if evse_id > len(self._connector_status):
            needed = evse_id - len(self._connector_status)
            self._connector_status.extend([[] for _ in range(needed)])
        if connector_id > len(self._connector_status[evse_id - 1]):
            self._connector_status[evse_id - 1] += [None] * (
                connector_id - len(self._connector_status[evse_id - 1])
            )

        evse_list = self._connector_status[evse_id - 1]
        evse_list[connector_id - 1] = ConnectorStatusEnumType(connector_status)

        global_idx = self._pair_to_global(evse_id, connector_id)
        translated = self._connector_status_v16(
            ConnectorStatusEnumType(connector_status)
        )
        # Occupied says a vehicle is connected, not whether it is charging, so
        # it is strictly less specific than a charging state from
        # TransactionEvent. trigger_status_notification() provokes exactly this
        # message on every reconnect, and the periodic transaction events that
        # follow only carry chargingState when it changes - so letting it
        # overwrite Charging would turn charge_control off for the rest of the
        # session. Every other status is real news and still applies.
        current = self._metrics[(global_idx, cstat.status_connector)].value
        downgrades_live_charge = (
            translated == ChargePointStatusv16.preparing
            and current in self._CHARGING_STATES
            and self._has_live_transaction(evse_id, connector_id)
        )
        if not downgrades_live_charge:
            self._metrics[(global_idx, cstat.status_connector)].value = translated.value

        evse_status = self._aggregate_evse_status(evse_id)
        if evse_status is not None:
            aggregate = self._connector_status_v16(evse_status)
            # Same precedence as the per-connector write above, applied to this
            # EVSE's own state. Other EVSEs are handled by deriving the station
            # value in _report_evse_status rather than overwriting it.
            held = self._evse_status_v16.get(evse_id)
            if not (
                aggregate == ChargePointStatusv16.preparing
                and held is not None
                and held.value in self._CHARGING_STATES
                and self._has_live_transaction(evse_id)
            ):
                self._report_evse_status(evse_id, aggregate)

    def _drain_pending_status_notifications(self):
        """Apply and clear buffered status notifications, then notify HA.

        The HA update must be scheduled here rather than left to
        _apply_status_notification: that only schedules one via
        _report_evse_status, which is skipped while any connector in the EVSE
        still has no known status, so a drained entry could change a metric
        without the sensor ever refreshing.
        """
        pending = self._pending_status_notifications
        self._pending_status_notifications = []
        for t, st, evse_id, conn_id in pending:
            self._apply_status_notification(t, st, evse_id, conn_id)
        if pending:
            self.hass.async_create_task(self.update(self.settings.cpid))

    def _flush_pending_status_notifications(self):
        """Flush buffered status notifications when the map is ready."""
        if not self._ensure_connector_map():
            return
        self._drain_pending_status_notifications()

    def _total_connectors(self) -> int:
        """Total physical connectors across all EVSE."""
        if not self._inventory:
            return 0
        return sum(self._inventory.connector_count or [0])

    async def async_update_device_info_v201(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.settings.cpid, boot_info)
        await self.async_update_device_info(
            boot_info.get("serial_number", None),
            boot_info.get("vendor_name", None),
            boot_info.get("model", None),
            boot_info.get("firmware_version", None),
        )

    async def _get_inventory(self):
        if self._inventory is not None:
            return
        if self._wait_inventory is not None:
            # An attempt is already in flight (post_connect can run twice:
            # boot notification racing the 10s monitor backstop). Taking
            # ownership here would overwrite the owner's event, and once the
            # owner settled and cleared it, this caller's accepted response
            # would dereference None at _wait_inventory.wait(). Return and
            # leave the attempt - and the drain at its settle point - to the
            # single owner.
            return
        self._wait_inventory = asyncio.Event()
        req = call.GetBaseReport(1, "FullInventory")
        resp: call_result.GetBaseReport | None = None
        try:
            try:
                resp = await self.call(req)
            except ocpp.exceptions.NotImplementedError:
                self._inventory = InventoryReport()
            except OCPPError:
                self._inventory = None
            if (resp is not None) and (resp.status == "Accepted"):
                # A charger that accepts GetBaseReport but never finishes
                # reporting must not abort post_connect: swallowing the
                # timeout lets get_number_of_connectors fall back to one
                # connector below. Accepted trade-off: parts that did arrive
                # may yield a partial inventory, and since a same-version
                # reconnect reuses this ChargePoint, anything wrong with it
                # persists until the integration is reloaded.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wait_inventory.wait(), self._response_timeout
                    )
        finally:
            # Release ownership on EVERY exit. The request itself can raise
            # something the handlers above don't cover - the ocpp library's
            # response timeout is a bare TimeoutError, and the task can be
            # cancelled - and now that an in-flight event turns other callers
            # away, leaking it set would make every future attempt silently
            # return forever.
            self._wait_inventory = None
            # However this attempt ended - final report received, timed out,
            # refused, unsupported, or an escaping exception - it is over,
            # and nothing else will drain the statuses buffered while it ran:
            # on_report's flush only fires when a final part arrives, a
            # timed-out partial report that yielded SOME connectors bypasses
            # the zero-connector fallback entirely, and on a persistently
            # failing charger the next attempt would strand them again. Drain
            # inside the finally so this really is the one point every
            # outcome passes through. With a map (even a partial one)
            # statuses route through it; without one they take
            # _pair_to_global's dynamic allocation, so the first
            # charger-reported pair becomes connector 1. (Station-level
            # statuses never buffer - on_status_notification applies them
            # immediately - so only real connector pairs pass through here.)
            # A concurrent second caller (boot notification racing the 10s
            # monitor backstop) never reaches this point mid-stream - it
            # returns early on the _inventory check above - so a
            # half-streamed report can never be drained into a dynamic map
            # that the real inventory could then not replace.
            if self._inventory:
                self._build_connector_map()
            self._drain_pending_status_notifications()

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger.

        Some chargers (e.g. FoxESS A-series, issue #2008) answer
        GetBaseReport with an inventory that omits their EVSE/Connector
        components - only a charging-station-level ``evse.id=0`` entry is
        reported - so the inventory yields 0 connectors even though the
        charger clearly has one (its StatusNotification reports
        evseId=1/connectorId=1). Chargers that cannot answer GetBaseReport at
        all land in the same place.

        Returning 0 left the base post_connect connector-slot init loop
        empty, so session metrics (Time.Session, Session.Energy, meter_start)
        never received their units - raising a spurious `units_changed`
        repair when a charger switches between OCPP 1.6 and 2.0.1 - and the
        EVSE<->global connector map could never be built, so buffered
        StatusNotifications were held forever.

        Such a charger is exposed as one logical connector, matching the
        OCPP 1.6 path which already defaults to 1. No topology is invented:
        statuses route through _pair_to_global's existing dynamic
        allocation, so the first charger-reported pair becomes connector 1.

        Accepted limitation: a genuine multi-connector charger with an
        unusable inventory is exposed as a single connector - discovering
        more would require growing entities after setup. Statuses for a
        second pair route to an index with no entity behind it; harmless.
        A same-version reconnect reuses this ChargePoint and its maps, so
        anything imperfect here persists until the integration is reloaded
        (or the charger starts reporting a usable inventory and Home
        Assistant is restarted).
        """
        await self._get_inventory()
        total = self._total_connectors()
        if total == 0:
            # Buffered statuses are drained by the owning attempt when it
            # settles (see _get_inventory); a concurrent second caller can
            # reach this floor while that attempt is still in flight, and
            # correctly leaves them for the owner. Only the count needs
            # flooring here.
            total = DEFAULT_NUM_CONNECTORS
        return total

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        req = call.SetVariables(
            [
                {
                    "component": {"name": "SampledDataCtrlr"},
                    "variable": {"name": "TxUpdatedInterval"},
                    "attribute_value": str(self.settings.meter_interval),
                }
            ]
        )
        await self.call(req)

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""
        await self._get_inventory()
        if self._inventory:
            measurands: str = ",".join(
                measurand.value for measurand in self._inventory.tx_updated_measurands
            )
            if not measurands:
                # Nothing to go on. Writing an empty list would clear whatever
                # the charger is configured to report, disabling every meter
                # value inside TransactionEvent, and it persists on the
                # charger. Return the configured value unchanged so the config
                # entry is not overwritten either - post_connect stores this
                # result in monitored_variables and reloads the entry when it
                # differs, and sensor.py builds no measurand sensors from "".
                _LOGGER.warning(
                    "No measurands could be resolved for '%s'; leaving the "
                    "charger and the configured measurands untouched",
                    self.id,
                )
                return self.settings.monitored_variables or ""
            if self._inventory.tx_updated_measurands_rank < 2:
                # Anything short of a charging-station-level advertised list
                # describes either the charger's current configuration or a
                # single EVSE, while this SetVariables targets the station.
                # Writing it back could only narrow the station's settings.
                # Report it to Home Assistant, but leave the charger alone.
                _LOGGER.debug(
                    "Measurands for '%s' came from the charger's current "
                    "configuration; not writing them back",
                    self.id,
                )
                return measurands
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
        """Get feature profiles supported by the charger."""
        await self._get_inventory()
        features = Profiles.CORE
        if self._inventory and self._inventory.smart_charging_available:
            features |= Profiles.SMART
        if self._inventory and self._inventory.reservation_available:
            features |= Profiles.RES
        if self._inventory and self._inventory.local_auth_available:
            features |= Profiles.AUTH

        # Mirrors the OCPP 1.6 path. SmartChargingCtrlr/Available is optional
        # in OCPP 2.0.1, so a charger can implement smart charging and still
        # not advertise it, leaving the profile off. This override lets the
        # user restore it, and is the only escape hatch when detection fails.
        if self.settings.force_smart_charging:
            _LOGGER.warning("Force Smart Charging feature profile")
            features |= Profiles.SMART

        # SmartChargingCtrlr/Available is optional, so a charger can implement
        # smart charging and never advertise it - the FoxESS A-series reports
        # ProfileStackLevel, RateUnit and PeriodsPerSchedule but no Available.
        # Absence is not a denial, so resolve it by asking. GetCompositeSchedule
        # is read-only, unlike SetChargingProfile, which would mutate charger
        # state on every connect. Any answer at all proves the message is
        # implemented: Rejected is a legitimate reply to a schedule request the
        # charger cannot compute, and says nothing about support. Only a
        # CallError, which is how an unimplemented message comes back, denies it.
        if (Profiles.SMART not in features) and (
            self._inventory is None or self._inventory.smart_charging_available is None
        ):
            schedule_req = call.GetCompositeSchedule(60, 0)
            try:
                await self.call(schedule_req)
                features |= Profiles.SMART
            except OCPPError as e:
                _LOGGER.info("Smart charging not supported: %s", e)
            except TimeoutError:
                _LOGGER.warning(
                    "No response to GetCompositeSchedule probe, assuming no SMART"
                )

        fw_req = call.UpdateFirmware(
            1,
            {
                "location": "dummy://dummy",
                "retrieveDateTime": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "signature": "☺",
            },
        )
        # A probe that goes unanswered has to cost only its own profile. The
        # ocpp library raises asyncio.TimeoutError rather than an OCPPError
        # when a charger never replies, so catching OCPPError alone let that
        # escape get_supported_features, past the assignment in
        # fetch_supported_features, into post_connect's bare handler - leaving
        # every profile off, including SMART, and no feature metric at all.
        try:
            await self.call(fw_req)
            features |= Profiles.FW
        except OCPPError as e:
            _LOGGER.info("Firmware update not supported: %s", e)
        except TimeoutError:
            _LOGGER.warning("No response to UpdateFirmware probe, assuming no FW")

        trigger_req = call.TriggerMessage("StatusNotification")
        try:
            await self.call(trigger_req)
            features |= Profiles.REM
        except OCPPError as e:
            _LOGGER.info("TriggerMessage not supported: %s", e)
        except TimeoutError:
            _LOGGER.warning("No response to TriggerMessage probe, assuming no REM")

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

    async def clear_profile(self) -> bool:
        """Clear all charging profiles.

        Returns True when the charger accepted, or reported it had nothing to
        clear - Unknown means the end state we wanted already holds. Mirrors
        ocppv16.clear_profile, and lets set_charge_rate avoid claiming success
        for a clear the charger refused.
        """
        req: call.ClearChargingProfile = call.ClearChargingProfile(
            None,
            {
                "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile.value
            },
        )
        resp: call_result.ClearChargingProfile = await self.call(req)
        return resp.status in (
            ClearChargingProfileStatusEnumType.accepted,
            ClearChargingProfileStatusEnumType.unknown,
        )

    async def set_charge_rate(
        self,
        limit_amps: int | None = None,
        limit_watts: int | None = None,
        conn_id: int = 0,
        profile: dict | None = None,
    ) -> bool:
        """Set a charging profile with defined limit (OCPP 2.x).

        - A caller-supplied ``profile`` is sent to the EVSE mapped from
          ``conn_id`` (0 = the Charging Station, evse_id 0); which EVSE is
          valid depends on the profile's purpose.
        - A managed ``limit_amps``/``limit_watts`` request below the maximum
          builds a ChargingStationMaxProfile. OCPP 2.0.1 requires that profile
          on evse_id 0, so ``conn_id`` is not used for it. This mirrors 1.6,
          which sends its ChargePointMaxProfile on connector 0.
        - A request at or above the maximum, or with no limit at all, clears
          the station profile with ClearChargingProfile instead of sending one.

        Returns whether the charger honoured the request. Callers treat the
        result as a success flag - number.py logs a rejection when it is
        falsy - so a path that succeeded must say so, and one that cleared a
        profile must report what the charger made of that rather than assume.
        A refused SetChargingProfile still raises HomeAssistantError, which
        carries the charger's own status message.
        """

        if profile is not None:
            # A caller-supplied profile is an escape hatch: its purpose decides
            # which EVSE is valid, so honour the requested connector's mapping.
            evse_target = 0
            if conn_id and conn_id > 0:
                with contextlib.suppress(Exception):
                    evse_target, _ = self._global_to_pair(int(conn_id))
            req = call.SetChargingProfile(evse_target, profile)
            resp: call_result.SetChargingProfile = await self.call(req)
            if resp.status != ChargingProfileStatusEnumType.accepted:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="set_variables_error",
                    translation_placeholders={
                        "message": f"{str(resp.status)}: {str(resp.status_info)}"
                    },
                )
            return True

        # Removing the limit is a successful outcome too: a request at or above
        # the maximum means "no restriction", not a failure to apply one. The
        # amp threshold has to be the configured maximum rather than a literal
        # 32, because that is what bounds number.<cpid>_maximum_current - with
        # a higher max_current every request in between was turned into a bare
        # profile clear, so the charger ran unrestricted while the slider
        # showed the figure the user had asked for.
        if limit_watts is not None:
            if float(limit_watts) >= 22000:
                return await self.clear_profile()
            period_limit = int(limit_watts)
            unit_value = ChargingRateUnitEnumType.watts.value

        elif limit_amps is not None:
            if float(limit_amps) >= float(self.settings.max_current):
                return await self.clear_profile()
            period_limit = (
                int(limit_amps) if float(limit_amps).is_integer() else float(limit_amps)
            )
            unit_value = ChargingRateUnitEnumType.amps.value

        else:
            return await self.clear_profile()

        schedule: dict = {
            "id": 1,
            "charging_rate_unit": unit_value,
            "charging_schedule_period": [{"start_period": 0, "limit": period_limit}],
        }

        charging_profile: dict = {
            "id": 1,
            "stack_level": 0,
            "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile.value,
            "charging_profile_kind": ChargingProfileKindEnumType.relative.value,
            "charging_schedule": [schedule],
        }

        # ChargingStationMaxProfile describes the whole station and OCPP 2.0.1
        # requires it on evseId 0, so conn_id has no meaning for it. 1.6 sends
        # its ChargePointMaxProfile on connector 0 for the same reason.
        req: call.SetChargingProfile = call.SetChargingProfile(0, charging_profile)
        resp: call_result.SetChargingProfile = await self.call(req)
        if resp.status != ChargingProfileStatusEnumType.accepted:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_variables_error",
                translation_placeholders={
                    "message": f"{str(resp.status)}: {str(resp.status_info)}"
                },
            )
        return True

    async def set_availability(self, state: bool = True, connector_id: int | None = 0):
        """Change availability."""
        status = (
            OperationalStatusEnumType.operative.value
            if state
            else OperationalStatusEnumType.inoperative.value
        )
        if not connector_id:
            await self.call(call.ChangeAvailability(status))
            return

        evse_id = None
        with contextlib.suppress(Exception):
            evse_id, _ = self._global_to_pair(int(connector_id))

        if evse_id:
            await self.call(call.ChangeAvailability(status, evse={"id": evse_id}))
        else:
            await self.call(call.ChangeAvailability(status))

    async def start_transaction(self, connector_id: int = 1) -> bool:
        """Remote start a transaction."""
        evse_id = connector_id
        if connector_id and connector_id > 0:
            evse_id, _ = self._global_to_pair(connector_id)

        req: call.RequestStartTransaction = call.RequestStartTransaction(
            evse_id=evse_id,
            id_token={
                "id_token": self._remote_id_tag,
                "type": IdTokenEnumType.central.value,
            },
            remote_start_id=1,
        )
        resp: call_result.RequestStartTransaction = await self.call(req)
        return resp.status == RequestStartStopStatusEnumType.accepted.value

    async def stop_transaction(self, connector_id: int | None = None) -> bool:
        """Request remote stop of current transaction.

        If connector_id is provided, only stop the transaction running on that EVSE.
        If connector_id is None, stop the first active transaction found (legacy behavior).
        """
        await self._get_inventory()

        # Determine total EVSEs (connectors) if available
        total = int(self._total_connectors() or 1)

        tx_id: str | None = None

        if connector_id is not None:
            # Per-connector stop: do NOT fall back to other EVSEs
            evse = int(connector_id)
            if evse < 1 or evse > total:
                _LOGGER.info("Requested EVSE %s is out of range (1..%s)", evse, total)
                return False
            val = self._metrics[(evse, csess.transaction_id)].value
            tx_id = str(val) if val else None
        else:
            # Global stop: find the first active transaction across EVSEs
            for evse in range(1, total + 1):
                val = self._metrics[(evse, csess.transaction_id)].value
                if val:
                    tx_id = str(val)
                    break

        if not tx_id:
            _LOGGER.info("No active transaction found to stop")
            return False

        req: call.RequestStopTransaction = call.RequestStopTransaction(
            transaction_id=tx_id
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
        # Mirrors the OCPP 1.6 handler: record the heartbeat and push the
        # entities, so sensor.<cpid>_heartbeat tracks the charger. Without
        # the write the sensor keeps whatever an earlier session left -
        # heartbeats were answered here but recorded nowhere.
        now = datetime.now(tz=UTC)
        self._metrics[(0, cstat.heartbeat)].value = now
        self._async_refresh_metric_entities([cstat.heartbeat])
        # Deliberately not mirrored: 1.6 replies with whole seconds
        # (strftime %H:%M:%SZ); 2.0.1 keeps its pre-existing isoformat
        # reply, microseconds and all - both are valid RFC 3339.
        return call_result.Heartbeat(current_time=now.isoformat())

    def _report_evse_status(
        self,
        evse_id: int,
        evse_status_v16: ChargePointStatusv16,
        connector_id: int | None = None,
    ):
        """Report EVSE-level status on the global connector.

        With a connector_id the same value is also recorded against that
        connector. StatusNotification reports occupancy rather than charging
        state, so Charging/SuspendedEV/SuspendedEVSE can only reach the
        per-connector metric from TransactionEvent - and without them
        switch.charge_control, whose condition is written in those terms,
        can never read on.
        """
        if evse_id >= 1:
            self._evse_status_v16[evse_id] = evse_status_v16
        derived = self._derive_station_status()
        self._metrics[(0, cstat.status_connector)].value = (
            derived if derived is not None else evse_status_v16.value
        )
        if connector_id is not None and evse_id >= 1 and connector_id >= 1:
            # Same guard as _apply_status_notification: a degenerate pair would
            # have _pair_to_global allocate a phantom connector and strand the
            # real one, so it must not reach the metric from here either.
            global_idx = self._pair_to_global(evse_id, connector_id)
            self._metrics[
                (global_idx, cstat.status_connector)
            ].value = evse_status_v16.value
        self.hass.async_create_task(self.update(self.settings.cpid))

    @on(Action.status_notification)
    def on_status_notification(
        self, timestamp: str, connector_status: str, evse_id: int, connector_id: int
    ):
        """Perform OCPP callback."""
        # Station-level (0, 0) and malformed ids never route through the
        # connector map, so they are applied immediately: buffering them on a
        # charger whose inventory yields no map would strand them - and the
        # chargers that send station-level statuses (e.g. FoxESS A-series)
        # are exactly the ones with such inventories.
        if (
            evse_id >= 1
            and connector_id >= 1
            and not self._ensure_connector_map()
            and (not self.post_connect_success or self._wait_inventory is not None)
        ):
            # No inventory-derived map, and either setup is still running or
            # an inventory attempt is in flight (stop_transaction re-fetches
            # when none is cached, so this can happen after setup too): hold
            # the status until the attempt settles, so it cannot poison the
            # map of a charger whose real report is still on its way.
            # _get_inventory drains this buffer when the attempt ends, however
            # it ends, so nothing held here can be stranded.
            #
            # Once setup has finished and no attempt is running, a missing
            # map means the inventory is unusable and no flush is ever
            # coming - fall through and let _pair_to_global's dynamic
            # allocation route it instead of buffering it forever.
            self._pending_status_notifications.append(
                (timestamp, connector_status, evse_id, connector_id)
            )
            return call_result.StatusNotification()

        self._apply_status_notification(
            timestamp, connector_status, evse_id, connector_id
        )
        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.StatusNotification()

    @on(Action.firmware_status_notification)
    def on_firmware_status_notification(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.FirmwareStatusNotification()

    @on(Action.meter_values)
    def on_meter_values(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.MeterValues()

    @on(Action.log_status_notification)
    def on_log_status_notification(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.LogStatusNotification()

    @on(Action.notify_event)
    def on_notify_event(self, **kwargs):
        """Perform OCPP callback."""
        return call_result.NotifyEvent()

    @on(Action.notify_report)
    def on_report(self, request_id: int, generated_at: str, seq_no: int, **kwargs):
        """Handle OCPP 2.x inventory/report updates."""
        if self._wait_inventory is None:
            return call_result.NotifyReport()

        if self._inventory is None:
            self._inventory = InventoryReport()

        reports: list[dict] = kwargs.get("report_data", []) or []
        for report_data in reports:
            component: dict = report_data.get("component", {}) or {}
            variable: dict = report_data.get("variable", {}) or {}
            component_name: str = str(component.get("name", "") or "")
            variable_name: str = str(variable.get("name", "") or "")

            value: str | None = None
            for attr in report_data.get("variable_attribute", []) or []:
                if ("type" not in attr) or (
                    str(attr.get("type", "")).casefold() == "actual"
                ):
                    if "value" in attr:
                        v = attr.get("value")
                        value = str(v) if v is not None else None
                        break

            bool_value: bool = False
            if value is not None and str(value).strip():
                bool_value = str(value).strip().casefold() == "true"

            if (component_name == "SmartChargingCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.smart_charging_available = bool_value
                continue
            if (component_name == "ReservationCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.reservation_available = bool_value
                continue
            if (component_name == "LocalAuthListCtrlr") and (
                variable_name == "Available"
            ):
                self._inventory.local_auth_available = bool_value
                continue

            if (component_name == "EVSE") and ("evse" in component):
                evse_id = int(component["evse"].get("id", 0) or 0)
                if evse_id > 0:
                    self._inventory.evse_count = max(
                        self._inventory.evse_count, evse_id
                    )
                    if (
                        len(self._inventory.connector_count)
                        < self._inventory.evse_count
                    ):
                        self._inventory.connector_count += [0] * (
                            self._inventory.evse_count
                            - len(self._inventory.connector_count)
                        )
                continue

            if (
                (component_name == "Connector")
                and ("evse" in component)
                and ("connector_id" in component["evse"])
            ):
                evse_id = int(component["evse"].get("id", 0) or 0)
                conn_id = int(component["evse"].get("connector_id", 0) or 0)
                if evse_id > 0 and conn_id > 0:
                    self._inventory.evse_count = max(
                        self._inventory.evse_count, evse_id
                    )
                    if (
                        len(self._inventory.connector_count)
                        < self._inventory.evse_count
                    ):
                        self._inventory.connector_count += [0] * (
                            self._inventory.evse_count
                            - len(self._inventory.connector_count)
                        )
                    self._inventory.connector_count[evse_id - 1] = max(
                        self._inventory.connector_count[evse_id - 1], conn_id
                    )
                continue

            if (component_name == "SampledDataCtrlr") and (
                variable_name == "TxUpdatedMeasurands"
            ):
                characteristics: dict = (
                    report_data.get("variable_characteristics", {}) or {}
                )
                # valuesList is optional in OCPP 2.0.1, so a charger need not
                # advertise the measurands it supports. Fall back to the ones
                # it is currently configured to report - the variable's actual
                # value, which arrives in this same report - rather than
                # treating the omission as "no measurands".
                values: str = str(characteristics.get("values_list", "") or "")
                advertised: bool = bool(values.strip())
                if not advertised:
                    values = str(value or "")
                meas_list = [
                    s.strip() for s in values.split(",") if s is not None and s.strip()
                ]
                parsed: list[MeasurandEnumType] = []
                for s in meas_list:
                    try:
                        parsed.append(MeasurandEnumType(s))
                    except ValueError:
                        # Two sources feed this list, so a value the enum does
                        # not know must not abort the whole report. Dropping it
                        # does mean the list no longer describes the charger,
                        # which is why it is not authoritative below.
                        _LOGGER.debug(
                            "Ignoring unknown measurand '%s' from '%s'", s, self.id
                        )
                # Only an advertised valuesList we understood in full may be
                # written back. A list derived from the current value is the
                # charger's own configuration - writing it back is a no-op at
                # best - and a list that lost entries would narrow it.
                understood: bool = advertised and len(parsed) == len(meas_list)
                rank: int = 0
                if understood:
                    rank = 1 if "evse" in component else 2
                # SampledDataCtrlr is reportable per-EVSE and reports may be
                # chunked, so entries can arrive more than once and in any
                # order. Take a new list only when it is at least as
                # well-scoped, so ordering alone cannot decide what we hold -
                # or, via rank 2 below, what we write to the charger.
                if rank >= self._inventory.tx_updated_measurands_rank:
                    self._inventory.tx_updated_measurands = parsed
                    self._inventory.tx_updated_measurands_rank = rank
                continue

        if not kwargs.get("tbc", False):
            if hasattr(self, "_build_connector_map"):
                self._build_connector_map()
            if hasattr(self, "_flush_pending_status_notifications"):
                self._flush_pending_status_notifications()
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

    def _set_meter_values(
        self,
        tx_event_type: str,
        meter_values: list[dict],
        evse_id: int,
        connector_id: int,
    ):
        global_idx: int = self._pair_to_global(evse_id, connector_id)
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
            and (self._metrics[(global_idx, csess.meter_start)].value is None)
        ):
            energy_measurand = MeasurandEnumType.energy_active_import_register.value
            for meter_value in converted_values:
                for measurand_item in meter_value:
                    if measurand_item.measurand == energy_measurand:
                        energy_value = cp.get_energy_kwh(measurand_item)
                        energy_unit = HA_ENERGY_UNIT if measurand_item.unit else None
                        self._metrics[
                            (global_idx, csess.meter_start)
                        ].value = energy_value
                        self._metrics[
                            (global_idx, csess.meter_start)
                        ].unit = energy_unit

        self.process_measurands(converted_values, True, global_idx)

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
                        and ((global_idx, measurand) in self._metrics)
                        and not measurand.startswith("Energy")
                    ):
                        self._metrics[(global_idx, measurand)].value = 0

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
        evse_id: int = kwargs["evse"]["id"] if "evse" in kwargs else 1
        evse_conn_id: int = (
            kwargs["evse"].get("connector_id", 1) if "evse" in kwargs else 1
        )
        if evse_id < 1 or evse_conn_id < 1:
            # The same degenerate pair _apply_status_notification refuses. It
            # has to be caught before _pair_to_global, which would otherwise
            # allocate a phantom connector, record the transaction and its
            # meter values against it, and leave the real connector empty.
            # The charging state is still station-level news, so report that.
            _LOGGER.debug(
                "Ignoring connector-scoped data from a TransactionEvent with "
                "a malformed pair (evse_id=%s, connector_id=%s)",
                evse_id,
                evse_conn_id,
            )
            station_v16 = self._charging_state_v16(
                transaction_info.get("charging_state")
            )
            if station_v16:
                self._report_evse_status(evse_id, station_v16)
            return call_result.TransactionEvent()
        global_idx: int = self._pair_to_global(evse_id, evse_conn_id)
        offline: bool = kwargs.get("offline", False)
        meter_values: list[dict] = kwargs.get("meter_value", [])
        self._set_meter_values(event_type, meter_values, evse_id, evse_conn_id)
        t = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

        if "charging_state" in transaction_info:
            state = transaction_info["charging_state"]
            evse_status_v16 = self._charging_state_v16(state)
            if evse_status_v16:
                if state == ChargingStateEnumType.idle:
                    # Idle means no session, not an empty connector, so its
                    # Available must not reach the connector. Nor may the
                    # connector keep reporting Charging: a cable left in does
                    # not change the connector status, so the charger need not
                    # send another StatusNotification. Fall back to the
                    # occupancy already recorded, which is what describes the
                    # connector once the session has gone.
                    # The station keeps reporting the session's end. Only the
                    # charger knows whether the cable came out with it, so
                    # second-guessing that from stale occupancy would be wrong
                    # whenever the transaction ended because the EV left.
                    self._report_evse_status(evse_id, evse_status_v16)
                    known = self._known_occupancy(evse_id, evse_conn_id)
                    if known is not None:
                        self._metrics[
                            (global_idx, cstat.status_connector)
                        ].value = self._connector_status_v16(known).value
                else:
                    self._report_evse_status(
                        evse_id, evse_status_v16, connector_id=evse_conn_id
                    )

        response = call_result.TransactionEvent()
        id_token = kwargs.get("id_token")
        if id_token:
            response.id_token_info = {"status": AuthorizationStatusEnumType.accepted}
            id_tag_string: str = id_token["type"] + ":" + id_token["id_token"]
            self._metrics[(global_idx, cstat.id_tag)].value = id_tag_string

        if event_type == TransactionEventEnumType.started.value:
            self._tx_start_time[global_idx] = t
            tx_id: str = transaction_info["transaction_id"]
            self._metrics[(global_idx, csess.transaction_id)].value = tx_id
            self._metrics[(global_idx, csess.session_time)].value = 0
            self._metrics[(global_idx, csess.session_time)].unit = UnitOfTime.MINUTES
        else:
            if self._tx_start_time.get(global_idx):
                elapsed = (t - self._tx_start_time[global_idx]).total_seconds()
                duration_minutes: int = int((elapsed + 59) // 60)
                self._metrics[(global_idx, csess.session_time)].value = duration_minutes
                self._metrics[
                    (global_idx, csess.session_time)
                ].unit = UnitOfTime.MINUTES
            if event_type == TransactionEventEnumType.ended.value:
                self._metrics[(global_idx, csess.transaction_id)].value = ""
                self._metrics[(global_idx, cstat.id_tag)].value = ""
                self._tx_start_time.pop(global_idx, None)

        if not offline:
            self.hass.async_create_task(self.update(self.settings.cpid))

        return response
