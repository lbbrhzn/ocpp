"""Sensor platform for ocpp."""

from homeassistant.const import CONF_NAME
from homeassistant.helpers.entity import Entity

from .const import CONDITIONS, DOMAIN, GENERAL, ICON, MEASURANDS


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_sys = hass.data[DOMAIN][entry.entry_id]

    metrics = []
    for measurand in MEASURANDS:
        metrics.append(
            ChargePointMetric(measurand, central_sys, "M", entry.data[CONF_NAME])
        )
    for condition in CONDITIONS:
        metrics.append(
            ChargePointMetric(condition, central_sys, "S", entry.data[CONF_NAME])
        )
    for gen in GENERAL:
        metrics.append(ChargePointMetric(gen, central_sys, "G", entry.data[CONF_NAME]))

    async_add_devices(metrics)


class ChargePointMetric(Entity):
    """Individual sensor for charge point metrics."""

    def __init__(self, metric, central_sys, genre, prefix):
        """Instantiate instance of a ChargePointMetrics."""
        self.metric = metric
        self.central_sys = central_sys
        self._genre = genre
        self.prefix = prefix
        self._state = None
        self.type = "connected_chargers"

    @property
    def name(self):
        """Return the name of the sensor."""
        return self.prefix + "." + self.metric

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.central_sys.get_metric(self.metric)

    @property
    def genre(self):
        """Return the type of sensor "M"=measurand "S"=status "G"= general info."""
        return self._genre

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self.central_sys.get_unit(self.metric)

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return ICON

    def update(self):
        """Get the latest data and update the states."""
        pass
