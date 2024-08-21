"""Representation of a OCCP Entities."""

from __future__ import annotations

import logging
import ssl

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OK
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_component, entity_registry
import homeassistant.helpers.config_validation as cv
from websockets import Subprotocol

from .ocppv16 import ChargePoint as ChargePointv16
import voluptuous as vol
import websockets.protocol
import websockets.server

from .const import (
    CONF_AUTH_LIST,
    CONF_AUTH_STATUS,
    CONF_CPID,
    CONF_CSID,
    CONF_DEFAULT_AUTH_STATUS,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_ID_TAG,
    CONF_IDLE_INTERVAL,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_PORT,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_SUBPROTOCOL,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    CONFIG,
    DEFAULT_CPID,
    DEFAULT_CSID,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_FORCE_SMART_CHARGING,
    DEFAULT_HOST,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MEASURAND,
    DEFAULT_METER_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_POWER_UNIT,
    DEFAULT_SKIP_SCHEMA_VALIDATION,
    DEFAULT_SSL,
    DEFAULT_SSL_CERTFILE_PATH,
    DEFAULT_SSL_KEYFILE_PATH,
    DEFAULT_SUBPROTOCOL,
    DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_INTERVAL,
    DEFAULT_WEBSOCKET_PING_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_TRIES,
    DOMAIN,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    OCPP_2_0,
)
from .enums import (
    HAChargerServices as csvcs,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)
