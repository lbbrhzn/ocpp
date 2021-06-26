"""Implement a OCCP Central System."""
import logging

import websockets

from .charge_point import ChargePoint
from .const import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SUBPROTOCOL

_LOGGER = logging.getLogger(__name__)


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self, id, config):
        """Instantiate instance of a CentralSystem."""
        self._server = None
        self._connected_charger = None
        self._connected_id = ""
        self._cp_metrics = {}
        self.config = config
        self.id = id

    @staticmethod
    async def create(
        id,
        config,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        proto: str = DEFAULT_SUBPROTOCOL,
    ):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem(id, config)
        server = await websockets.serve(
            self.on_connect, host, port, subprotocols=[proto]
        )

        self._server = server
        return self

    async def on_connect(self, websocket, path: str):
        """Request handler executed for every new OCPP connection."""
        # For now only 1 charger can connect.
        cp_id = path.strip("/")
        if self._connected_charger is not None:
            return

        try:
            cp = ChargePoint(cp_id, websocket, self.config)
            self._connected_charger = cp
            if self._connected_id == cp.id:
                _LOGGER.debug(f"Charger {cp_id} reconnected.")
                self._cp_metrics = await cp.reconnect(self._cp_metrics)
            else:
                _LOGGER.info(f"Charger {cp_id} connected.")
                self._connected_id = cp.id
                self._cp_metrics = await cp.start()
        finally:
            self._connected_charger = None

    def get_metric(self, measurand: str):
        """Return last known value for given measurand."""
        if self._connected_charger is not None:
            return self._connected_charger.get_metric(measurand)
        return None

    def get_unit(self, measurand: str):
        """Return unit of given measurand."""
        if self._connected_charger is not None:
            return self._connected_charger.get_unit(measurand)
        return None
