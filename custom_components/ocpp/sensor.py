"""Sensor platform for ocpp."""

from homeassistant.const import CONF_MONITORED_VARIABLES
from homeassistant.helpers.entity import Entity

from .api import CentralSystem
from .const import CONDITIONS, CONF_CPID, DOMAIN, GENERAL, ICON


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data[CONF_CPID]

    entities = []

    for measurand in entry.data[CONF_MONITORED_VARIABLES].split(","):
        entities.append(
            ChargePointMetric(
                central_system,
                cp_id,
                measurand,
                "M",
            )
        )
    for condition in CONDITIONS:
        entities.append(
            ChargePointMetric(
                central_system,
                cp_id,
                condition,
                "S",
            )
        )
    for general in GENERAL:
        entities.append(
            ChargePointMetric(
                central_system,
                cp_id,
                general,
                "G",
            )
        )

    async_add_devices(entities, False)


class ChargePointMetric(Entity):
    """Individual sensor for charge point metrics."""

    def __init__(
        self,
        central_system: CentralSystem,
        cp_id: str,
        metric: str,
        genre: str,
    ):
        """Instantiate instance of a ChargePointMetrics."""
        self.central_system = central_system
        self.cp_id = cp_id
        self.metric = metric
        self._genre = genre
        self._state = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return ".".join([self.cp_id, self.metric])

    @property
    def unique_id(self):
        """Return the unique id of this sensor."""
        return ".".join([DOMAIN, self.cp_id, self.metric, "sensor"])

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.central_system.get_metric(self.cp_id, self.metric)

    @property
    def genre(self):
        """Return the type of sensor "M"=measurand "S"=status "G"= general info."""
        return self._genre

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self.central_system.get_unit(self.cp_id, self.metric)

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state.

        False if entity pushes its state to HA.
        """
        return True

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return ICON

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.cp_id)},
            "via_device": (DOMAIN, self.central_system.id),
        }

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
