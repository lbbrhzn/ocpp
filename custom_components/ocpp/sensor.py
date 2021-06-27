"""Sensor platform for ocpp."""

from homeassistant.const import CONF_MONITORED_VARIABLES, CONF_NAME
from homeassistant.helpers.entity import Entity

from .const import CONDITIONS, DOMAIN, GENERAL, ICON


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_sys = hass.data[DOMAIN][entry.entry_id]

    entities = []

    for measurand in entry.data[CONF_MONITORED_VARIABLES].split(","):
        entities.append(
            ChargePointMetric(measurand, central_sys, "M", entry.data[CONF_NAME])
        )
    for condition in CONDITIONS:
        entities.append(
            ChargePointMetric(condition, central_sys, "S", entry.data[CONF_NAME])
        )
    for gen in GENERAL:
        entities.append(ChargePointMetric(gen, central_sys, "G", entry.data[CONF_NAME]))

    async_add_devices(entities)


class ChargePointMetric(Entity):
    """Individual sensor for charge point metrics."""

    def __init__(self, metric, central_sys, genre, prefix):
        """Instantiate instance of a ChargePointMetrics."""
        self.metric = metric
        self.central_sys = central_sys
        self._id = ".".join([DOMAIN, "sensor", self.central_sys.id, self.metric])
        self._genre = genre
        self.prefix = prefix
        self._state = None
        self.type = "connected_chargers"

    @property
    def name(self):
        """Return the name of the sensor."""
        return DOMAIN + "." + self.prefix + "." + self.metric

    @property
    def unique_id(self):
        """Return the unique id of this sensor."""
        # This may need to be improved, perhaps use the vendor, model and serial number?
        return self._id

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

    @property
    def device_info(self):
        """Return device information."""
        return self.central_sys.device_info()

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "unique_id": self.unique_id,
            "integration": DOMAIN,
        }

    def update(self):
        """Get the latest data and update the states."""
        pass
