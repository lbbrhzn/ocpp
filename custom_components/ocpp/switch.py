"""Switch platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.helpers.entity import DeviceInfo
from ocpp.v16.enums import ChargePointStatus

from .api import CentralSystem
from .const import CONF_CPID, CONF_CPIDS, CONF_NUM_CONNECTORS, DOMAIN, ICON
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
    metric_condition: list[str] | None = None
    default_state: bool = False
    per_connector: bool = False


SWITCHES: Final[list[OcppSwitchDescription]] = [
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
        per_connector=True,
    ),
    OcppSwitchDescription(
        key="availability",
        name="Availability",
        icon=ICON,
        on_action=HAChargerServices.service_availability.name,
        off_action=HAChargerServices.service_availability.name,
        metric_state=HAChargerStatuses.status.value,  # charger-level status
        metric_condition=[ChargePointStatus.available.value],
        default_state=True,
        per_connector=False,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the switch platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    entities: list[ChargePointSwitch] = []

    for charger in entry.data[CONF_CPIDS]:
        cp_settings = list(charger.values())[0]
        cpid = cp_settings[CONF_CPID]
        num_connectors = int(cp_settings.get(CONF_NUM_CONNECTORS, 1) or 1)
        flatten_single = num_connectors == 1

        for desc in SWITCHES:
            if desc.per_connector:
                for conn_id in range(1, num_connectors + 1):
                    entities.append(
                        ChargePointSwitch(
                            central_system,
                            cpid,
                            desc,
                            connector_id=conn_id,
                            flatten_single=flatten_single,
                        )
                    )
            else:
                entities.append(
                    ChargePointSwitch(
                        central_system,
                        cpid,
                        desc,
                        connector_id=None,
                        flatten_single=False,
                    )
                )

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
        connector_id: int | None = None,
        flatten_single: bool = False,
    ):
        """Instantiate instance of a ChargePointSwitch."""
        self.cpid = cpid
        self.central_system = central_system
        self.entity_description = description
        self.connector_id = connector_id
        self._flatten_single = flatten_single
        self._state = self.entity_description.default_state
        parts = [SWITCH_DOMAIN, DOMAIN, cpid]
        if self.connector_id and not self._flatten_single:
            parts.append(f"conn{self.connector_id}")
        parts.append(description.key)
        self._attr_unique_id = ".".join(parts)
        self._attr_name = self.entity_description.name
        if self.connector_id and not self._flatten_single:
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

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        target_conn = (
            self.connector_id if self.entity_description.per_connector else None
        )
        return self.central_system.get_available(self.cpid, target_conn)

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        """Test metric state against condition if present"""
        if self.entity_description.metric_state is not None:
            metric_conn = (
                self.connector_id
                if (
                    self.entity_description.metric_state
                    == HAChargerStatuses.status_connector.value
                    or self.entity_description.per_connector
                )
                else None
            )
            resp = self.central_system.get_metric(
                self.cpid, self.entity_description.metric_state, metric_conn
            )
            if self.entity_description.metric_condition is not None:
                self._state = resp in self.entity_description.metric_condition
        return self._state

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        target_conn = self.connector_id if self.entity_description.per_connector else 0
        self._state = await self.central_system.set_charger_state(
            self.cpid, self.entity_description.on_action, True, connector_id=target_conn
        )

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        target_conn = self.connector_id if self.entity_description.per_connector else 0
        if self.entity_description.off_action is None:
            resp = True
        elif self.entity_description.off_action == self.entity_description.on_action:
            resp = await self.central_system.set_charger_state(
                self.cpid,
                self.entity_description.off_action,
                False,
                connector_id=target_conn,
            )
        else:
            resp = await self.central_system.set_charger_state(
                self.cpid, self.entity_description.off_action, connector_id=target_conn
            )
        self._state = not resp
