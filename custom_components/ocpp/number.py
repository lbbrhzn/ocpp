"""Number platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberEntityDescription,
    RestoreNumber,
)
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .api import CentralSystem
from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_MAX_CURRENT,
    CONF_NUM_CONNECTORS,
    DATA_UPDATED,
    DEFAULT_MAX_CURRENT,
    DOMAIN,
    ICON,
)
from .enums import Profiles


@dataclass
class OcppNumberDescription(NumberEntityDescription):
    """Class to describe a Number entity."""

    initial_value: float | None = None


ELECTRIC_CURRENT_AMPERE = UnitOfElectricCurrent.AMPERE

NUMBERS: Final = [
    OcppNumberDescription(
        key="maximum_current",
        name="Maximum Current",
        icon=ICON,
        initial_value=DEFAULT_MAX_CURRENT,
        native_min_value=0,
        native_max_value=DEFAULT_MAX_CURRENT,
        native_step=1,
        native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the number platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    entities: list[ChargePointNumber] = []
    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]
        num_connectors = int(cp_id_settings.get(CONF_NUM_CONNECTORS, 1) or 1)
        for desc in NUMBERS:
            if desc.key == "maximum_current":
                ent_initial = cp_id_settings[CONF_MAX_CURRENT]
                ent_max = cp_id_settings[CONF_MAX_CURRENT]
            else:
                ent_initial = desc.initial_value
                ent_max = desc.native_max_value

            if num_connectors > 1:
                for conn_id in range(1, num_connectors + 1):
                    entities.append(
                        ChargePointNumber(
                            hass=hass,
                            central_system=central_system,
                            cpid=cpid,
                            description=OcppNumberDescription(
                                key=desc.key,
                                name=desc.name,
                                icon=desc.icon,
                                initial_value=ent_initial,
                                native_min_value=desc.native_min_value,
                                native_max_value=ent_max,
                                native_step=desc.native_step,
                                native_unit_of_measurement=desc.native_unit_of_measurement,
                            ),
                            connector_id=conn_id,
                            op_connector_id=conn_id,
                        )
                    )
            else:
                entities.append(
                    ChargePointNumber(
                        hass=hass,
                        central_system=central_system,
                        cpid=cpid,
                        description=OcppNumberDescription(
                            key=desc.key,
                            name=desc.name,
                            icon=desc.icon,
                            initial_value=ent_initial,
                            native_min_value=desc.native_min_value,
                            native_max_value=ent_max,
                            native_step=desc.native_step,
                            native_unit_of_measurement=desc.native_unit_of_measurement,
                        ),
                        connector_id=None,
                        op_connector_id=0,
                    )
                )

    async_add_devices(entities, False)


class ChargePointNumber(RestoreNumber, NumberEntity):
    """Individual slider for setting charge rate."""

    _attr_has_entity_name = True
    entity_description: OcppNumberDescription

    def __init__(
        self,
        hass: HomeAssistant,
        central_system: CentralSystem,
        cpid: str,
        description: OcppNumberDescription,
        connector_id: int | None = None,
        op_connector_id: int | None = None,
    ):
        """Initialize a Number instance."""
        self.cpid = cpid
        self._hass = hass
        self.central_system = central_system
        self.entity_description = description
        self.connector_id = connector_id
        self._op_connector_id = (
            op_connector_id if op_connector_id is not None else (connector_id or 1)
        )

        parts = [NUMBER_DOMAIN, DOMAIN, cpid, description.key]
        if self.connector_id:
            parts.insert(3, f"conn{self.connector_id}")
        self._attr_unique_id = ".".join(parts)
        self._attr_name = self.entity_description.name
        if self.connector_id:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{cpid}-conn{self.connector_id}")},
                name=f"{cpid} Connector {self.connector_id}",
                via_device=(DOMAIN, cpid),
            )
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, cpid)},
                name=cpid,
            )
        self._attr_native_value = self.entity_description.initial_value
        self._attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if restored := await self.async_get_last_number_data():
            self._attr_native_value = restored.native_value
        async_dispatcher_connect(
            self._hass, DATA_UPDATED, self._schedule_immediate_update
        )

    @callback
    def _schedule_immediate_update(self):
        self.async_schedule_update_ha_state(True)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        features = self.central_system.get_supported_features(self.cpid)
        has_smart = bool(features & Profiles.SMART)
        return bool(
            self.central_system.get_available(self.cpid, self._op_connector_id)
            and has_smart
        )

    async def async_set_native_value(self, value):
        """Set new value for station-wide max current (EVSE 0)."""
        num_value = float(value)
        resp = await self.central_system.set_max_charge_rate_amps(
            self.cpid,
            num_value,
            connector_id=self._op_connector_id,
        )
        if resp is True:
            self._attr_native_value = num_value
            self.async_write_ha_state()
