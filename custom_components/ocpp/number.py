"""Number platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
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
    DATA_UPDATED,
    DEFAULT_MAX_CURRENT,
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
    entities: list[ChargePointNumber | SessionCurrentLimitNumber] = []
    ent_reg = er.async_get(hass)

    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]
        try:
            connector_count = max(1, int(cp_id_settings.get("num_connectors", 1)))
        except (TypeError, ValueError):
            connector_count = 1

        legacy_uid = re.compile(
            rf"{NUMBER_DOMAIN}\.{DOMAIN}\.{re.escape(cpid)}\.conn\d+\.maximum_current"
        )
        session_connector_uid = re.compile(
            rf"{NUMBER_DOMAIN}\.{DOMAIN}\.{re.escape(cpid)}\.conn(\d+)\.session_current_limit"
        )
        session_flat_uid = f"{NUMBER_DOMAIN}.{DOMAIN}.{cpid}.session_current_limit"
        for registry_entry in er.async_entries_for_config_entry(
            ent_reg, entry.entry_id
        ):
            if (
                registry_entry.platform == DOMAIN
                and registry_entry.domain == NUMBER_DOMAIN
                and legacy_uid.fullmatch(registry_entry.unique_id)
            ):
                _LOGGER.info(
                    "Removing stale connector-level entity %s; "
                    "Maximum Current is station-wide on this charger",
                    registry_entry.entity_id,
                )
                ent_reg.async_remove(registry_entry.entity_id)
                continue
            if (
                registry_entry.platform != DOMAIN
                or registry_entry.domain != NUMBER_DOMAIN
            ):
                continue
            session_match = session_connector_uid.fullmatch(registry_entry.unique_id)
            stale_session = (connector_count == 1 and session_match is not None) or (
                connector_count > 1 and registry_entry.unique_id == session_flat_uid
            )
            if session_match is not None and int(session_match.group(1)) > min(
                connector_count, 99
            ):
                stale_session = True
            if stale_session:
                _LOGGER.info(
                    "Removing stale session-current entity %s after connector "
                    "topology changed",
                    registry_entry.entity_id,
                )
                ent_reg.async_remove(registry_entry.entity_id)

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

            uid_flat = ".".join([NUMBER_DOMAIN, DOMAIN, cpid, desc.key])
            fresh = ent_reg.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, uid_flat) is None
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
                    fresh=fresh,
                )
            )

        max_cur = float(cp_id_settings.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        if connector_count > 99:
            _LOGGER.warning(
                "%s: session current limit entities are limited to connectors 1..99",
                cpid,
            )
        for connector_id in range(1, min(connector_count, 99) + 1):
            entities.append(
                SessionCurrentLimitNumber(
                    hass,
                    central_system,
                    cpid,
                    connector_id,
                    connector_count,
                    max_cur,
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
        fresh: bool = False,
    ):
        """Initialize a Number instance."""
        self.cpid = cpid
        self._hass = hass
        self.central_system = central_system
        self.entity_description = description
        self._fresh = fresh
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
        # Restore data is keyed by entity id, not unique id. A new station
        # entity must not inherit an old connector slider's renamed state.
        if not self._fresh and (restored := await self.async_get_last_number_data()):
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
        """Set the station-wide maximum current.

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
            ok = await self.central_system.set_max_charge_rate_amps(self.cpid, target)
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


class SessionCurrentLimitNumber(NumberEntity):
    """Non-restoring, non-optimistic limit for one observed transaction."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = ICON
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = ELECTRIC_CURRENT_AMPERE

    def __init__(
        self,
        hass: HomeAssistant,
        central_system: CentralSystem,
        cpid: str,
        connector_id: int,
        connector_count: int,
        max_current: float,
    ) -> None:
        """Initialize a connector-scoped session number."""
        self.central_system = central_system
        self.cpid = cpid
        self.connector_id = connector_id
        self._attr_native_max_value = max_current
        self._attr_name = "Session Current Limit"
        if connector_count == 1:
            self._attr_unique_id = ".".join(
                [NUMBER_DOMAIN, DOMAIN, cpid, "session_current_limit"]
            )
            self.entity_id = (
                f"{NUMBER_DOMAIN}.{slugify(f'{cpid}_session_current_limit')}"
            )
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, cpid)},
                name=cpid,
            )
        else:
            self._attr_unique_id = ".".join(
                [
                    NUMBER_DOMAIN,
                    DOMAIN,
                    cpid,
                    f"conn{connector_id}",
                    "session_current_limit",
                ]
            )
            object_id = f"{cpid}_connector_{connector_id}_session_current_limit"
            self.entity_id = f"{NUMBER_DOMAIN}.{slugify(object_id)}"
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{cpid}-conn{connector_id}")},
                name=f"{cpid} Connector {connector_id}",
                via_device=(DOMAIN, cpid),
            )

    @property
    def _controller(self):
        return self.central_system.get_session_controller(self.cpid)

    @property
    def available(self) -> bool:
        """Return whether an exact online-observed session can be changed."""
        controller = self._controller
        return bool(controller and controller.is_available(self.connector_id))

    @property
    def native_value(self) -> float:
        """Return the confirmed session limit or neutral maximum."""
        controller = self._controller
        return (
            controller.value(self.connector_id)
            if controller is not None
            else float(self._attr_native_max_value)
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return transaction and transmitted-unit diagnostics."""
        controller = self._controller
        return controller.attributes(self.connector_id) if controller else {}

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller state changes."""
        await super().async_added_to_hass()

        @callback
        def _update(*args) -> None:
            active_lookup = None
            if args:
                try:
                    active_lookup = set(args[0])
                except Exception:
                    active_lookup = None
            if active_lookup is None or self.entity_id in active_lookup:
                self.async_write_ha_state()

        self.async_on_remove(async_dispatcher_connect(self.hass, DATA_UPDATED, _update))
        controller = self._controller
        if controller is not None:
            self.async_on_remove(
                controller.register_entity(self.connector_id, self.entity_id)
            )

    async def async_set_native_value(self, value: float) -> None:
        """Wait for charger confirmation before the exposed value can change."""
        controller = self._controller
        if controller is None:
            raise HomeAssistantError("session current limit controller not found")
        token = controller.current_token(self.connector_id)
        if token is None:
            raise HomeAssistantError("there is no qualifying charging session")
        await self.central_system.set_session_charge_rate_amps(
            self.cpid,
            self.connector_id,
            token,
            float(value),
        )
        self.async_write_ha_state()