# Uncomment these when Debugging
# logging.getLogger("asyncio").setLevel(logging.DEBUG)
# logging.getLogger("websockets").setLevel(logging.DEBUG)

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


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Instantiate instance of a CentralSystem."""
        self.hass = hass
        self.entry = entry
        self.host = entry.data.get(CONF_HOST, DEFAULT_HOST)
        self.port = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self.csid = entry.data.get(CONF_CSID, DEFAULT_CSID)
        self.cpid = entry.data.get(CONF_CPID, DEFAULT_CPID)
        self.websocket_close_timeout = entry.data.get(
            CONF_WEBSOCKET_CLOSE_TIMEOUT, DEFAULT_WEBSOCKET_CLOSE_TIMEOUT
        )
        self.websocket_ping_tries = entry.data.get(
            CONF_WEBSOCKET_PING_TRIES, DEFAULT_WEBSOCKET_PING_TRIES
        )
        self.websocket_ping_interval = entry.data.get(
            CONF_WEBSOCKET_PING_INTERVAL, DEFAULT_WEBSOCKET_PING_INTERVAL
        )
        self.websocket_ping_timeout = entry.data.get(
            CONF_WEBSOCKET_PING_TIMEOUT, DEFAULT_WEBSOCKET_PING_TIMEOUT
        )

        self.subprotocols: list[Subprotocol] = entry.data.get(
            CONF_SUBPROTOCOL, DEFAULT_SUBPROTOCOL
        ).split(",")
        self._server = None
        self.config = entry.data
        self.id = entry.entry_id
        self.charge_points = {}
        if entry.data.get(CONF_SSL, DEFAULT_SSL):
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # see https://community.home-assistant.io/t/certificate-authority-and-self-signed-certificate-for-ssl-tls/196970
            localhost_certfile = entry.data.get(
                CONF_SSL_CERTFILE_PATH, DEFAULT_SSL_CERTFILE_PATH
            )
            localhost_keyfile = entry.data.get(
                CONF_SSL_KEYFILE_PATH, DEFAULT_SSL_KEYFILE_PATH
            )
            self.ssl_context.load_cert_chain(
                localhost_certfile, keyfile=localhost_keyfile
            )
        else:
            self.ssl_context = None

    @staticmethod
    async def create(hass: HomeAssistant, entry: ConfigEntry):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem(hass, entry)

        server = await websockets.server.serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=self.subprotocols,
            ping_interval=None,  # ping interval is not used here, because we send pings mamually in ChargePoint.monitor_connection()
            ping_timeout=None,
            close_timeout=self.websocket_close_timeout,
            ssl=self.ssl_context,
        )
        self._server = server
        return self

    async def on_connect(self, websocket: websockets.server.WebSocketServerProtocol):
        """Request handler executed for every new OCPP connection."""
        if self.config.get(CONF_SKIP_SCHEMA_VALIDATION, DEFAULT_SKIP_SCHEMA_VALIDATION):
            _LOGGER.warning("Skipping websocket subprotocol validation")
        else:
            if websocket.subprotocol is not None:
                _LOGGER.info("Websocket Subprotocol matched: %s", websocket.subprotocol)
            else:
                # In the websockets lib if no subprotocols are supported by the
                # client and the server, it proceeds without a subprotocol,
                # so we have to manually close the connection.
                _LOGGER.warning(
                    "Protocols mismatched | expected Subprotocols: %s,"
                    " but client supports  %s | Closing connection",
                    websocket.available_subprotocols,
                    websocket.request_headers.get("Sec-WebSocket-Protocol", ""),
                )
                return await websocket.close()

        _LOGGER.info(f"Charger websocket path={websocket.path}")
        cp_id = websocket.path.strip("/")
        cp_id = cp_id[cp_id.rfind("/") + 1 :]
        if self.cpid not in self.charge_points:
            _LOGGER.info(f"Charger {cp_id} connected to {self.host}:{self.port}.")
            if websocket.subprotocol and websocket.subprotocol.startswith(OCPP_2_0):
                _LOGGER.warning("OCPP 2.0 not implemented")
                return await websocket.close()
            else:
                charge_point = ChargePointv16(
                    cp_id, websocket, self.hass, self.entry, self
                )
            self.charge_points[self.cpid] = charge_point
            await charge_point.start()
        else:
            _LOGGER.info(f"Charger {cp_id} reconnected to {self.host}:{self.port}.")
            charge_point = self.charge_points[self.cpid]
            await charge_point.reconnect(websocket)
        _LOGGER.info(f"Charger {cp_id} disconnected from {self.host}:{self.port}.")

    def get_metric(self, cp_id: str, measurand: str):
        """Return last known value for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].value
        return None

    def del_metric(self, cp_id: str, measurand: str):
        """Set given measurand to None."""
        if cp_id in self.charge_points:
            self.charge_points[cp_id]._metrics[measurand].value = None
        return None

    def get_unit(self, cp_id: str, measurand: str):
        """Return unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].unit
        return None

    def get_ha_unit(self, cp_id: str, measurand: str):
        """Return home assistant unit of given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].ha_unit
        return None

    def get_extra_attr(self, cp_id: str, measurand: str):
        """Return last known extra attributes for given measurand."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].extra_attr
        return None

    def get_available(self, cp_id: str):
        """Return whether the charger is available."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].status == STATE_OK
        return False

    def get_supported_features(self, cp_id: str):
        """Return what profiles the charger supports."""
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].supported_features
        return 0

    async def set_max_charge_rate_amps(self, cp_id: str, value: float):
        """Set the maximum charge rate in amps."""
        if cp_id in self.charge_points:
            return await self.charge_points[cp_id].set_charge_rate(limit_amps=value)
        return False

    async def set_charger_state(
        self, cp_id: str, service_name: str, state: bool = True
    ):
        """Carry out requested service/state change on connected charger."""
        resp = False
        if cp_id in self.charge_points:
            if service_name == csvcs.service_availability.name:
                resp = await self.charge_points[cp_id].set_availability(state)
            if service_name == csvcs.service_charge_start.name:
                resp = await self.charge_points[cp_id].start_transaction()
            if service_name == csvcs.service_charge_stop.name:
                resp = await self.charge_points[cp_id].stop_transaction()
            if service_name == csvcs.service_reset.name:
                resp = await self.charge_points[cp_id].reset()
            if service_name == csvcs.service_unlock.name:
                resp = await self.charge_points[cp_id].unlock()
        return resp

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

    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.id)},
        }
