"""Representation of a OCPP 2.0.1 or 2.1 charging station."""

import asyncio
import contextlib
from datetime import datetime, UTC
from dataclasses import dataclass, field
import logging

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
    DOMAIN,
    HA_ENERGY_UNIT,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


@dataclass
class InventoryReport:
    """Cached full inventory report for a charger."""

    evse_count: int = 0
    connector_count: list[int] = field(default_factory=list)
    smart_charging_available: bool = False
    reservation_available: bool = False
    local_auth_available: bool = False
    tx_updated_measurands: list[MeasurandEnumType] = field(default_factory=list)


class ChargePoint(cp):
    """Server side representation of a charger."""

    _inventory: InventoryReport | None = None
    _wait_inventory: asyncio.Event | None = None
    _connector_status: list[list[ConnectorStatusEnumType | None]]
    _tx_start_time: dict[int, datetime]
    _global_to_evse: dict[int, tuple[int, int]]  # global_idx -> (evse_id, connector_id)
    _evse_to_global: dict[tuple[int, int], int]  # (evse_id, connector_id) -> global_idx
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

    def _apply_status_notification(
        self, timestamp: str, connector_status: str, evse_id: int, connector_id: int
    ):
        """Update per connector and evse aggregated."""
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
        self._metrics[
            (global_idx, cstat.status_connector.value)
        ].value = ConnectorStatusEnumType(connector_status).value

        evse_status: ConnectorStatusEnumType | None = None
        for st in evse_list:
            if st is None:
                evse_status = None
                break
            evse_status = st
            if st != ConnectorStatusEnumType.available:
                break
        if evse_status is not None:
            if evse_status == ConnectorStatusEnumType.available:
                v16 = ChargePointStatusv16.available
            elif evse_status == ConnectorStatusEnumType.faulted:
                v16 = ChargePointStatusv16.faulted
            elif evse_status == ConnectorStatusEnumType.unavailable:
                v16 = ChargePointStatusv16.unavailable
            else:
                v16 = ChargePointStatusv16.preparing
            self._report_evse_status(evse_id, v16)

    def _flush_pending_status_notifications(self):
        """Flush buffered status notifications when the map is ready."""
        if not self._ensure_connector_map():
            return
        pending = self._pending_status_notifications
        self._pending_status_notifications = []
        for t, st, evse_id, conn_id in pending:
            self._apply_status_notification(t, st, evse_id, conn_id)
        self.hass.async_create_task(self.update(self.settings.cpid))

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
        self._wait_inventory = asyncio.Event()
        req = call.GetBaseReport(1, "FullInventory")
        resp: call_result.GetBaseReport | None = None
        try:
            resp = await self.call(req)
        except ocpp.exceptions.NotImplementedError:
            self._inventory = InventoryReport()
        except OCPPError:
            self._inventory = None
        if (resp is not None) and (resp.status == "Accepted"):
            await asyncio.wait_for(self._wait_inventory.wait(), self._response_timeout)
        self._wait_inventory = None
        if self._inventory:
            self._build_connector_map()

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        await self._get_inventory()
        return self._total_connectors()

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

    async def clear_profile(self):
        """Clear all charging profiles."""
        req: call.ClearChargingProfile = call.ClearChargingProfile(
            None,
            {
                "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile.value
            },
        )
        await self.call(req)

    async def set_charge_rate(
        self,
        limit_amps: int | None = None,
        limit_watts: int | None = None,
        conn_id: int = 0,
        profile: dict | None = None,
    ):
        """Set a charging profile with defined limit (OCPP 2.x).

        - conn_id=0 (default) targets the Charging Station (evse_id=0).
        - conn_id>0 targets the specific EVSE corresponding to the global connector index.
        """

        evse_target = 0
        if conn_id and conn_id > 0:
            with contextlib.suppress(Exception):
                evse_target, _ = self._global_to_pair(int(conn_id))
        if profile is not None:
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
            return

        if limit_watts is not None:
            if float(limit_watts) >= 22000:
                await self.clear_profile()
                return
            period_limit = int(limit_watts)
            unit_value = ChargingRateUnitEnumType.watts.value

        elif limit_amps is not None:
            if float(limit_amps) >= 32:
                await self.clear_profile()
                return
            period_limit = (
                int(limit_amps) if float(limit_amps).is_integer() else float(limit_amps)
            )
            unit_value = ChargingRateUnitEnumType.amps.value

        else:
            await self.clear_profile()
            return

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

        req: call.SetChargingProfile = call.SetChargingProfile(
            evse_target, charging_profile
        )
        resp: call_result.SetChargingProfile = await self.call(req)
        if resp.status != ChargingProfileStatusEnumType.accepted:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_variables_error",
                translation_placeholders={
                    "message": f"{str(resp.status)}: {str(resp.status_info)}"
                },
            )

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
            val = self._metrics[(evse, csess.transaction_id.value)].value
            tx_id = str(val) if val else None
        else:
            # Global stop: find the first active transaction across EVSEs
            for evse in range(1, total + 1):
                val = self._metrics[(evse, csess.transaction_id.value)].value
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
        return call_result.Heartbeat(current_time=datetime.now(tz=UTC).isoformat())

    def _report_evse_status(self, evse_id: int, evse_status_v16: ChargePointStatusv16):
        """Report EVSE-level status on the global connector."""
        self._metrics[(0, cstat.status_connector.value)].value = evse_status_v16.value
        self.hass.async_create_task(self.update(self.settings.cpid))

    @on(Action.status_notification)
    def on_status_notification(
        self, timestamp: str, connector_status: str, evse_id: int, connector_id: int
    ):
        """Perform OCPP callback."""
        if not self._ensure_connector_map():
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
                values: str = str(characteristics.get("values_list", "") or "")
                meas_list = [
                    s.strip() for s in values.split(",") if s is not None and s.strip()
                ]
                self._inventory.tx_updated_measurands = [
                    MeasurandEnumType(s) for s in meas_list
                ]
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
            and (self._metrics[(global_idx, csess.meter_start.value)].value is None)
        ):
            energy_measurand = MeasurandEnumType.energy_active_import_register.value
            for meter_value in converted_values:
                for measurand_item in meter_value:
                    if measurand_item.measurand == energy_measurand:
                        energy_value = cp.get_energy_kwh(measurand_item)
                        energy_unit = HA_ENERGY_UNIT if measurand_item.unit else None
                        self._metrics[
                            (global_idx, csess.meter_start.value)
                        ].value = energy_value
                        self._metrics[
                            (global_idx, csess.meter_start.value)
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
        global_idx: int = self._pair_to_global(evse_id, evse_conn_id)
        offline: bool = kwargs.get("offline", False)
        meter_values: list[dict] = kwargs.get("meter_value", [])
        self._set_meter_values(event_type, meter_values, evse_id, evse_conn_id)
        t = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

        if "charging_state" in transaction_info:
            state = transaction_info["charging_state"]
            evse_status_v16: ChargePointStatusv16 | None = None
            if state == ChargingStateEnumType.idle:
                evse_status_v16 = ChargePointStatusv16.available
            elif state == ChargingStateEnumType.ev_connected:
                evse_status_v16 = ChargePointStatusv16.preparing
            elif state == ChargingStateEnumType.suspended_evse:
                evse_status_v16 = ChargePointStatusv16.suspended_evse
            elif state == ChargingStateEnumType.suspended_ev:
                evse_status_v16 = ChargePointStatusv16.suspended_ev
            elif state == ChargingStateEnumType.charging:
                evse_status_v16 = ChargePointStatusv16.charging
            if evse_status_v16:
                self._report_evse_status(evse_id, evse_status_v16)

        response = call_result.TransactionEvent()
        id_token = kwargs.get("id_token")
        if id_token:
            response.id_token_info = {"status": AuthorizationStatusEnumType.accepted}
            id_tag_string: str = id_token["type"] + ":" + id_token["id_token"]
            self._metrics[(global_idx, cstat.id_tag.value)].value = id_tag_string

        if event_type == TransactionEventEnumType.started.value:
            self._tx_start_time[global_idx] = t
            tx_id: str = transaction_info["transaction_id"]
            self._metrics[(global_idx, csess.transaction_id.value)].value = tx_id
            self._metrics[(global_idx, csess.session_time.value)].value = 0
            self._metrics[
                (global_idx, csess.session_time.value)
            ].unit = UnitOfTime.MINUTES
        else:
            if self._tx_start_time.get(global_idx):
                elapsed = (t - self._tx_start_time[global_idx]).total_seconds()
                duration_minutes: int = int((elapsed + 59) // 60)
                self._metrics[
                    (global_idx, csess.session_time.value)
                ].value = duration_minutes
                self._metrics[
                    (global_idx, csess.session_time.value)
                ].unit = UnitOfTime.MINUTES
            if event_type == TransactionEventEnumType.ended.value:
                self._metrics[(global_idx, csess.transaction_id.value)].value = ""
                self._metrics[(global_idx, cstat.id_tag.value)].value = ""
                self._tx_start_time.pop(global_idx, None)

        if not offline:
            self.hass.async_create_task(self.update(self.settings.cpid))

        return response
