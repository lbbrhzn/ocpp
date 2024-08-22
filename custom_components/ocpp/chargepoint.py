"""Common classes for charge points of all OCPP versions."""

from types import MappingProxyType
from typing import Any

from .const import (
    UNITS_OCCP_TO_HA,
)


class CentralSystemSettings:
    """A subset of CentralSystem properties needed by a ChargePoint."""

    websocket_close_timeout: int
    websocket_ping_interval: int
    websocket_ping_timeout: int
    websocket_ping_tries: int
    csid: str
    cpid: str
    config: MappingProxyType[str, Any]


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
