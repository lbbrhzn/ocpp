"""Number platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Final

from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberEntityDescription,
    RestoreNumber,
)
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from .api import CentralSystem
from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_MAX_CURRENT,
    CONF_NUM_CONNECTORS,
    DATA_UPDATED,
    DEFAULT_MAX_CURRENT,
    DEFAULT_NUM_CONNECTORS,
    DOMAIN,
    ICON,
)
from .enums import Profiles

_LOGGER: logging.Logger = logging.getLogger(__package__)


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
    ent_reg = er.async_get(hass)

    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]

        num_connectors = 1
        for item in entry.data.get(CONF_CPIDS, []):
            for _, cfg in item.items():
                if cfg.get(CONF_CPID) == cpid:
                    num_connectors = int(
                        cfg.get(CONF_NUM_CONNECTORS, DEFAULT_NUM_CONNECTORS)
                    )
                    break
            else:
                continue
            break

        if num_connectors > 1:
            for desc in NUMBERS:
                uid_flat = ".".join([NUMBER_DOMAIN, DOMAIN, cpid, desc.key])
                stale_eid = ent_reg.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, uid_flat)
                if stale_eid:
                    ent_reg.async_remove(stale_eid)

        for desc in NUMBERS:
            if desc.key == "maximum_current":
                max_cur = float(
                    cp_id_settings.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT)
                )
                ent_initial = max_cur
                ent_max = max_cur
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

    _attr_has_entity_name = False
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
        if self.connector_id is not None:
            object_id = f"{self.cpid}_connector_{self.connector_id}_{self.entity_description.key}"
        else:
            object_id = f"{self.cpid}_{self.entity_description.key}"
        self.entity_id = f"{NUMBER_DOMAIN}.{slugify(object_id)}"
        self._attr_native_value = self.entity_description.initial_value
        # The last limit this integration believes the charger is holding:
        # confirmed by an accepted request this session, or restored from
        # the previous one (the charger keeps its profile across our
        # restarts). None on a fresh install, so a rollback cannot invent
        # a limit. Requests the charger performs without this entity - the
        # ocpp.clear_profile / ocpp.set_charge_rate services - are not
        # reflected here, the same blind spot the pre-#2049 code had.
        self._confirmed_value: float | None = None
        # Monotonic ticket per request, and the ticket of the newest
        # accepted one. The transport serialises calls today (the ocpp
        # library holds its call lock across send and response), so
        # completions cannot cross - but that is a property of a library
        # two layers down, not of this entity. The guard keeps the
        # display-owns-latest-accepted invariant provable right here.
        self._request_seq: int = 0
        self._accepted_seq: int = 0
        self._attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if restored := await self.async_get_last_number_data():
            self._attr_native_value = restored.native_value
            # What the previous session last settled on. The charger keeps
            # its charging profile across our restarts, so this is the best
            # available proxy for what it is holding - stale only if the
            # charger was reset or cleared in between, and corrected by the
            # next accepted request either way.
            self._confirmed_value = restored.native_value

        @callback
        def _maybe_update(*args):
            active_lookup = None
            if args:
                try:
                    active_lookup = set(args[0])
                except Exception:
                    active_lookup = None

            if active_lookup is None or self.entity_id in active_lookup:
                self.async_schedule_update_ha_state(True)

        self.async_on_remove(
            async_dispatcher_connect(self.hass, DATA_UPDATED, _maybe_update)
        )

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
        """Set new value for max current (station-wide when _op_connector_id==0, otherwise per-connector).

        - Optimistic UI: move the slider immediately so it tracks the drag.
        - On refusal, put it back and raise: a current limit that reads as
          applied while the charger runs unrestricted is worse than an
          error, because the number is the only thing telling the user what
          the circuit is doing. Keeping the value only logged the problem.
        """
        target = float(value)
        self._request_seq += 1
        seq = self._request_seq
        self._attr_native_value = target
        self.async_write_ha_state()

        try:
            ok = await self.central_system.set_max_charge_rate_amps(
                self.cpid, target, connector_id=self._op_connector_id
            )
        except HomeAssistantError:
            # set_charge_rate raises this for a rejected profile, and its
            # message carries the charger's own status_info - the only
            # explanation of why. Surface it rather than restating it.
            self._revert_to_confirmed()
            raise
        except Exception as ex:
            self._revert_to_confirmed()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_charge_rate_error",
                translation_placeholders={"message": str(ex)},
            ) from ex

        if not ok:
            self._revert_to_confirmed()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_charge_rate_error",
                translation_placeholders={
                    "message": f"charger did not accept {target:.1f} A"
                },
            )

        if seq <= self._accepted_seq:
            # A newer request was already accepted while this one was in
            # flight; its limit superseded this one on the charger, so it
            # owns the display and the confirmed value.
            _LOGGER.debug(
                "Accepted limit %.1f A superseded in flight; display stays at %s",
                target,
                self._attr_native_value,
            )
            return
        self._accepted_seq = seq
        self._confirmed_value = target
        if self._attr_native_value != target:
            # A request that started later failed while this one was in
            # flight and rolled the slider back. This limit is the one the
            # charger is holding, so it owns what is displayed.
            self._attr_native_value = target
            self.async_write_ha_state()

    def _revert_to_confirmed(self) -> None:
        """Put the slider back to the last limit the charger accepted.

        Reverting to whatever was displayed when this request started would
        clobber a concurrent request that has since been accepted - two
        quick drags, or an automation racing the UI - and leave the slider
        disagreeing with the charger, which is the thing this is meant to
        prevent rather than cause.
        """
        # Whole-amp values, so equality is exact (native_step=1). A
        # fractional step would need a tolerance here and in the
        # superseded check above.
        if self._attr_native_value == self._confirmed_value:
            return
        _LOGGER.debug(
            "Reverting current limit display from %s to last accepted %s",
            self._attr_native_value,
            self._confirmed_value,
        )
        self._attr_native_value = self._confirmed_value
        self.async_write_ha_state()
