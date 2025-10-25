"""Representation of a OCPP 1.6 charging station."""

from datetime import datetime, timedelta, UTC
import logging

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfTime
import voluptuous as vol
from websockets.asyncio.server import ServerConnection

from ocpp.routing import on
from ocpp.v16 import call, call_result
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
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnlockStatus,
)

from .chargepoint import (
    OcppVersion,
    MeasurandValue,
    SetVariableResult,
)
from .chargepoint import ChargePoint as cp

from .enums import (
    ConfigurationKey as ckey,
    HAChargerDetails as cdet,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    OcppMisc as om,
    Profiles as prof,
)

from .const import (
    CentralSystemSettings,
    ChargerSystemSettings,
    DEFAULT_MEASURAND,
    DOMAIN,
    HA_ENERGY_UNIT,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


def _to_message_trigger(name: str) -> MessageTrigger | None:
    if isinstance(name, MessageTrigger):
        return name
    key = str(name).strip().replace(" ", "").replace("_", "").lower()
    mapping = {
        "bootnotification": MessageTrigger.boot_notification,
        "heartbeat": MessageTrigger.heartbeat,
        "metervalues": MessageTrigger.meter_values,
        "statusnotification": MessageTrigger.status_notification,
        "diagnosticsstatusnotification": MessageTrigger.diagnostics_status_notification,
        "firmwarestatusnotification": MessageTrigger.firmware_status_notification,
    }
    return mapping.get(key)


class ChargePoint(cp):
    """Server side representation of a charger."""

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
            OcppVersion.V16,
            hass,
            entry,
            central,
            charger,
        )
        self._active_tx: dict[int, int] = {}  # connector_id -> transaction_id

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        resp = None

        try:
            req = call.GetConfiguration(key=["NumberOfConnectors"])
            resp = await self.call(req)
        except Exception:
            resp = None

        cfg = None
        if resp is not None:
            cfg = getattr(resp, "configuration_key", None)

            if (
                cfg is None
                and isinstance(resp, list | tuple)
                and len(resp) >= 3
                and isinstance(resp[2], dict)
            ):
                cfg = resp[2].get("configurationKey") or resp[2].get(
                    "configuration_key"
                )

        if cfg:
            for kv in cfg:
                k = getattr(kv, "key", None)
                v = getattr(kv, "value", None)
                if k is None and isinstance(kv, dict):
                    k = kv.get("key")
                    v = kv.get("value")
                if k == "NumberOfConnectors" and v not in (None, ""):
                    try:
                        n = int(str(v).strip())
                        if n > 0:
                            return n
                    except (ValueError, TypeError):
                        pass

        return 1

    async def get_heartbeat_interval(self):
        """Retrieve heartbeat interval from the charger and store it."""
        await self.get_configuration(ckey.heartbeat_interval.value)

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""
        all_measurands = self.settings.monitored_variables or ""
        autodetect_measurands = bool(self.settings.monitored_variables_autoconfig)
        key = ckey.meter_values_sampled_data.value

        desired_csv = all_measurands.strip().strip(",")
        cfg_ok = {ConfigurationStatus.accepted, ConfigurationStatus.reboot_required}

        effective_csv: str = ""

        if autodetect_measurands:
            # One-shot CSV attempt
            if desired_csv:
                _LOGGER.debug(
                    "'%s' attempting CSV set for measurands: %s", self.id, desired_csv
                )
                try:
                    resp = await self.call(
                        call.ChangeConfiguration(key=key, value=desired_csv)
                    )
                    if getattr(resp, "status", None) in cfg_ok:
                        _LOGGER.debug(
                            "'%s' measurands CSV accepted with status=%s",
                            self.id,
                            resp.status,
                        )
                        effective_csv = desired_csv
                    else:
                        _LOGGER.debug(
                            "'%s' measurands CSV rejected with status=%s; falling back to GetConfiguration",
                            self.id,
                            getattr(resp, "status", None),
                        )
                except Exception as ex:
                    _LOGGER.debug(
                        "get_supported_measurands CSV set raised for '%s': %s",
                        self.id,
                        ex,
                    )

            # Always read back what the charger actually has
            chgr_csv = await self.get_configuration(key)

            if not effective_csv:
                _LOGGER.debug(
                    "'%s' measurands not configurable by integration", self.id
                )
                _LOGGER.debug("'%s' allowed measurands: '%s'", self.id, chgr_csv)
                return chgr_csv or ""

            _LOGGER.debug(
                "Returning accepted measurands for '%s': '%s'", self.id, effective_csv
            )
            await self.configure(key, effective_csv)
            return effective_csv

        # Non-autodetect path:
        if desired_csv:
            try:
                resp = await self.call(
                    call.ChangeConfiguration(key=key, value=desired_csv)
                )
                _LOGGER.debug(
                    "'%s' measurands set manually to %s", self.id, desired_csv
                )
                if getattr(resp, "status", None) in cfg_ok:
                    effective_csv = desired_csv
                else:
                    _LOGGER.debug(
                        "'%s' manual measurands set not accepted (status=%s); using charger's value",
                        self.id,
                        getattr(resp, "status", None),
                    )
                    effective_csv = await self.get_configuration(key)
            except Exception as ex:
                _LOGGER.debug(
                    "Manual measurands set failed for '%s': %s; using charger's value",
                    self.id,
                    ex,
                )
                effective_csv = await self.get_configuration(key)
        else:
            effective_csv = await self.get_configuration(key)

        if effective_csv:
            _LOGGER.debug("'%s' allowed measurands: '%s'", self.id, effective_csv)
            # Only configure if we successfully set our desired CSV
            if desired_csv and effective_csv == desired_csv:
                await self.configure(key, effective_csv)
        else:
            _LOGGER.debug("'%s' measurands not configurable by integration", self.id)

        return effective_csv or ""

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        await self.configure(
            ckey.meter_value_sample_interval.value,
            str(self.settings.meter_interval),
        )
        await self.configure(
            ckey.clock_aligned_data_interval.value,
            str(self.settings.idle_interval),
        )

    async def get_supported_features(self) -> prof:
        """Get features supported by the charger."""
        features = prof.NONE
        req = call.GetConfiguration(key=[ckey.supported_feature_profiles.value])
        resp = await self.call(req)
        try:
            feature_list = (resp.configuration_key[0][om.value.value]).split(",")
        except (IndexError, KeyError, TypeError):
            feature_list = [""]
        if feature_list[0] == "":
            _LOGGER.warning("No feature profiles detected, defaulting to Core")
            await self.notify_ha("No feature profiles detected, defaulting to Core")
            feature_list = [om.feature_profile_core.value]

        if self.settings.force_smart_charging:
            _LOGGER.warning("Force Smart Charging feature profile")
            features |= prof.SMART

        for item in feature_list:
            item = item.strip().replace(" ", "")
            if item == om.feature_profile_core.value:
                features |= prof.CORE
            elif item == om.feature_profile_firmware.value:
                features |= prof.FW
            elif item == om.feature_profile_smart.value:
                features |= prof.SMART
            elif item == om.feature_profile_reservation.value:
                features |= prof.RES
            elif item == om.feature_profile_remote.value:
                features |= prof.REM
            elif item == om.feature_profile_auth.value:
                features |= prof.AUTH
            else:
                _LOGGER.warning("Unknown feature profile detected ignoring: %s", item)
                await self.notify_ha(
                    f"Warning: Unknown feature profile detected ignoring {item}"
                )
        return features

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        req = call.TriggerMessage(requested_message=MessageTrigger.boot_notification)
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            self.triggered_boot_notification = True
            return True
        else:
            self.triggered_boot_notification = False
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        try:
            n = int(self._metrics[0][cdet.connectors.value].value or 1)
        except Exception:
            n = 1

        # Single connector: only probe 1. Multi: probe 0 then 1..n.
        attempts = [1] if n <= 1 else [0] + list(range(1, n + 1))

        for cid in attempts:
            _LOGGER.debug("trigger status notification for connector=%s", cid)
            try:
                req = call.TriggerMessage(
                    requested_message=MessageTrigger.status_notification,
                    connector_id=int(cid),
                )
                resp = await self.call(req)
                status = getattr(resp, "status", None)
            except Exception as ex:
                _LOGGER.debug("TriggerMessage failed for connector=%s: %s", cid, ex)
                status = None

            if status != TriggerMessageStatus.accepted:
                if cid > 0:
                    _LOGGER.warning("Failed with response: %s", status)
                    # Reduce to the last known-good connector index.
                    self._metrics[0][cdet.connectors.value].value = max(1, cid - 1)
                    return False
                # If connector 0 is rejected, continue probing numbered connectors.

        return True

    async def trigger_custom_message(
        self,
        requested_message: str | MessageTrigger = "StatusNotification",
    ):
        """Trigger Custom Message."""
        trig = _to_message_trigger(requested_message)
        if trig is None:
            _LOGGER.warning("Unsupported TriggerMessage: %s", requested_message)
            return False

        req = call.TriggerMessage(requested_message=trig)
        resp = await self.call(req)
        if resp.status != TriggerMessageStatus.accepted:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False
        return True

    async def clear_profile(
        self,
        conn_id: int | None = None,
        purpose: ChargingProfilePurposeType | None = None,
    ) -> bool:
        """Clear charging profiles (per connector and/or purpose)."""
        try:
            req = call.ClearChargingProfile(
                connector_id=(int(conn_id) if conn_id is not None else None),
                charging_profile_purpose=(purpose.value if purpose else None),
            )
            resp = await self.call(req)
            return resp.status in (
                ClearChargingProfileStatus.accepted,
                ClearChargingProfileStatus.unknown,
            )
        except Exception as ex:
            _LOGGER.debug("ClearChargingProfile raised %s (ignored)", ex)
            return False

    async def set_charge_rate(
        self,
        limit_amps: int = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
    ) -> bool:
        """Set charge rate."""
        if profile is not None:
            try:
                req = call.SetChargingProfile(
                    connector_id=int(conn_id), cs_charging_profiles=profile
                )
                resp = await self.call(req)
                if resp.status == ChargingProfileStatus.accepted:
                    return True
                _LOGGER.warning("Custom SetChargingProfile rejected: %s", resp.status)
            except Exception as ex:
                _LOGGER.warning("Custom SetChargingProfile failed: %s", ex)
                await self.notify_ha(
                    "Warning: Set charging profile failed with response Exception"
                )
            return False

        if prof.SMART not in self._attr_supported_features:
            _LOGGER.info("Smart charging is not supported by this charger")
            return False

        # Determine allowed unit (default to Amps if not reported)
        units_resp = await self.get_configuration(
            ckey.charging_schedule_allowed_charging_rate_unit.value
        )
        if not units_resp:
            _LOGGER.debug("Charging rate unit not reported; assuming Amps")
            units_resp = om.current.value

        use_amps = om.current.value in units_resp
        limit_value = float(limit_amps if use_amps else limit_watts)
        units_value = (
            ChargingRateUnitType.amps.value
            if use_amps
            else ChargingRateUnitType.watts.value
        )

        try:
            stack_level_resp = await self.get_configuration(
                ckey.charge_profile_max_stack_level.value
            )
            stack_level = int(stack_level_resp)
        except Exception:
            stack_level = 1

        # Helper to build a simple relative schedule with one period
        def _mk_schedule(_units: str, _limit: float) -> dict:
            return {
                om.charging_rate_unit.value: _units,
                om.charging_schedule_period.value: [
                    {om.start_period.value: 0, om.limit.value: _limit}
                ],
            }

        # Helper to generate a unique, stable chargingProfileId per purpose+connector
        def _profile_id(purpose: str, cid: int) -> int:
            base = {
                ChargingProfilePurposeType.charge_point_max_profile.value: 1000,
                ChargingProfilePurposeType.tx_default_profile.value: 2000,
                ChargingProfilePurposeType.tx_profile.value: 3000,
            }.get(purpose, 9000)
            try:
                n = int(cid or 0)
            except Exception:
                n = 0
            return base + max(0, n)

        # Try ChargePointMaxProfile (connectorId = 0)
        try:
            req = call.SetChargingProfile(
                connector_id=0,
                cs_charging_profiles={
                    om.charging_profile_id.value: _profile_id(
                        ChargingProfilePurposeType.charge_point_max_profile.value, 0
                    ),
                    om.stack_level.value: stack_level,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.charge_point_max_profile.value,
                    om.charging_schedule.value: _mk_schedule(units_value, limit_value),
                },
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                return True
            _LOGGER.debug(
                "ChargePointMaxProfile not accepted (%s); will continue.",
                resp.status,
            )
        except Exception as ex:
            _LOGGER.debug("ChargePointMaxProfile call raised: %s", ex)

        # Target connector (default 1 if unspecified/0)
        target_cid = int(conn_id) if conn_id and int(conn_id) > 0 else 1

        # Read active transaction on this connector
        try:
            active_tx_id = int(self._active_tx.get(target_cid, 0) or 0)
        except Exception:
            active_tx_id = 0

        txp_ok = False
        txd_ok = False

        # If an active transaction exists on this connector, try TxProfile first (affects ongoing charging)
        if active_tx_id > 0:
            try:
                txp_stack = max(1, stack_level)  # keep same or higher than defaults
                req = call.SetChargingProfile(
                    connector_id=target_cid,
                    cs_charging_profiles={
                        om.charging_profile_id.value: _profile_id(
                            ChargingProfilePurposeType.tx_profile.value, target_cid
                        ),
                        om.stack_level.value: txp_stack,
                        om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                        om.charging_profile_purpose.value: ChargingProfilePurposeType.tx_profile.value,
                        om.charging_schedule.value: _mk_schedule(
                            units_value, limit_value
                        ),
                        # Bind to the ongoing transaction
                        om.transaction_id.value: active_tx_id,
                    },
                )
                resp = await self.call(req)
                if resp.status == ChargingProfileStatus.accepted:
                    txp_ok = True
                else:
                    _LOGGER.debug("TxProfile not accepted (%s).", resp.status)
            except Exception as ex:
                _LOGGER.debug("TxProfile call raised: %s.", ex)

        # Always attempt TxDefaultProfile as well (for future sessions)
        try:
            tx_stack = max(
                1, stack_level - 1
            )  # slightly lower to avoid overriding TxProfile
            req = call.SetChargingProfile(
                connector_id=target_cid,
                cs_charging_profiles={
                    om.charging_profile_id.value: _profile_id(
                        ChargingProfilePurposeType.tx_default_profile.value, target_cid
                    ),
                    om.stack_level.value: tx_stack,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.tx_default_profile.value,
                    om.charging_schedule.value: _mk_schedule(units_value, limit_value),
                },
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                txd_ok = True
            else:
                _LOGGER.debug("Set TxDefaultProfile rejected: %s", resp.status)
                if txp_ok:
                    _LOGGER.debug(
                        f"Note: Active TxProfile applied, but TxDefaultProfile was rejected ({resp.status})."
                    )
        except Exception as ex:
            _LOGGER.debug("Set TxDefaultProfile failed: %s", ex)
            if txp_ok:
                _LOGGER.debug(
                    f"Note: Active TxProfile applied, but TxDefaultProfile failed: {ex}"
                )

        return bool(txp_ok or txd_ok)

    async def set_availability(self, state: bool = True, connector_id: int | None = 0):
        """Change availability."""
        try:
            conn = 0 if connector_id in (None, 0) else int(connector_id)
        except Exception:
            conn = 0

        typ = AvailabilityType.operative if state else AvailabilityType.inoperative
        req = call.ChangeAvailability(connector_id=conn, type=typ)

        try:
            resp = await self.call(req)
        except TimeoutError as ex:
            _LOGGER.debug("ChangeAvailability timed out (conn=%s): %s", conn, ex)
            return False
        except Exception as ex:
            _LOGGER.debug("ChangeAvailability failed (conn=%s): %s", conn, ex)
            return False

        try:
            status = getattr(resp, "status", None)

            # Fallback: some single-connector chargers reject station-level (connectorId=0).
            if status == AvailabilityStatus.rejected and conn == 0:
                try:
                    n = int(getattr(self, "num_connectors", 1) or 1)
                except Exception:
                    n = 1
                if n == 1:
                    _LOGGER.debug(
                        "Station-level ChangeAvailability rejected; retrying on connector 1."
                    )
                    return await self.set_availability(state=state, connector_id=1)

            pending_key = "availability_pending"
            target_str = "Operative" if state else "Inoperative"
            scope_str = "station" if conn == 0 else "connector"

            metric_key = (conn, cstat.status_connector.value)
            metric = self._metrics.get(metric_key)

            if status == AvailabilityStatus.scheduled:
                info = {
                    "target": target_str,
                    "scope": scope_str,
                    "since": datetime.now(tz=UTC).isoformat(),
                }
                if metric is not None:
                    metric.extra_attr[pending_key] = info

                self.hass.async_create_task(self.update(self.settings.cpid))
                return True

            if status == AvailabilityStatus.accepted:
                if metric is not None:
                    metric.extra_attr.pop(pending_key, None)
                self.hass.async_create_task(self.update(self.settings.cpid))
                return True

            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

        except Exception:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Set availability failed with response {resp.status}"
            )
            return False

    async def start_transaction(self, connector_id: int = 1):
        """Remote start a transaction."""
        _LOGGER.info("Start transaction with remote ID tag: %s", self._remote_id_tag)
        req = call.RemoteStartTransaction(
            connector_id=connector_id, id_tag=self._remote_id_tag
        )
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Start transaction failed with response {resp.status}"
            )
            return False

    async def stop_transaction(self, connector_id: int | None = None):
        """Request remote stop of current transaction.

        If connector_id is provided, only stop the transaction running on that connector.
        """
        # Resolve which transaction to stop
        tx_id = 0
        if connector_id is not None:
            # Per-connector stop: do NOT fall back to other connectors
            try:
                tx_id = int(self._active_tx.get(int(connector_id), 0) or 0)
            except Exception:
                tx_id = 0

            # For single-connector chargers, maintain compatibility with legacy global field
            if tx_id == 0:
                try:
                    n = int(getattr(self, "num_connectors", 0) or 0)
                except Exception:
                    n = 0
                if n == 1 and int(connector_id) in (0, 1):
                    tx_id = int(self.active_transaction_id or 0)
        else:
            # Global stop (legacy behavior): stop the known active tx, or any active tx
            tx_id = int(self.active_transaction_id or 0)
            if tx_id == 0:
                tx_id = next((int(v) for v in self._active_tx.values() if v), 0)

        # Nothing to stop - succeed as no-op
        if tx_id == 0:
            return True

        req = call.RemoteStopTransaction(transaction_id=tx_id)
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True

        _LOGGER.warning("Failed with response: %s", resp.status)
        await self.notify_ha(
            f"Warning: Stop transaction failed with response {resp.status}"
        )
        return False

    async def reset(self, typ: str = ResetType.hard):
        """Hard reset charger unless soft reset requested."""
        self._metrics[0][cstat.reconnects.value].value = 0
        req = call.Reset(typ)
        resp = await self.call(req)
        if resp.status == ResetStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Reset failed with response {resp.status}")
            return False

    async def unlock(self, connector_id: int = 1):
        """Unlock charger if requested."""
        req = call.UnlockConnector(connector_id)
        resp = await self.call(req)
        if resp.status == UnlockStatus.unlocked:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Unlock failed with response {resp.status}")
            return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available.

        - firmware_url: http/https URL of the new firmware
        - wait_time: hours from now to wait before install
        """
        features = int(self._attr_supported_features or 0)
        if not (features & prof.FW):
            _LOGGER.warning("Charger does not support OCPP firmware updating")
            return False

        schema = vol.Schema(vol.Url())
        try:
            url = schema(firmware_url)
        except vol.MultipleInvalid as e:
            _LOGGER.warning("Failed to parse url: %s", e)
            return False

        try:
            retrieve_time = (
                datetime.now(tz=UTC) + timedelta(hours=max(0, int(wait_time or 0)))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            retrieve_time = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            req = call.UpdateFirmware(location=str(url), retrieve_date=retrieve_time)
            resp = await self.call(req)
            _LOGGER.info("UpdateFirmware response: %s", resp)
            return True
        except Exception as e:
            _LOGGER.error("UpdateFirmware failed: %s", e)
            return False

    async def get_diagnostics(self, upload_url: str):
        """Upload diagnostic data to server from charger."""
        features = int(self._attr_supported_features or 0)
        if features & prof.FW:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(upload_url)
            except vol.MultipleInvalid as e:
                _LOGGER.warning("Failed to parse url: %s", e)
                return
            req = call.GetDiagnostics(location=str(url))
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.debug(
                "Charger %s does not support ocpp diagnostics uploading",
                self.id,
            )
            return False

    async def data_transfer(self, vendor_id: str, message_id: str = "", data: str = ""):
        """Request vendor specific data transfer from charger."""
        req = call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=data)
        resp = await self.call(req)
        if resp.status == DataTransferStatus.accepted:
            _LOGGER.info(
                "Data transfer [vendorId(%s), messageId(%s), data(%s)] response: %s",
                vendor_id,
                message_id,
                data,
                resp.data,
            )
            self._metrics[0][cdet.data_response.value].value = datetime.now(tz=UTC)
            self._metrics[0][cdet.data_response.value].extra_attr = {
                message_id: resp.data
            }
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Data transfer failed with response {resp.status}"
            )
            return False

    async def get_configuration(self, key: str = "") -> str:
        """Get Configuration of charger for supported keys else return None."""
        if key == "":
            req = call.GetConfiguration()
        else:
            req = call.GetConfiguration(key=[key])
        resp = await self.call(req)
        if resp.configuration_key:
            value = resp.configuration_key[0][om.value.value]
            _LOGGER.debug("Get Configuration for %s: %s", key, value)
            self._metrics[0][cdet.config_response.value].value = datetime.now(tz=UTC)
            self._metrics[0][cdet.config_response.value].extra_attr = {key: value}
            return value
        if resp.unknown_key:
            _LOGGER.warning("Get Configuration returned unknown key for: %s", key)
            await self.notify_ha(f"Warning: charger reports {key} is unknown")
            return "Unknown"

    async def configure(self, key: str, value: str):
        """Configure charger by setting the key to target value.

        First the configuration key is read using GetConfiguration. The key's
        value is compared with the target value. If the key is already set to
        the correct value nothing is done.

        If the key has a different value a ChangeConfiguration request is issued.

        """
        req = call.GetConfiguration(key=[key])

        resp = await self.call(req)

        if resp.unknown_key is not None:
            if key in resp.unknown_key:
                _LOGGER.warning("%s is unknown (not supported)", key)
                return "Unknown"

        for key_value in resp.configuration_key:
            # If the key already has the targeted value we don't need to set
            # it.
            if key_value[om.key.value] == key and key_value[om.value.value] == value:
                return

            if key_value.get(om.readonly.name, False):
                _LOGGER.warning("%s is a read only setting", key)
                await self.notify_ha(f"Warning: {key} is read-only")

        req = call.ChangeConfiguration(key=key, value=value)

        resp = await self.call(req)

        if resp.status in [
            ConfigurationStatus.rejected,
            ConfigurationStatus.not_supported,
        ]:
            _LOGGER.warning("%s while setting %s to %s", resp.status, key, value)
            await self.notify_ha(
                f"Warning: charger reported {resp.status} while setting {key}={value}"
            )
            return resp.status

        if resp.status == ConfigurationStatus.reboot_required:
            self._requires_reboot = True
            await self.notify_ha(f"A reboot is required to apply {key}={value}")
            return SetVariableResult.reboot_required

        return SetVariableResult.accepted

    async def async_update_device_info_v16(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.settings.cpid, boot_info)
        await self.async_update_device_info(
            boot_info.get(om.charge_point_serial_number.name, None),
            boot_info.get(om.charge_point_vendor.name, None),
            boot_info.get(om.charge_point_model.name, None),
            boot_info.get(om.firmware_version.name, None),
        )

    @on(Action.meter_values)
    def on_meter_values(self, connector_id: int, meter_value: dict, **kwargs):
        """Request handler for MeterValues Calls (multi-connector aware)."""

        transaction_id: int = int(kwargs.get(om.transaction_id.name, 0) or 0)
        tx_has_id: bool = transaction_id not in (None, 0)

        # Restore missing per-connector meter_start / active_transaction_id from HA if possible.
        ms_key = (connector_id, csess.meter_start.value)
        tx_key = (connector_id, csess.transaction_id.value)
        session_key = (connector_id, csess.session_time.value)

        if self._metrics[ms_key].value is None:
            value = self.get_ha_metric(csess.meter_start.value, connector_id)
            if value is None:
                m = self._metrics.get((connector_id, DEFAULT_MEASURAND))
                value = m.value if m is not None else None
            else:
                try:
                    value = float(value)
                    _LOGGER.debug(
                        "%s[%s] was None, restored value=%s from HA.",
                        csess.meter_start.value,
                        connector_id,
                        value,
                    )
                except (ValueError, TypeError):
                    value = None
            self._metrics[ms_key].value = value

        if self._metrics[tx_key].value is None:
            value = self.get_ha_metric(csess.transaction_id.value, connector_id)
            if value is None:
                value = transaction_id if transaction_id else None
            else:
                try:
                    value = int(value)
                    _LOGGER.debug(
                        "%s[%s] was None, restored value=%s from HA.",
                        csess.transaction_id.value,
                        connector_id,
                        value,
                    )
                except (ValueError, TypeError):
                    value = None
            self._metrics[tx_key].value = value
            # Track active tx per connector
            self._active_tx[connector_id] = value

        if connector_id not in self._active_tx:
            try:
                self._active_tx[connector_id] = int(self._metrics[tx_key].value or 0)
            except Exception:
                self._active_tx[connector_id] = 0

        recorded_tx = int(self._metrics[tx_key].value or 0)
        active_tx = int(self._active_tx.get(connector_id, 0) or 0)

        # Self-heal after restart: adopt incoming txId if we have none recorded yet
        if transaction_id and (recorded_tx == 0 and active_tx == 0):
            self._metrics[tx_key].value = transaction_id
            self._active_tx[connector_id] = transaction_id
            active_tx = transaction_id
            recorded_tx = transaction_id
            _LOGGER.debug(
                "Restored transactionId=%s on conn %s from MeterValues.",
                transaction_id,
                connector_id,
            )

        # Keep legacy field synced for single-connector chargers,
        # even if self-heal did not run (e.g., values were already restored).
        try:
            n_con = int(getattr(self, "num_connectors", 1) or 1)
        except Exception:
            n_con = 1
        if n_con == 1:
            try:
                legacy = int(getattr(self, "active_transaction_id", 0) or 0)
            except Exception:
                legacy = 0
            if legacy != int(active_tx or 0):
                self.active_transaction_id = int(active_tx or 0)

        transaction_matches: bool = False
        # Match is also false if no transaction is in progress, i.e. active_tx==transaction_id==0
        if transaction_id == active_tx and transaction_id != 0:
            transaction_matches = True
        elif transaction_id != 0 and active_tx != 0 and transaction_id != active_tx:
            _LOGGER.warning(
                "Unknown transaction detected on conn %s with id=%i (expected %s)",
                connector_id,
                transaction_id,
                active_tx,
            )

        meter_values: list[list[MeasurandValue]] = []
        for bucket in meter_value:
            measurands: list[MeasurandValue] = []
            for sampled_value in bucket.get(om.sampled_value.name, []):
                measurand = sampled_value.get(om.measurand.value, None)
                value = sampled_value.get(om.value.value, None)
                # Where an empty string is supplied convert to 0
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    value = 0.0
                unit = sampled_value.get(om.unit.value, None)
                phase = sampled_value.get(om.phase.value, None)
                location = sampled_value.get(om.location.value, None)
                context = sampled_value.get(om.context.value, None)
                measurands.append(
                    MeasurandValue(measurand, value, phase, unit, context, location)
                )
            meter_values.append(measurands)

        self.process_measurands(meter_values, transaction_matches, connector_id)

        if tx_has_id and transaction_matches:
            try:
                tx_start_epoch = float(self._metrics[tx_key].value)
            except (TypeError, ValueError):
                tx_start_epoch = time.time()
            if tx_start_epoch > 0:
                self._metrics[session_key].value = round(
                    (time.time() - tx_start_epoch) / 60
                )
                self._metrics[session_key].unit = UnitOfTime.MINUTES
            else:
                _LOGGER.debug(
                    "Skipping session time calc — invalid tx_start_epoch=%s",
                    tx_start_epoch,
                )
        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.MeterValues()

    @on(Action.boot_notification)
    def on_boot_notification(self, **kwargs):
        """Handle a boot notification."""
        resp = call_result.BootNotification(
            current_time=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            interval=3600,
            status=RegistrationStatus.accepted.value,
        )
        self.received_boot_notification = True
        _LOGGER.debug("Received boot notification for %s: %s", self.id, kwargs)

        self.hass.async_create_task(self.async_update_device_info_v16(kwargs))
        self._register_boot_notification()
        return resp

    @on(Action.status_notification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Handle a status notification."""

        if connector_id == 0 or connector_id is None:
            self._metrics[(0, cstat.status.value)].value = status
            self._metrics[(0, cstat.error_code.value)].value = error_code
        else:
            self._metrics[(connector_id, cstat.status_connector.value)].value = status
            self._metrics[
                (connector_id, cstat.error_code_connector.value)
            ].value = error_code

            if status in (
                ChargePointStatus.suspended_ev.value,
                ChargePointStatus.suspended_evse.value,
            ):
                for meas in [
                    Measurand.current_import.value,
                    Measurand.power_active_import.value,
                    Measurand.power_reactive_import.value,
                    Measurand.current_export.value,
                    Measurand.power_active_export.value,
                    Measurand.power_reactive_export.value,
                ]:
                    if meas in self._metrics[connector_id]:
                        self._metrics[(connector_id, meas)].value = 0

        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.StatusNotification()

    @on(Action.firmware_status_notification)
    def on_firmware_status(self, status, **kwargs):
        """Handle firmware status notification."""
        self._metrics[0][cstat.firmware_status.value].value = status
        self.hass.async_create_task(self.update(self.settings.cpid))
        self.hass.async_create_task(self.notify_ha(f"Firmware upload status: {status}"))
        return call_result.FirmwareStatusNotification()

    @on(Action.diagnostics_status_notification)
    def on_diagnostics_status(self, status, **kwargs):
        """Handle diagnostics status notification."""
        _LOGGER.info("Diagnostics upload status: %s", status)
        self.hass.async_create_task(
            self.notify_ha(f"Diagnostics upload status: {status}")
        )
        return call_result.DiagnosticsStatusNotification()

    @on(Action.security_event_notification)
    def on_security_event(self, type, timestamp, **kwargs):
        """Handle security event notification."""
        _LOGGER.info(
            "Security event notification received: %s at %s [techinfo: %s]",
            type,
            timestamp,
            kwargs.get(om.tech_info.name, "none"),
        )
        self.hass.async_create_task(
            self.notify_ha(f"Security event notification received: {type}")
        )
        return call_result.SecurityEventNotification()

    @on(Action.authorize)
    def on_authorize(self, id_tag, **kwargs):
        """Handle an Authorization request."""
        self._metrics[0][cstat.id_tag.value].value = id_tag
        auth_status = self.get_authorization_status(id_tag)
        return call_result.Authorize(id_tag_info={om.status.value: auth_status})

    @on(Action.start_transaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        """Handle a Start Transaction request."""

        auth_status = self.get_authorization_status(id_tag)
        if auth_status == AuthorizationStatus.accepted.value:
            tx_id = int(time.time())
            self._active_tx[connector_id] = tx_id
            self.active_transaction_id = tx_id
            self._metrics[(connector_id, cstat.id_tag.value)].value = id_tag
            self._metrics[(connector_id, cstat.stop_reason.value)].value = ""
            self._metrics[(connector_id, csess.transaction_id.value)].value = tx_id
            try:
                meter_start_kwh = float(meter_start) / 1000.0
            except Exception:
                meter_start_kwh = 0.0
            self._metrics[
                (connector_id, csess.meter_start.value)
            ].value = meter_start_kwh
            self._metrics[(connector_id, csess.meter_start.value)].unit = HA_ENERGY_UNIT

            self._metrics[(connector_id, csess.session_time.value)].value = 0
            self._metrics[
                (connector_id, csess.session_time.value)
            ].unit = UnitOfTime.MINUTES
            self._metrics[(connector_id, csess.session_energy.value)].value = 0.0
            self._metrics[
                (connector_id, csess.session_energy.value)
            ].unit = HA_ENERGY_UNIT

            result = call_result.StartTransaction(
                id_tag_info={om.status.value: AuthorizationStatus.accepted.value},
                transaction_id=tx_id,
            )
        else:
            result = call_result.StartTransaction(
                id_tag_info={om.status.value: auth_status},
                transaction_id=0,
            )

        self.hass.async_create_task(self.update(self.settings.cpid))
        return result

    @on(Action.stop_transaction)
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        """Stop the current transaction (multi-connector)."""

        # Resolve connector from active tx map
        conn = next(
            (c for c, tx in self._active_tx.items() if tx == transaction_id), None
        )
        if conn is None:
            _LOGGER.error(
                "Stop transaction received for unknown transaction id=%i",
                transaction_id,
            )
            conn = 1  # conservative fallback

        # Reset active transaction (global + per-connector)
        self._active_tx[conn] = 0
        self.active_transaction_id = 0
        self._metrics[(conn, cstat.id_tag.value)].value = ""
        self._metrics[(conn, csess.transaction_id.value)].value = 0
        self._metrics[(conn, cstat.stop_reason.value)].value = kwargs.get(
            om.reason.name, None
        )

        ms_key = (conn, csess.meter_start.value)
        if (
            self._metrics[ms_key].value is not None
            and not self._charger_reports_session_energy
        ):
            try:
                session_kwh = int(meter_stop) / 1000.0 - float(
                    self._metrics[ms_key].value
                )
            except Exception:
                session_kwh = 0.0
            self._metrics[(conn, csess.session_energy.value)].value = session_kwh

        for meas in [
            Measurand.current_import.value,
            Measurand.power_active_import.value,
            Measurand.power_reactive_import.value,
            Measurand.current_export.value,
            Measurand.power_active_export.value,
            Measurand.power_reactive_export.value,
        ]:
            key = (conn, meas)
            if key in self._metrics:
                self._metrics[key].value = 0

        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.StopTransaction(
            id_tag_info={om.status.value: AuthorizationStatus.accepted.value}
        )

    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        """Handle a Data transfer request."""
        _LOGGER.debug("Data transfer received from %s: %s", self.id, kwargs)
        self._metrics[0][cdet.data_transfer.value].value = datetime.now(tz=UTC)
        self._metrics[0][cdet.data_transfer.value].extra_attr = {vendor_id: kwargs}
        return call_result.DataTransfer(status=DataTransferStatus.accepted.value)

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle a Heartbeat."""
        now = datetime.now(tz=UTC)
        self._metrics[0][cstat.heartbeat.value].value = now
        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.Heartbeat(current_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
