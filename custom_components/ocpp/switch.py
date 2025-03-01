"""Switch platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.const import UnitOfPower
from homeassistant.helpers.entity import DeviceInfo
from ocpp.v16.enums import ChargePointStatus

from .api import CentralSystem
from .const import CONF_CPID, CONF_CPIDS, DOMAIN, ICON
from .enums import HAChargerServices, HAChargerStatuses


# Switch configuration definitions
# At a minimum define switch name and on service call,
# metric and condition combination can be used to drive switch state, use default to set initial state to True
@dataclass
class OcppSwitchDescription(SwitchEntityDescription):
    """Class to describe a Switch entity."""

    on_action: str | None = None
    off_action: str | None = None
    metric_state: str | None = None
    metric_condition: str | None = None
    default_state: bool = False


POWER_KILO_WATT = UnitOfPower.KILO_WATT

SWITCHES: Final = [
    OcppSwitchDescription(
        key="charge_control",
        name="Charge Control",
        icon=ICON,
        on_action=HAChargerServices.service_charge_start.name,
        off_action=HAChargerServices.service_charge_stop.name,
        metric_state=HAChargerStatuses.status_connector.value,
        metric_condition=[
            ChargePointStatus.charging.value,
            ChargePointStatus.suspended_evse.value,
            ChargePointStatus.suspended_ev.value,
        ],
    ),
    OcppSwitchDescription(
        key="availability",
        name="Availability",
        icon=ICON,
        on_action=HAChargerServices.service_availability.name,
        off_action=HAChargerServices.service_availability.name,
        metric_state=HAChargerStatuses.status_connector.value,
        metric_condition=[ChargePointStatus.available.value],
        default_state=True,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the switch platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]

        for ent in SWITCHES:
            cpx = ChargePointSwitch(central_system, cpid, ent)
            entities.append(cpx)

    async_add_devices(entities, False)


class ChargePointSwitch(SwitchEntity):
    """Individual switch for charge point."""

    _attr_has_entity_name = True
    entity_description: OcppSwitchDescription

    def __init__(
        self,
        central_system: CentralSystem,
        cpid: str,
        description: OcppSwitchDescription,
    ):
        """Instantiate instance of a ChargePointSwitch."""
        self.cpid = cpid
        self.central_system = central_system
        self.entity_description = description
        self._state = self.entity_description.default_state
        self._attr_unique_id = ".".join(
            [SWITCH_DOMAIN, DOMAIN, self.cpid, self.entity_description.key]
        )
        self._attr_name = self.entity_description.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.cpid)},
        )

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        return self.central_system.get_available(self.cpid)  # type: ignore [no-any-return]

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        """Test metric state against condition if present"""
        if self.entity_description.metric_state is not None:
            resp = self.central_system.get_metric(
                self.cpid, self.entity_description.metric_state
            )
            if resp in self.entity_description.metric_condition:
                self._state = True
            else:
                self._state = False
        return self._state  # type: ignore [no-any-return]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._state = await self.central_system.set_charger_state(
            self.cpid, self.entity_description.on_action
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        """Response is True if successful but State is False"""
        if self.entity_description.off_action is None:
            resp = True
        elif self.entity_description.off_action == self.entity_description.on_action:
            resp = await self.central_system.set_charger_state(
                self.cpid, self.entity_description.off_action, False
            )
        else:
            resp = await self.central_system.set_charger_state(
                self.cpid, self.entity_description.off_action
            )
        self._state = not resp
