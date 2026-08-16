"""Representation of a OCPP Entities."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import ssl

from functools import partial
from homeassistant.config_entries import ConfigEntry, SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import STATE_OK, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from websockets import Subprotocol, NegotiationError
import websockets.server
from websockets.asyncio.server import ServerConnection

from .ocppv16 import ChargePoint as ChargePointv16
from .ocppv201 import ChargePoint as ChargePointv201

from .const import (
    CentralSystemSettings,
    DOMAIN,
    OCPP_1_6,
    OCPP_2_0,
    OCPP_VERSION_AUTO,
    ChargerSystemSettings,
)
from .enums import (
    HAChargerServices as csvcs,
    HAChargerStatuses as cstat,
)
from .chargepoint import SetVariableResult

_LOGGER: logging.Logger = logging.getLogger(__package__)
# Uncomment these when Debugging
# logging.getLogger("asyncio").setLevel(logging.DEBUG)
# logging.getLogger("websockets").setLevel(logging.DEBUG)

UFW_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("devid"): cv.string,
        vol.Required("firmware_url"): cv.string,
        vol.Optional("delay_hours"): cv.positive_int,
    }
)
CONF_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("devid"): cv.string,
        vol.Required("ocpp_key"): cv.string,
        vol.Required("value"): cv.string,
    }
)
GCONF_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("devid"): cv.string,
        vol.Optional("ocpp_key"): cv.string,
    }
)
GDIAG_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("devid"): cv.string,
        vol.Required("upload_url"): cv.string,
    }
)
TRANS_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("devid"): cv.string,
        vol.Required("vendor_id"): cv.string,
        vol.Optional("message_id"): cv.string,
        vol.Optional("data"): cv.string,
    }
)
CHRGR_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("devid"): cv.string,
        vol.Optional("limit_amps"): cv.positive_float,
        vol.Optional("limit_watts"): cv.positive_int,
        vol.Optional("conn_id"): cv.positive_int,
        vol.Optional("custom_profile"): vol.Any(cv.string, dict),
    }
)
CUSTMSG_SERVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("devid"): cv.string,
        vol.Required("requested_message"): cv.string,
    }
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Instantiate instance of a CentralSystem."""
        self.hass = hass
        self.entry = entry
        self.settings = CentralSystemSettings(**entry.data)
        self.subprotocols = self._resolve_subprotocols(self.settings)
        self._server = None
        self.id = self.settings.csid
        self.charge_points = {}  # uses cp_id as reference to charger instance
        self.cpids = {}  # dict of {cpid:cp_id}
        self.connections = 0
        # Whether async_setup_entry forwarded the entity platforms. Unload
        # has to mirror that exact decision - deriving it from anything
        # live (connection count, current entry data) diverges from what
        # setup actually did whenever state changed in between.
        self.platforms_forwarded = False

        # Register custom services with home assistant
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_configure.value,
            self.handle_configure,
            CONF_SERVICE_DATA_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_get_configuration.value,
            self.handle_get_configuration,
            GCONF_SERVICE_DATA_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_data_transfer.value,
            self.handle_data_transfer,
            TRANS_SERVICE_DATA_SCHEMA,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_trigger_custom_message.value,
            self.handle_trigger_custom_message,
            CUSTMSG_SERVICE_DATA_SCHEMA,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_clear_profile.value,
            self.handle_clear_profile,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_set_charge_rate.value,
            self.handle_set_charge_rate,
            CHRGR_SERVICE_DATA_SCHEMA,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_update_firmware.value,
            self.handle_update_firmware,
            UFW_SERVICE_DATA_SCHEMA,
        )
        self.hass.services.async_register(
            DOMAIN,
            csvcs.service_get_diagnostics.value,
            self.handle_get_diagnostics,
            GDIAG_SERVICE_DATA_SCHEMA,
        )

    @staticmethod
    async def create(hass: HomeAssistant, entry: ConfigEntry):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem(hass, entry)

        if self.settings.ssl:
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # see https://community.home-assistant.io/t/certificate-authority-and-self-signed-certificate-for-ssl-tls/196970
            localhost_certfile = self.settings.ssl_certfile_path
            localhost_keyfile = self.settings.ssl_keyfile_path
            await self.hass.async_add_executor_job(
                partial(
                    self.ssl_context.load_cert_chain,
                    localhost_certfile,
                    keyfile=localhost_keyfile,
                )
            )
        else:
            self.ssl_context = None

        server = await websockets.serve(
            self.on_connect,
            self.settings.host,
            self.settings.port,
            select_subprotocol=self.select_subprotocol,
            subprotocols=self.subprotocols,
            ping_interval=None,  # ping interval is not used here, because we send pings mamually in ChargePoint.monitor_connection()
            ping_timeout=None,
            close_timeout=self.settings.websocket_close_timeout,
            ssl=self.ssl_context,
        )
        self._server = server
        return self

    @staticmethod
    def _norm_conn(connector_id: int | None) -> int:
        if connector_id is None:
            return 0
        try:
            return int(connector_id)
        except Exception:
            return 0

    @staticmethod
    def _resolve_subprotocols(settings: CentralSystemSettings) -> list:
        """Return the websocket subprotocols the server should advertise.

        When the user pins an OCPP version (config-flow "OCPP version" field is
        not ``auto``), advertise only that version's subprotocol so a charger
        that offers several versions cannot negotiate the wrong one. ``auto``
        advertises everything the integration supports, preserving the previous
        behaviour.
        """
        ocpp_version = settings.ocpp_version
        if ocpp_version and ocpp_version != OCPP_VERSION_AUTO:
            return [f"ocpp{ocpp_version}"]
        return list(settings.subprotocols)

    def select_subprotocol(
        self, connection: ServerConnection, subprotocols
    ) -> Subprotocol | None:
        """Override default subprotocol selection."""

        _LOGGER.debug("Charger offered subprotocols: %s", list(subprotocols or []))

        # Returning None here (rather than raising, as the websockets default
        # does) is a deliberate deviation that lets a charger offering no
        # subprotocol default to OCPP 1.6, and it is kept. It is only valid
        # while the server is actually willing to talk 1.6: when the version is
        # pinned to 2.x, accepting such a connection would create a v1.6
        # ChargePoint that then poisons the charge point cache for the
        # follow-up connection, so reject instead.
        if not subprotocols:
            if OCPP_1_6 in self.subprotocols:
                return None
            raise NegotiationError(
                "no subprotocol offered; expected one of "
                + ", ".join(self.subprotocols)
            )

        # Server and client both offer subprotocols. Look for a shared one,
        # iterating the server's ordered list - the documented websockets
        # behaviour this function overrides ("pick the first one in the list
        # declared the server"). This loop previously iterated the client's
        # offer as a set instead, so for a charger offering several
        # subprotocols the negotiated version depended on set-iteration order
        # and varied between handshakes. Pinning an OCPP version leaves a
        # single entry in self.subprotocols, which is how a charger is held to
        # a version other than the default preference.
        proposed_subprotocols = set(subprotocols)
        for subprotocol in self.subprotocols:
            if subprotocol in proposed_subprotocols:
                return subprotocol

        # No common subprotocol was found.
        raise NegotiationError(
            "invalid subprotocol; expected one of " + ", ".join(self.subprotocols)
        )

    @staticmethod
    def _negotiated_ocpp_version(websocket: ServerConnection) -> str:
        """Return the ocpp version string implied by the negotiated subprotocol.

        Mirrors how ``ChargePoint._ocpp_version`` is derived in the v16/v201
        subclasses: ``ocpp1.6`` -> ``1.6``, ``ocpp2.0.1`` -> ``2.0.1``,
        ``ocpp2.1`` -> ``2.1``. A charger that offers no subprotocol defaults
        to 1.6.
        """
        subprotocol = websocket.subprotocol
        if not subprotocol:
            return "1.6"
        return subprotocol.replace("ocpp", "")

    def _build_charge_point(self, cp_id: str, websocket: ServerConnection, cp_settings):
        """Construct the ChargePoint matching this connection's subprotocol."""
        if websocket.subprotocol and websocket.subprotocol.startswith(OCPP_2_0):
            return ChargePointv201(
                cp_id, websocket, self.hass, self.entry, self.settings, cp_settings
            )
        return ChargePointv16(
            cp_id, websocket, self.hass, self.entry, self.settings, cp_settings
        )

    async def on_connect(self, websocket: ServerConnection):
        """Request handler executed for every new OCPP connection."""
        if websocket.subprotocol is not None:
            _LOGGER.info("Websocket Subprotocol matched: %s", websocket.subprotocol)
        else:
            _LOGGER.info(
                "Websocket Subprotocol not provided by charger: default to ocpp1.6"
            )

        _LOGGER.info(f"Charger websocket path={websocket.request.path}")
        cp_id = websocket.request.path.strip("/")
        cp_id = cp_id[cp_id.rfind("/") + 1 :]
        if cp_id not in self.charge_points:
            try:
                config_flow = False
                for cfg in self.settings.cpids:
                    if cfg.get(cp_id):
                        config_flow = True
                        cp_settings = ChargerSystemSettings(**list(cfg.values())[0])
                        _LOGGER.info(
                            f"Charger match found for {cp_settings.cpid}:{cp_id}"
                        )
                        _LOGGER.debug(f"Central settings: {self.settings}")

                if not config_flow:
                    # discovery_info for flow
                    info = {"cp_id": cp_id, "entry": self.entry}
                    await self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": SOURCE_INTEGRATION_DISCOVERY},
                        data=info,
                    )
                    # use return to wait for config entry to reload after discovery
                    return

                self.cpids.update({cp_settings.cpid: cp_id})
            except Exception as e:
                _LOGGER.error(f"Failed to setup charger {cp_id}: {str(e)}")
                return

            charge_point = self._build_charge_point(cp_id, websocket, cp_settings)
            self.charge_points[cp_id] = charge_point
            self.connections += 1
            _LOGGER.info(
                f"Charger {cp_settings.cpid}:{cp_id} connected to {self.settings.host}:{self.settings.port}."
            )
            _LOGGER.info(
                f"{self.connections} charger(s): {self.cpids} now connected to central system:{self.settings.csid}."
            )
            await charge_point.start()
        else:
            charge_point = self.charge_points[cp_id]
            negotiated_version = self._negotiated_ocpp_version(websocket)
            if negotiated_version != charge_point._ocpp_version:
                # The charger reconnected negotiating a different OCPP version
                # than the cached ChargePoint was built with. The cached object
                # carries a fixed validator / message set / entity model, so
                # reusing it would validate the new version's payloads against
                # the old version's schema (e.g. a 2.0.1 BootNotification
                # against the 1.6 schema), raising FormatViolationError on
                # every reconnect until the config entry is reloaded. Some
                # chargers (e.g. FoxESS A-series) make a short-lived 1.6 probe
                # connection right after a version switch, which is exactly
                # what plants the mismatched object. Rebuild the ChargePoint
                # from this handshake's negotiated subprotocol instead. See
                # issue #2008.
                _LOGGER.info(
                    f"Charger {cp_id} reconnected with a different OCPP version "
                    f"({charge_point._ocpp_version} -> {negotiated_version}); "
                    f"rebuilding charge point."
                )
                # stop() closes the websocket before cancelling its tasks, so a
                # failure to close would otherwise leave the stale instance's
                # monitor_connection running against a charger that has already
                # been replaced here - duplicate pings and metric writes.
                # Cancel them explicitly if stop() does not get that far.
                try:
                    await charge_point.stop()
                except Exception:
                    _LOGGER.debug(
                        "Error stopping stale charge point %s during rebuild; "
                        "cancelling its tasks directly",
                        cp_id,
                        exc_info=True,
                    )
                    for task in getattr(charge_point, "tasks", None) or []:
                        task.cancel()
                charge_point = self._build_charge_point(
                    cp_id, websocket, charge_point.settings
                )
                self.charge_points[cp_id] = charge_point
                await charge_point.start()
            else:
                _LOGGER.info(
                    f"Charger {cp_id} reconnected to {self.settings.host}:{self.settings.port}."
                )
                await charge_point.reconnect(websocket)

    def _get_metrics(self, id: str):
        """Return (cp_id, metrics mapping, cp instance, safe int num_connectors)."""
        cp_id = self.cpids.get(id, id)
        cp = self.charge_points.get(cp_id)

        def _safe_int(value, default=1):
            try:
                iv = int(value)
                return iv if iv > 0 else default
            except Exception:
                return default

        n_connectors = _safe_int(getattr(cp, "num_connectors", 1), default=1)

        return (
            (cp_id, cp._metrics, cp, n_connectors)
            if cp is not None
            else (None, None, None, None)
        )

    def get_metric(self, id: str, measurand: str, connector_id: int | None = None):
        """Return last known value for given measurand."""
        cp_id, m, cp, n_connectors = self._get_metrics(id)
        if cp is None:
            return None

        def _try_val(key):
            with contextlib.suppress(Exception):
                val = m[key].value
                return val
            return None

        # 1) Explicit connector_id (including 0): just get it
        if connector_id is not None:
            conn = self._norm_conn(connector_id)
            return _try_val((conn, measurand))

        # 2) No connector_id: try CHARGER level (conn=0)
        val = _try_val((0, measurand))
        if val is not None:
            return val

        # 3) Legacy "flat" key (before the connector support)
        with contextlib.suppress(Exception):
            val = m[measurand].value
            if val is not None:
                return val

        # 4) Fallback to connector 1 (old tests often expect this)
        if n_connectors >= 1:
            val = _try_val((1, measurand))
            if val is not None:
                return val

        # 5) Last resort: find the first connector 2..N with value
        for c in range(2, int(n_connectors) + 1):
            val = _try_val((c, measurand))
            if val is not None:
                return val

        return None

    def del_metric(self, id: str, measurand: str, connector_id: int | None = None):
        """Set given measurand to None."""
        cp_id, m, cp, n_connectors = self._get_metrics(id)

        if m is None:
            return None

        conn = self._norm_conn(connector_id)
        try:
            m[(conn, measurand)].value = None
        except Exception:
            if conn == 0:
                with contextlib.suppress(Exception):
                    m[measurand].value = None
        return None

    def get_unit(self, id: str, measurand: str, connector_id: int | None = None):
        """Return unit of given measurand."""
        cp_id, m, cp, n_connectors = self._get_metrics(id)

        if cp is None:
            return None

        def _try_unit(key):
            with contextlib.suppress(Exception):
                val = m[key].unit
                if isinstance(val, str) and val.strip() == "":
                    return None
                return val
            return None

        if connector_id is not None:
            conn = self._norm_conn(connector_id)
            return _try_unit((conn, measurand))

        val = _try_unit((0, measurand))
        if val is not None:
            return val

        with contextlib.suppress(Exception):
            val = m[measurand].unit
            if isinstance(val, str) and val.strip() == "":
                val = None
            if val is not None:
                return val

        if n_connectors >= 1:
            val = _try_unit((1, measurand))
            if val is not None:
                return val

        for c in range(2, int(n_connectors) + 1):
            val = _try_unit((c, measurand))
            if val is not None:
                return val

        return None

    def get_ha_unit(self, id: str, measurand: str, connector_id: int | None = None):
        """Return home assistant unit of given measurand."""
        cp_id, m, cp, n_connectors = self._get_metrics(id)

        if cp is None:
            return None

        def _try_ha_unit(key):
            with contextlib.suppress(Exception):
                val = m[key].ha_unit
                if isinstance(val, str) and val.strip() == "":
                    return None
                return val
            return None

        if connector_id is not None:
            conn = self._norm_conn(connector_id)
            return _try_ha_unit((conn, measurand))

        val = _try_ha_unit((0, measurand))
        if val is not None:
            return val

        with contextlib.suppress(Exception):
            val = m[measurand].ha_unit
            if isinstance(val, str) and val.strip() == "":
                val = None
            if val is not None:
                return val

        if n_connectors >= 1:
            val = _try_ha_unit((1, measurand))
            if val is not None:
                return val

        for c in range(2, int(n_connectors) + 1):
            val = _try_ha_unit((c, measurand))
            if val is not None:
                return val

        return None

    def get_extra_attr(self, id: str, measurand: str, connector_id: int | None = None):
        """Return extra attributes for given measurand."""
        cp_id, m, cp, n_connectors = self._get_metrics(id)

        if cp is None:
            return None

        def _try_extra(key):
            with contextlib.suppress(Exception):
                val = m[key].extra_attr
                if isinstance(val, dict) and not val:
                    return None
                return val
            return None

        if connector_id is not None:
            conn = self._norm_conn(connector_id)
            return _try_extra((conn, measurand))

        val = _try_extra((0, measurand))
        if val is not None:
            return val

        with contextlib.suppress(Exception):
            val = m[measurand].extra_attr
            if isinstance(val, dict) and not val:
                val = None
            if val is not None:
                return val

        if n_connectors >= 1:
            val = _try_extra((1, measurand))
            if val is not None:
                return val

        for c in range(2, int(n_connectors) + 1):
            val = _try_extra((c, measurand))
            if val is not None:
                return val

        return None

    def get_available(self, id: str, connector_id: int | None = None):
        """Return whether the charger (or a specific connector) is available."""
        cp_id, m, cp, n_connectors = self._get_metrics(id)

        if cp is None:
            return None

        if self._norm_conn(connector_id) == 0:
            return cp.status == STATE_OK

        status_val = None
        with contextlib.suppress(Exception):
            status_val = m[
                (self._norm_conn(connector_id), cstat.status_connector.value)
            ].value

        if not status_val:
            try:
                flat = m[cstat.status_connector.value]
                if hasattr(flat, "extra_attr"):
                    status_val = flat.extra_attr.get(
                        self._norm_conn(connector_id)
                    ) or getattr(flat, "value", None)
            except Exception:
                pass

        if not status_val:
            return cp.status == STATE_OK

        ok_statuses_norm = {
            "available",
            "preparing",
            "charging",
            "suspendedev",
            "suspendedevse",
            "finishing",
            "occupied",
            "reserved",
            "unavailable",  # do NOT make HA entities unavailable for this OCPP state
        }

        ret = _norm(status_val) in ok_statuses_norm
        # If backend/WS is down, entity should be unavailable regardless.
        return ret and (cp.status == STATE_OK)

    def get_supported_features(self, id: str):
        """Return what profiles the charger supports."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id, id)

        if cp_id in self.charge_points:
            return self.charge_points[cp_id].supported_features
        return 0

    async def set_max_charge_rate_amps(
        self, id: str, value: float, connector_id: int = 0
    ):
        """Set the maximum charge rate in amps."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id, id)

        if cp_id in self.charge_points:
            return await self.charge_points[cp_id].set_charge_rate(
                limit_amps=value, conn_id=connector_id
            )
        return False

    async def set_charger_state(
        self,
        id: str,
        service_name: str,
        state: bool = True,
        connector_id: int | None = 1,
    ):
        """Carry out requested service/state change on connected charger."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id, id)

        resp = False
        if cp_id in self.charge_points:
            if service_name == csvcs.service_availability.name:
                resp = await self.charge_points[cp_id].set_availability(
                    state, connector_id=connector_id
                )
            if service_name == csvcs.service_charge_start.name:
                resp = await self.charge_points[cp_id].start_transaction(
                    connector_id=connector_id
                )
            if service_name == csvcs.service_charge_stop.name:
                resp = await self.charge_points[cp_id].stop_transaction(
                    connector_id=connector_id
                )
            if service_name == csvcs.service_reset.name:
                resp = await self.charge_points[cp_id].reset()
            if service_name == csvcs.service_unlock.name:
                resp = await self.charge_points[cp_id].unlock(connector_id=connector_id)
        return resp

    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.id)},
        }

    def check_charger_available(func):
        """Check charger is available before executing service with Decorator."""

        async def wrapper(self, call, *args, **kwargs):
            try:
                cp_id = self.cpids.get(call.data["devid"], call.data["devid"])
                cp = self.charge_points[cp_id]
            except KeyError:
                cp = list(self.charge_points.values())[0]
            if cp.status == STATE_UNAVAILABLE:
                _LOGGER.warning(f"{cp_id}: charger is currently unavailable")
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="unavailable",
                    translation_placeholders={"message": cp_id},
                )
            return await func(self, call, cp, *args, **kwargs)

        return wrapper

    # Define custom service handles for charge point
    @check_charger_available
    async def handle_trigger_custom_message(self, call, cp):
        """Handle the message request with a custom message."""
        requested_message = call.data.get("requested_message")
        await cp.trigger_custom_message(requested_message)

    @check_charger_available
    async def handle_clear_profile(self, call, cp):
        """Handle the clear profile service call."""
        await cp.clear_profile()

    @check_charger_available
    async def handle_update_firmware(self, call, cp):
        """Handle the firmware update service call."""
        url = call.data.get("firmware_url")
        delay = int(call.data.get("delay_hours", 0))
        await cp.update_firmware(url, delay)

    @check_charger_available
    async def handle_get_diagnostics(self, call, cp):
        """Handle the get get diagnostics service call."""
        url = call.data.get("upload_url")
        await cp.get_diagnostics(url)

    @check_charger_available
    async def handle_data_transfer(self, call, cp):
        """Handle the data transfer service call."""
        vendor = call.data.get("vendor_id")
        message = call.data.get("message_id", "")
        data = call.data.get("data", "")
        await cp.data_transfer(vendor, message, data)

    @check_charger_available
    async def handle_set_charge_rate(self, call, cp):
        """Handle the data transfer service call."""
        amps = call.data.get("limit_amps", None)
        watts = call.data.get("limit_watts", None)
        id = call.data.get("conn_id", 0)
        custom_profile = call.data.get("custom_profile", None)
        if custom_profile is not None:
            if type(custom_profile) is str:
                custom_profile = custom_profile.replace("'", '"')
                custom_profile = json.loads(custom_profile)
            await cp.set_charge_rate(profile=custom_profile, conn_id=id)
        elif watts is not None:
            await cp.set_charge_rate(limit_watts=watts, conn_id=id)
        elif amps is not None:
            await cp.set_charge_rate(limit_amps=amps, conn_id=id)

    @check_charger_available
    async def handle_configure(self, call, cp) -> ServiceResponse:
        """Handle the configure service call."""
        key = call.data.get("ocpp_key")
        value = call.data.get("value")
        result: SetVariableResult = await cp.configure(key, value)
        return {"reboot_required": result == SetVariableResult.reboot_required}

    @check_charger_available
    async def handle_get_configuration(self, call, cp) -> ServiceResponse:
        """Handle the get configuration service call."""
        key = call.data.get("ocpp_key", "")
        value = await cp.get_configuration(key)
        return {"value": value}
