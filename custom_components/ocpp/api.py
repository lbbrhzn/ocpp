"""Representation of a OCPP Entities."""

from __future__ import annotations

import logging
import ssl

from functools import partial
from homeassistant.config_entries import ConfigEntry, SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import STATE_OK
from homeassistant.core import HomeAssistant
from websockets import Subprotocol, NegotiationError
import websockets.server
from websockets.asyncio.server import ServerConnection

from .ocppv16 import ChargePoint as ChargePointv16
from .ocppv201 import ChargePoint as ChargePointv201

from .const import (
    CentralSystemSettings,
    DOMAIN,
    OCPP_2_0,
    ChargerSystemSettings,
)
from .enums import (
    HAChargerServices as csvcs,
)
from .chargepoint import async_setup_charger

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)
# Uncomment these when Debugging
# logging.getLogger("asyncio").setLevel(logging.DEBUG)
# logging.getLogger("websockets").setLevel(logging.DEBUG)


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Instantiate instance of a CentralSystem."""
        self.hass = hass
        self.entry = entry
        self.settings = CentralSystemSettings(**entry.data)
        self.subprotocols = self.settings.subprotocols
        self._server = None
        self.id = self.settings.csid
        self.charge_points = {}  # uses cp_id as reference to charger instance
        self.cpids = {}  # dict of {cpid:cp_id}
        self.connections = 0

    @staticmethod
    async def create(hass: HomeAssistant, entry: ConfigEntry):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem(hass, entry)

        if self.settings.ssl:
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # see https://community.home-assistant.io/t/certificate-authority-and-self-signed-certificate-for-ssl-tls/196970
            localhost_certfile = self.settings.certfile
            localhost_keyfile = self.settings.keyfile
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

    def select_subprotocol(
        self, connection: ServerConnection, subprotocols
    ) -> Subprotocol | None:
        """Override default subprotocol selection."""

        # Server offers at least one subprotocol but client doesn't offer any.
        # Default to None
        if not subprotocols:
            return None

        # Server and client both offer subprotocols. Look for a shared one.
        proposed_subprotocols = set(subprotocols)
        for subprotocol in proposed_subprotocols:
            if subprotocol in self.subprotocols:
                return subprotocol

        # No common subprotocol was found.
        raise NegotiationError(
            "invalid subprotocol; expected one of " + ", ".join(self.subprotocols)
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
            # check if charger already has config entry
            config_flow = False
            for cfg in self.settings.cpids:
                if cfg.get(cp_id):
                    config_flow = True
                    cp_settings = ChargerSystemSettings(**list(cfg.values())[0])
                    _LOGGER.info(f"Charger match found for {cp_settings.cpid}:{cp_id}")
                    _LOGGER.debug(f"Central settings: {self.settings}")

            if not config_flow:
                # discovery_info for flow
                info = {"cp_id": cp_id, "entry": self.entry}
                await self.hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY}, data=info
                )

            self.cpids.update({cp_settings.cpid: cp_id})
            await async_setup_charger(
                self.hass, self.entry, cs_id=self.id, cpid=cp_settings.cpid, cp_id=cp_id
            )

            if websocket.subprotocol and websocket.subprotocol.startswith(OCPP_2_0):
                charge_point = ChargePointv201(
                    cp_id, websocket, self.hass, self.entry, self.settings, cp_settings
                )
            else:
                charge_point = ChargePointv16(
                    cp_id, websocket, self.hass, self.entry, self.settings, cp_settings
                )
            self.charge_points[cp_id] = charge_point

            await charge_point.start()
            self.connections = +1
            _LOGGER.info(
                f"Charger {cp_settings.cpid}:{cp_id} connected to {self.settings.host}:{self.settings.port}."
            )
            _LOGGER.info(
                f"{self.connections} charger(s): {self.cpids} now connected to central system:{self.settings.csid}."
            )
        else:
            _LOGGER.info(
                f"Charger {cp_id} reconnected to {self.settings.host}:{self.settings.port}."
            )
            charge_point = self.charge_points[cp_id]
            await charge_point.reconnect(websocket)

    def get_metric(self, id: str, measurand: str):
        """Return last known value for given measurand."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].value
        return None

    def del_metric(self, id: str, measurand: str):
        """Set given measurand to None."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if self.cpids.get(cp_id) in self.charge_points:
            self.charge_points[cp_id]._metrics[measurand].value = None
        return None

    def get_unit(self, id: str, measurand: str):
        """Return unit of given measurand."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].unit
        return None

    def get_ha_unit(self, id: str, measurand: str):
        """Return home assistant unit of given measurand."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].ha_unit
        return None

    def get_extra_attr(self, id: str, measurand: str):
        """Return last known extra attributes for given measurand."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if cp_id in self.charge_points:
            return self.charge_points[cp_id]._metrics[measurand].extra_attr
        return None

    def get_available(self, id: str):
        """Return whether the charger is available."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].status == STATE_OK
        return False

    def get_supported_features(self, id: str):
        """Return what profiles the charger supports."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if cp_id in self.charge_points:
            return self.charge_points[cp_id].supported_features
        return 0

    async def set_max_charge_rate_amps(self, id: str, value: float):
        """Set the maximum charge rate in amps."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
        if cp_id in self.charge_points:
            return await self.charge_points[cp_id].set_charge_rate(limit_amps=value)
        return False

    async def set_charger_state(self, id: str, service_name: str, state: bool = True):
        """Carry out requested service/state change on connected charger."""
        # allow id to be either cpid or cp_id
        cp_id = self.cpids.get(id)
        if cp_id is None:
            cp_id = id
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

    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.id)},
        }
