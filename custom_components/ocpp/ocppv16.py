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
    DEFAULT_ENERGY_UNIT,
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

    def _profile_ids_for(
        self, conn_id: int, purpose: str, tx_id: int | None = None
    ) -> tuple[int, int]:
        """Return (chargingProfileId, stackLevel) unique per (purpose, connector).

        - Keeps IDs small and stable across restarts.
        - For TxProfile you may include tx_id to avoid clashes if multiple are alive.
        """
        PURPOSE_CODE = {
            "ChargePointMaxProfile": 1,
            "TxDefaultProfile": 2,
            "TxProfile": 3,
        }
        if purpose == "ChargePointMaxProfile":
            conn_seg = 0
        else:
            try:
                conn_seg = max(1, int(conn_id or 1))
            except Exception:
                conn_seg = 1

        base = 1000
        pid = base + PURPOSE_CODE[purpose] + conn_seg * 10

        if purpose == "TxProfile" and tx_id is not None:
            pid = pid * 1000 + (int(tx_id) % 1000)

        stack_level = 1
        return pid, stack_level

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
                resp = await self.call(
                    call.SetChargingProfile(
                        connector_id=int(conn_id), cs_charging_profiles=profile
                    )
                )
                if resp.status == ChargingProfileStatus.accepted:
                    return True
                _LOGGER.warning("Custom SetChargingProfile rejected: %s", resp.status)
            except Exception as ex:
                _LOGGER.warning("Custom SetChargingProfile failed: %s", ex)

        resp_units = await self.get_configuration(
            ckey.charging_schedule_allowed_charging_rate_unit.value
        )
        if resp_units is None:
            _LOGGER.warning("Failed to query charging rate unit, assuming Amps")
            resp_units = om.current.value

        use_amps = om.current.value in resp_units
        limit_val = float(limit_amps if use_amps else limit_watts)
        unit_val = (
            ChargingRateUnitType.amps.value
            if use_amps
            else ChargingRateUnitType.watts.value
        )

        # Build attempt order (CPMax -> TxDefault -> TxProfile if active)
        attempts: list[tuple[int, str]] = []
        attempts.append((0, "ChargePointMaxProfile"))
        if conn_id and conn_id > 0:
            attempts.append((conn_id, "TxDefaultProfile"))

        has_active = bool(getattr(self, "active_transaction_id", 0))
        if has_active:
            tx_conn = next(
                (c for c, tx in getattr(self, "_active_tx", {}).items() if tx),
                conn_id or 1,
            )
            attempts.append((tx_conn, "TxProfile"))

        await self.clear_profile(
            None, ChargingProfilePurposeType.charge_point_max_profile
        )
        if conn_id and conn_id > 0:
            await self.clear_profile(
                conn_id, ChargingProfilePurposeType.tx_default_profile
            )

        def _mk_profile(purpose: str, cid: int) -> dict:
            tx_id = (
                self.active_transaction_id
                if (purpose == "TxProfile" and has_active)
                else None
            )
            pid, stack = self._profile_ids_for(cid, purpose, tx_id=tx_id)
            return {
                om.charging_profile_id.value: pid,
                om.stack_level.value: stack,
                om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                om.charging_profile_purpose.value: purpose,
                om.charging_schedule.value: {
                    om.charging_rate_unit.value: unit_val,
                    om.charging_schedule_period.value: [
                        {om.start_period.value: 0, om.limit.value: limit_val}
                    ],
                },
            }

        # Try each purpose/connector in order; optionally clear-by-id before setting
        last_status = None
        for cid, purpose in attempts:
            try:
                try:
                    tx_id = (
                        self.active_transaction_id
                        if (purpose == "TxProfile" and has_active)
                        else None
                    )
                    pid, _ = self._profile_ids_for(cid, purpose, tx_id=tx_id)
                    await self.call(call.ClearChargingProfile(id=pid))
                except Exception:
                    pass

                req = call.SetChargingProfile(
                    connector_id=cid, cs_charging_profiles=_mk_profile(purpose, cid)
                )
                resp = await self.call(req)
                last_status = resp.status
                if resp.status == ChargingProfileStatus.accepted:
                    _LOGGER.debug(
                        "SetChargingProfile accepted with purpose=%s connectorId=%s",
                        purpose,
                        cid,
                    )
                    return True
                _LOGGER.debug(
                    "SetChargingProfile %s on connector %s -> %s",
                    purpose,
                    cid,
                    resp.status,
                )
            except Exception as ex:
                _LOGGER.debug(
                    "SetChargingProfile %s on connector %s raised %s", purpose, cid, ex
                )

        if last_status is not None:
            _LOGGER.warning("SetChargingProfile failed (last status=%s).", last_status)
            await self.notify_ha(
                f"SetChargingProfile failed (last status={last_status})."
            )
        return False

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
            return status in (
                AvailabilityStatus.accepted,
                AvailabilityStatus.scheduled,
            )
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

    async def stop_transaction(self):
        """Request remote stop of current transaction.

        Leaves charger in finishing state until unplugged.
        Use reset() to make the charger available again for remote start
        """
        if self.active_transaction_id == 0 and not any(self._active_tx.values()):
            return True
        tx_id = self.active_transaction_id or next(
            (v for v in self._active_tx.values() if v), 0
        )
        if tx_id == 0:
            return True
        req = call.RemoteStopTransaction(transaction_id=tx_id)
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True
        else:
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
        """Handle MeterValues (per connector).

        - EAIR **without** transactionId always writes to connector 0,
        even if it decreases relative to a previously mirrored value.
        - Tx-bound EAIR (with transactionId) writes to the specific connector using a non-decreasing rule,
        **except** when a new transaction begins on that connector — then lower values are allowed.
        - For single-connector chargers, mirror tx-bound EAIR to connector 0 **only** until a true
        main-meter (no txId) value is observed. After that, do not mirror.
        - Session energy is computed **only** from tx-bound EAIR, never from main-meter readings.
        """
        transaction_id: int | None = kwargs.get(om.transaction_id.name, None)
        tx_has_id: bool = transaction_id not in (None, 0)

        active_tx_for_conn: int | None = (
            int(self._active_tx.get(connector_id, 0) or 0) or None
        )

        # If missing meter_start or active_transaction_id try to restore from HA states. If HA
        # does not have values either, generate new ones.
        if self._metrics[(connector_id, csess.meter_start.value)].value is None:
            restored = self.get_ha_metric(csess.meter_start.value, connector_id)
            restored_f: float | None
            if restored is not None:
                try:
                    restored_f = float(restored)
                except (ValueError, TypeError):
                    restored_f = None
            else:
                # Fallback: if no txId and connector has a per-connector EAIR stored, use that
                restored_f = None
            if restored_f is not None:
                self._metrics[
                    (connector_id, csess.meter_start.value)
                ].value = restored_f

        if self._metrics[(connector_id, csess.transaction_id.value)].value is None:
            restored_tx = self.get_ha_metric(csess.transaction_id.value, connector_id)
            candidate: int | None
            if restored_tx is not None:
                try:
                    candidate = int(restored_tx)
                except (ValueError, TypeError):
                    candidate = None
            else:
                candidate = int(transaction_id) if tx_has_id else None

            if candidate is not None and candidate != 0:
                self._metrics[
                    (connector_id, csess.transaction_id.value)
                ].value = candidate
                self._active_tx[connector_id] = candidate
                active_tx_for_conn = candidate

        # --- Detect a new transaction on this connector (for example ABB resets to 0 at Transaction.Begin) ---
        new_tx_started = False
        if tx_has_id and (
            active_tx_for_conn is None or int(transaction_id) != int(active_tx_for_conn)
        ):
            # Register the new transaction and clear per-connector EAIR so that a lower starting
            # value (e.g., 0.0) is accepted.
            self._metrics[(connector_id, csess.transaction_id.value)].value = int(
                transaction_id
            )
            self._active_tx[connector_id] = int(transaction_id)
            active_tx_for_conn = int(transaction_id)
            new_tx_started = True
            # Reset tx-bound EAIR and session baseline; main meter (connector 0) remains untouched.
            self._metrics[(connector_id, DEFAULT_MEASURAND)].value = None
            self._metrics[(connector_id, csess.meter_start.value)].value = None

        if tx_has_id:
            transaction_matches = transaction_id == active_tx_for_conn
        else:
            transaction_matches = active_tx_for_conn not in (None, 0)

        meter_values: list[list[MeasurandValue]] = []
        for bucket in meter_value:
            measurands: list[MeasurandValue] = []
            for sampled_value in bucket.get(om.sampled_value.name, []):
                measurand = sampled_value.get(om.measurand.value, None)
                v = sampled_value.get(om.value.value, None)
                # where an empty string is supplied convert to 0
                try:
                    v = float(v)
                except (ValueError, TypeError):
                    v = 0.0
                unit = sampled_value.get(om.unit.value, None)
                phase = sampled_value.get(om.phase.value, None)
                location = sampled_value.get(om.location.value, None)
                context = sampled_value.get(om.context.value, None)
                measurands.append(
                    MeasurandValue(measurand, v, phase, unit, context, location)
                )
            meter_values.append(measurands)

        # --- Helper to rank contexts when multiple EAIR candidates exist in a bucket ---
        def _ctx_priority(ctx: str | None) -> int:
            if ctx == "Transaction.End":
                return 3
            if ctx == "Sample.Periodic":
                return 2
            if ctx == "Sample.Clock":
                return 1
            return 0

        # --- Step 1: Apply EAIR ---
        # target_cid = 0 (main meter) if no transactionId, else the connector itself
        target_cid = 0 if not tx_has_id else connector_id

        for bucket in meter_values:
            best_pr, best_val_kwh, best_item = -1, None, None
            for item in bucket:
                measurand = item.measurand or DEFAULT_MEASURAND
                if measurand != DEFAULT_MEASURAND:
                    continue
                # Ignore Transaction.Begin EAIR (often 0 right at start); ABB will be handled by new_tx_started
                if item.context == "Transaction.Begin":
                    continue
                try:
                    val_kwh = float(cp.get_energy_kwh(item))
                except Exception:
                    continue
                if val_kwh < 0.0 or (val_kwh != val_kwh):
                    continue
                pr = _ctx_priority(item.context)
                if (pr > best_pr) or (
                    pr == best_pr and (best_val_kwh is None or val_kwh > best_val_kwh)
                ):
                    best_pr, best_val_kwh, best_item = pr, val_kwh, item

            if best_item is None:
                continue

            if not tx_has_id and target_cid == 0:
                # Authoritative main meter: always write (can decrease vs a mirrored value)
                m = self._metrics[(0, DEFAULT_MEASURAND)]
                m.value = best_val_kwh
                m.unit = HA_ENERGY_UNIT
                m.extra_attr["source"] = "main"
                if best_item.context is not None:
                    m.extra_attr[om.context.value] = best_item.context
                if best_item.location is not None:
                    m.extra_attr[om.location.value] = best_item.location
            else:
                # Tx-bound EAIR: write non-decreasing, unless a new transaction just began
                m = self._metrics[(target_cid, DEFAULT_MEASURAND)]
                prev = m.value
                allow = new_tx_started or (prev is None or best_val_kwh >= float(prev))
                if allow:
                    m.value = best_val_kwh
                    m.unit = HA_ENERGY_UNIT
                    if best_item.context is not None:
                        m.extra_attr[om.context.value] = best_item.context
                    if best_item.location is not None:
                        m.extra_attr[om.location.value] = best_item.location

                # Mirror to connector 0 only for single-connector chargers, and only
                # until we've observed an authoritative main meter.
                try:
                    n_connectors = int(getattr(self, "num_connectors", 1) or 1)
                except Exception:
                    n_connectors = 1
                if n_connectors == 1:
                    mm = self._metrics[(0, DEFAULT_MEASURAND)]
                    main_seen = mm.extra_attr.get("source") == "main"
                    if not main_seen:
                        prev_main = mm.value
                        allow_main = new_tx_started or (
                            prev_main is None or best_val_kwh >= float(prev_main)
                        )
                        if allow_main:
                            mm.value = best_val_kwh
                            mm.unit = HA_ENERGY_UNIT
                            mm.extra_attr["source"] = "mirrored_tx"
                            if best_item.context is not None:
                                mm.extra_attr[om.context.value] = best_item.context
                            if best_item.location is not None:
                                mm.extra_attr[om.location.value] = best_item.location

        # --- Step 2: Process non-EAIR measurands via existing pipeline ---
        mv_wo_eair: list[list[MeasurandValue]] = []
        for bucket in meter_values:
            filtered = [
                it
                for it in bucket
                if (it.measurand or DEFAULT_MEASURAND) != DEFAULT_MEASURAND
            ]
            if filtered:
                mv_wo_eair.append(filtered)
        self.process_measurands(mv_wo_eair, transaction_matches, connector_id)

        # --- Step 3: Update session metrics (time, energy) only for tx-bound EAIR ---
        if tx_has_id and transaction_matches:
            # Session time (minutes) — keep parity with previous behavior
            tx_start = float(
                self._metrics[(connector_id, csess.transaction_id.value)].value
                or time.time()
            )
            self._metrics[(connector_id, csess.session_time.value)].value = round(
                (int(time.time()) - tx_start) / 60
            )
            self._metrics[
                (connector_id, csess.session_time.value)
            ].unit = UnitOfTime.MINUTES

            # Session energy from tx-bound EAIR only
            eair_kwh_in_msg: float | None = None
            best_ctx_prio = -1
            for bucket in meter_values:
                for item in bucket:
                    measurand = item.measurand or DEFAULT_MEASURAND
                    if measurand != DEFAULT_MEASURAND:
                        continue
                    if item.context == "Transaction.Begin":
                        continue
                    try:
                        val_kwh = float(cp.get_energy_kwh(item))
                    except Exception:
                        continue
                    if val_kwh < 0.0 or (val_kwh != val_kwh):
                        continue
                    pr = _ctx_priority(item.context)
                    if (pr > best_ctx_prio) or (
                        pr == best_ctx_prio
                        and (eair_kwh_in_msg is None or val_kwh > eair_kwh_in_msg)
                    ):
                        best_ctx_prio = pr
                        eair_kwh_in_msg = val_kwh

            if eair_kwh_in_msg is not None:
                raw_start = self._metrics[(connector_id, csess.meter_start.value)].value
                try:
                    meter_start_kwh = (
                        float(raw_start) if raw_start is not None else None
                    )
                except Exception:
                    meter_start_kwh = None

                if meter_start_kwh is None:
                    # Initialize at first tx-bound EAIR; ABB starts at 0 which is desired here
                    self._metrics[
                        (connector_id, csess.meter_start.value)
                    ].value = eair_kwh_in_msg
                    session_kwh = 0.0
                else:
                    session_kwh = max(0.0, eair_kwh_in_msg - meter_start_kwh)

                self._metrics[
                    (connector_id, csess.session_energy.value)
                ].value = session_kwh
                self._metrics[
                    (connector_id, csess.session_energy.value)
                ].unit = HA_ENERGY_UNIT

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

        if status == ChargePointStatus.available:
            self._metrics[(connector_id or 1, cstat.id_tag.value)].value = ""
            self._metrics[(connector_id or 1, csess.transaction_id.value)].value = 0

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
        """Stop the current transaction."""
        conn = next(
            (c for c, tx in self._active_tx.items() if tx == transaction_id), None
        )
        if conn is None:
            _LOGGER.error(
                "Stop transaction received for unknown transaction id=%i",
                transaction_id,
            )
            conn = 1

        self._active_tx[conn] = 0
        self.active_transaction_id = 0

        self._metrics[(conn, cstat.stop_reason.value)].value = kwargs.get(
            om.reason.name, None
        )

        self._metrics[(conn, cstat.id_tag.value)].value = ""
        self._metrics[(conn, csess.transaction_id.value)].value = 0

        use_eair_from_tx = bool(self._charger_reports_session_energy)

        if use_eair_from_tx:
            sess_val = self._metrics[(conn, csess.session_energy.value)].value
            if sess_val is None:
                last_eair = self._metrics[(conn, DEFAULT_MEASURAND)].value
                last_unit = self._metrics[(conn, DEFAULT_MEASURAND)].unit
                try:
                    if last_eair is not None:
                        if last_unit == DEFAULT_ENERGY_UNIT:
                            eair_kwh = float(last_eair) / 1000.0
                        elif last_unit == HA_ENERGY_UNIT:
                            eair_kwh = float(last_eair)
                        else:
                            eair_kwh = float(last_eair)
                        self._metrics[
                            (conn, csess.session_energy.value)
                        ].value = eair_kwh
                        self._metrics[
                            (conn, csess.session_energy.value)
                        ].unit = HA_ENERGY_UNIT
                except Exception:
                    pass
        else:
            try:
                meter_stop_kwh = float(meter_stop) / 1000.0
            except Exception:
                meter_stop_kwh = 0.0
            try:
                meter_start_kwh = float(
                    self._metrics[(conn, csess.meter_start.value)].value or 0.0
                )
            except Exception:
                meter_start_kwh = 0.0

            session_kwh = max(0.0, meter_stop_kwh - meter_start_kwh)
            self._metrics[(conn, csess.session_energy.value)].value = session_kwh
            self._metrics[(conn, csess.session_energy.value)].unit = HA_ENERGY_UNIT

        for meas in [
            Measurand.current_import.value,
            Measurand.power_active_import.value,
            Measurand.power_reactive_import.value,
            Measurand.current_export.value,
            Measurand.power_active_export.value,
            Measurand.power_reactive_export.value,
        ]:
            self._metrics[(conn, meas)].value = 0
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
