"""Representation of a OCPP 1.6 charging station."""

from datetime import datetime, timedelta, UTC
import logging

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


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

    async def get_number_of_connectors(self):
        """Return number of connectors on this charger."""
        return await self.get_configuration(ckey.number_of_connectors.value)

    async def get_heartbeat_interval(self):
        """Retrieve heartbeat interval from the charger and store it."""
        await self.get_configuration(ckey.heartbeat_interval.value)

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""
        all_measurands = self.settings.monitored_variables
        autodetect_measurands = self.settings.monitored_variables_autoconfig

        key = ckey.meter_values_sampled_data.value

        if autodetect_measurands:
            accepted_measurands = []
            cfg_ok = [
                ConfigurationStatus.accepted,
                ConfigurationStatus.reboot_required,
            ]

            for measurand in all_measurands.split(","):
                _LOGGER.debug(f"'{self.id}' trying measurand: '{measurand}'")
                req = call.ChangeConfiguration(key=key, value=measurand)
                resp = await self.call(req)
                if resp.status in cfg_ok:
                    _LOGGER.debug(f"'{self.id}' adding measurand: '{measurand}'")
                    accepted_measurands.append(measurand)

            accepted_measurands = ",".join(accepted_measurands)
        else:
            accepted_measurands = all_measurands

            # Quirk:
            # Workaround for a bug on chargers that have invalid MeterValuesSampledData
            # configuration and reboot while the server requests MeterValuesSampledData.
            # By setting the configuration directly without checking current configuration
            # as done when calling self.configure, the server avoids charger reboot.
            # Corresponding issue: https://github.com/lbbrhzn/ocpp/issues/1275
            if len(accepted_measurands) > 0:
                req = call.ChangeConfiguration(key=key, value=accepted_measurands)
                resp = await self.call(req)
                _LOGGER.debug(
                    f"'{self.id}' measurands set manually to {accepted_measurands}"
                )

        chgr_measurands = await self.get_configuration(key)

        if len(accepted_measurands) > 0:
            _LOGGER.debug(f"'{self.id}' allowed measurands: '{accepted_measurands}'")
            await self.configure(key, accepted_measurands)
        else:
            _LOGGER.debug(f"'{self.id}' measurands not configurable by integration")
            _LOGGER.debug(f"'{self.id}' allowed measurands: '{chgr_measurands}'")

        return accepted_measurands

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
        return_value = True
        try:
            nof_connectors = int(self._metrics[0][cdet.connectors.value].value or 1)
        except Exception:
            nof_connectors = 1
        for cid in range(0, nof_connectors + 1):
            _LOGGER.debug(f"trigger status notification for connector={cid}")
            req = call.TriggerMessage(
                requested_message=MessageTrigger.status_notification,
                connector_id=int(cid),
            )
            resp = await self.call(req)
            if resp.status != TriggerMessageStatus.accepted:
                _LOGGER.warning("Failed with response: %s", resp.status)
                _LOGGER.warning(
                    "Forcing number of connectors to %d, charger returned %d",
                    cid - 1,
                    nof_connectors,
                )
                self._metrics[0][cdet.connectors.value].value = max(1, cid - 1)
                return_value = cid > 1
                break
        return return_value

    async def trigger_custom_message(
        self,
        requested_message: str = "StatusNotification",
    ):
        """Trigger Custom Message."""
        req = call.TriggerMessage(requested_message)
        resp = await self.call(req)
        if resp.status != TriggerMessageStatus.accepted:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False
        return True

    async def clear_profile(self):
        """Clear all charging profiles."""
        req = call.ClearChargingProfile()
        resp = await self.call(req)
        if resp.status == ClearChargingProfileStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Clear profile failed with response {resp.status}"
            )
            return False

    async def set_charge_rate(
        self,
        limit_amps: int = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
    ):
        """Set a charging profile with defined limit."""
        if profile is not None:  # assumes advanced user and correct profile format
            req = call.SetChargingProfile(
                connector_id=conn_id, cs_charging_profiles=profile
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                return True
            else:
                _LOGGER.warning("Failed with response: %s", resp.status)
                await self.notify_ha(
                    f"Warning: Set charging profile failed with response {resp.status}"
                )
                return False

        if prof.SMART in self._attr_supported_features:
            resp = await self.get_configuration(
                ckey.charging_schedule_allowed_charging_rate_unit.value
            )
            _LOGGER.info(
                "Charger supports setting the following units: %s",
                resp,
            )
            _LOGGER.info("If more than one unit supported default unit is Amps")
            # Some chargers (e.g. Teison) don't support querying charging rate unit
            if resp is None:
                _LOGGER.warning("Failed to query charging rate unit, assuming Amps")
                resp = om.current.value
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
            req = call.SetChargingProfile(
                connector_id=conn_id,
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
            _LOGGER.debug(
                "ChargePointMaxProfile is not supported by this charger, trying TxDefaultProfile instead..."
            )
            # try a lower stack level for chargers where level < maximum, not <=
            req = call.SetChargingProfile(
                connector_id=conn_id,
                cs_charging_profiles={
                    om.charging_profile_id.value: 8,
                    om.stack_level.value: stack_level - 1,
                    om.charging_profile_kind.value: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose.value: ChargingProfilePurposeType.tx_default_profile.value,
                    om.charging_schedule.value: {
                        om.charging_rate_unit.value: units,
                        om.charging_schedule_period.value: [
                            {om.start_period.value: 0, om.limit.value: lim}
                        ],
                    },
                },
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                return True
            else:
                _LOGGER.warning("Failed with response: %s", resp.status)
                await self.notify_ha(
                    f"Warning: Set charging profile failed with response {resp.status}"
                )
                return False

    async def set_availability(self, state: bool = True):
        """Change availability."""
        if state is True:
            typ = AvailabilityType.operative.value
        else:
            typ = AvailabilityType.inoperative.value

        req = call.ChangeAvailability(connector_id=0, type=typ)
        resp = await self.call(req)
        if resp.status in [
            AvailabilityStatus.accepted,
            AvailabilityStatus.scheduled,
        ]:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Set availability failed with response {resp.status}"
            )
            return False

    async def start_transaction(self):
        """Remote start a transaction."""
        _LOGGER.info("Start transaction with remote ID tag: %s", self._remote_id_tag)
        req = call.RemoteStartTransaction(connector_id=1, id_tag=self._remote_id_tag)
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
        """Update charger with new firmware if available."""
        """where firmware_url is the http or https url of the new firmware"""
        """and wait_time is hours from now to wait before install"""
        if prof.FW in self._attr_supported_features:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(firmware_url)
            except vol.MultipleInvalid as e:
                _LOGGER.debug("Failed to parse url: %s", e)
            update_time = (datetime.now(tz=UTC) + timedelta(hours=wait_time)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            req = call.UpdateFirmware(location=url, retrieve_date=update_time)
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
            req = call.GetDiagnostics(location=url)
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.warning("Charger does not support ocpp diagnostics uploading")
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
        """Request handler for MeterValues Calls."""
        transaction_id: int = kwargs.get(om.transaction_id.name, 0)
        active_tx_for_conn = self._active_tx.get(connector_id, 0)

        # If missing meter_start or active_transaction_id try to restore from HA states. If HA
        # does not have values either, generate new ones.
        if self._metrics[connector_id][csess.meter_start.value].value is None:
            value = self.get_ha_metric(csess.meter_start.value)
            if value is None:
                value = self._metrics[connector_id][DEFAULT_MEASURAND].value
            else:
                value = float(value)
                _LOGGER.debug(
                    f"{csess.meter_start.value} was None, restored value={value} from HA."
                )
            self._metrics[connector_id][csess.meter_start.value].value = value
        if self._metrics[connector_id][csess.transaction_id.value].value is None:
            value = self.get_ha_metric(csess.transaction_id.value)
            if value is None:
                candidate = transaction_id or active_tx_for_conn or None
            else:
                candidate = int(value)

            if candidate is not None and candidate != 0:
                self._metrics[connector_id][
                    csess.transaction_id.value
                ].value = candidate
                self._active_tx[connector_id] = candidate

        transaction_matches = (
            transaction_id != 0 and transaction_id == active_tx_for_conn
        )

        meter_values: list[list[MeasurandValue]] = []
        for bucket in meter_value:
            measurands: list[MeasurandValue] = []
            for sampled_value in bucket[om.sampled_value.name]:
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
        self.process_measurands(meter_values, transaction_matches, connector_id)

        if transaction_matches:
            tx_start = float(
                self._metrics[connector_id][csess.transaction_id.value].value
                or time.time()
            )
            self._metrics[connector_id][csess.session_time.value].value = round(
                (int(time.time()) - tx_start) / 60
            )
            self._metrics[connector_id][csess.session_time.value].unit = "min"
            if (
                self._metrics[connector_id][csess.meter_start.value].value is not None
                and not self._charger_reports_session_energy
            ):
                current_total = float(
                    self._metrics[connector_id][DEFAULT_MEASURAND].value or 0
                )
                meter_start = float(
                    self._metrics[connector_id][csess.meter_start.value].value or 0
                )
                self._metrics[connector_id][csess.session_energy.value].value = (
                    current_total - meter_start
                )
                self._metrics[connector_id][csess.session_energy.value].extra_attr[
                    cstat.id_tag.name
                ] = self._metrics[connector_id][cstat.id_tag.value].value

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
            self._metrics[0][cstat.status.value].value = status
            self._metrics[0][cstat.error_code.value].value = error_code
        else:
            self._metrics[connector_id][cstat.status_connector.value].value = status
            self._metrics[connector_id][
                cstat.error_code_connector.value
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
                        self._metrics[connector_id][meas].value = 0

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
            self._metrics[connector_id][cstat.id_tag.value].value = id_tag
            self._metrics[connector_id][cstat.stop_reason.value].value = ""
            self._metrics[connector_id][csess.transaction_id.value].value = tx_id
            self._metrics[connector_id][csess.meter_start.value].value = (
                int(meter_start) / 1000
            )

            result = call_result.StartTransaction(
                id_tag_info={om.status.value: AuthorizationStatus.accepted.value},
                transaction_id=tx_id,
            )
        else:
            result = call_result.StartTransaction(
                id_tag_info={om.status.value: auth_status}, transaction_id=0
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

        self._metrics[conn][cstat.stop_reason.value].value = kwargs.get(
            om.reason.name, None
        )

        if (
            self._metrics[conn][csess.meter_start.value].value is not None
            and not self._charger_reports_session_energy
        ):
            start_kwh = float(self._metrics[conn][csess.meter_start.value].value or 0)
            stop_kwh = int(meter_stop) / 1000.0
            self._metrics[conn][csess.session_energy.value].value = stop_kwh - start_kwh

        for meas in [
            Measurand.current_import.value,
            Measurand.power_active_import.value,
            Measurand.power_reactive_import.value,
            Measurand.current_export.value,
            Measurand.power_active_export.value,
            Measurand.power_reactive_export.value,
        ]:
            self._metrics[conn][meas].value = 0
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
