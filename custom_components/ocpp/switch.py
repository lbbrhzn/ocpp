"""Switch platform for ocpp."""
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.const import POWER_KILO_WATT
from homeassistant.helpers.entity import DeviceInfo

from ocpp.v16.enums import ChargePointStatus, Measurand

from .api import CentralSystem
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN, ICON
from .enums import HAChargerServices, HAChargerStatuses


# Switch configuration definitions
# At a minimum define switch name and on service call,
# metric and condition combination can be used to drive switch state, use default to set initial state to True
@dataclass
class OcppSwitchDescription(SwitchEntityDescription):
    """Class to describe a Switch entity."""

    on_action: str = ""
    off_action: str = ""
    metric_state: str = ""
    metric_condition: str = ""
    default_state: bool = False


SWITCHES: Final = [
    OcppSwitchDescription(
        key="charge_control",
        name="Charge_Control",
        icon=ICON,
        on_action=HAChargerServices.service_charge_start.name,
        off_action=HAChargerServices.service_charge_stop.name,
        metric_state=HAChargerStatuses.status.value,
        metric_condition=ChargePointStatus.charging.value,
    ),
    OcppSwitchDescription(
        key="availability",
        name="Availability",
        icon=ICON,
        on_action=HAChargerServices.service_availability.name,
        off_action=HAChargerServices.service_availability.name,
        metric_state=HAChargerStatuses.status.value,
        metric_condition=ChargePointStatus.available.value,
        default_state=True,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)

    entities = []

    for ent in SWITCHES:
        entities.append(ChargePointSwitch(central_system, cp_id, ent))

    async_add_devices(entities, False)


class ChargePointSwitch(SwitchEntity):
    """Individual switch for charge point."""

    entity_description: OcppSwitchDescription

    def __init__(
        self,
        central_system: CentralSystem,
        cp_id: str,
        description: OcppSwitchDescription,
    ):
        """Instantiate instance of a ChargePointSwitch."""
        self.cp_id = cp_id
        self.central_system = central_system
        self.entity_description = description
        self._state = self.entity_description.default_state
        self._attr_unique_id = ".".join(
            [SWITCH_DOMAIN, DOMAIN, self.cp_id, self.entity_description.key]
        )
        self._attr_name = ".".join([self.cp_id, self.entity_description.name])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.cp_id)},
            via_device=(DOMAIN, self.central_system.id),
        )
        self.entity_id = (
            SWITCH_DOMAIN + "." + "_".join([self.cp_id, self.entity_description.key])
        )

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        return self.central_system.get_available(self.cp_id)  # type: ignore [no-any-return]

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        """Test metric state against condition if present"""
        if self.entity_description.metric_state != "":
            resp = self.central_system.get_metric(
                self.cp_id, self.entity_description.metric_state
            )
            if resp == self.entity_description.metric_condition:
                self._state = True
            else:
                self._state = False
        return self._state  # type: ignore [no-any-return]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._state = await self.central_system.set_charger_state(
            self.cp_id, self.entity_description.on_action
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        """Response is True if successful but State is False"""
        if self.entity_description.off_action == "":
            resp = True
        elif self.entity_description.off_action == self.entity_description.on_action:
            resp = await self.central_system.set_charger_state(
                self.cp_id, self.entity_description.off_action, False
            )
        else:
            resp = await self.central_system.set_charger_state(
                self.cp_id, self.entity_description.off_action
            )
        self._state = not resp

    @property
    def current_power_w(self) -> Any:
        """Return the current power usage in W."""
        if self.entity_description.key == "charge_control":
            value = self.central_system.get_metric(
                self.cp_id, Measurand.power_active_import.value
            )
            if (
                self.central_system.get_ha_unit(
                    self.cp_id, Measurand.power_active_import.value
                )
                == POWER_KILO_WATT
            ):
                value = value * 1000
            return value
        return None

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "unique_id": self.unique_id,
            "integration": DOMAIN,
        }
