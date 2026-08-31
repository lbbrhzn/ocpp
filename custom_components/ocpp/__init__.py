"""Custom integration for Chargers that support the Open Charge Point Protocol."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import slugify
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import device_registry
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from ocpp.v16.enums import AuthorizationStatus

from .api import (
    CentralSystem,
    CHRGR_SERVICE_DATA_SCHEMA,
    CLEAR_PROFILE_SERVICE_DATA_SCHEMA,
    CONF_SERVICE_DATA_SCHEMA,
    GCONF_SERVICE_DATA_SCHEMA,
    GDIAG_SERVICE_DATA_SCHEMA,
    TRANS_SERVICE_DATA_SCHEMA,
    UFW_SERVICE_DATA_SCHEMA,
    CUSTMSG_SERVICE_DATA_SCHEMA,
)
from .enums import HAChargerServices as csvcs
from .const import (
    CONF_AUTH_LIST,
    CONF_AUTH_STATUS,
    CONF_CPIDS,
    CONF_DEFAULT_AUTH_STATUS,
    CONF_ENABLE_HA_NOTIFICATIONS,
    CONF_ID_TAG,
    CONF_NAME,
    CONF_CPID,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_NUM_CONNECTORS,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_PORT,
    CONF_CSID,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONFIG,
    DEFAULT_CPID,
    DEFAULT_ENABLE_HA_NOTIFICATIONS,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MAX_CURRENT,
    DEFAULT_METER_INTERVAL,
    DEFAULT_MONITORED_VARIABLES,
    DEFAULT_MONITORED_VARIABLES_AUTOCONFIG,
    DEFAULT_NUM_CONNECTORS,
    DEFAULT_SKIP_SCHEMA_VALIDATION,
    DEFAULT_FORCE_SMART_CHARGING,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_CSID,
    DEFAULT_SSL,
    DEFAULT_SSL_CERTFILE_PATH,
    DEFAULT_SSL_KEYFILE_PATH,
    DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_TRIES,
    DEFAULT_WEBSOCKET_PING_INTERVAL,
    DEFAULT_WEBSOCKET_PING_TIMEOUT,
    DOMAIN,
    PLATFORMS,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Key used to track domain-level services registered by this integration.
# Storing the names allows targeted removal on unload without relying on
# async_services_for_domain, which is unavailable before Home Assistant 2024.2.
_DOMAIN_SERVICE_NAMES = "_domain_service_names"


AUTH_LIST_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID_TAG): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_AUTH_STATUS): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONF_DEFAULT_AUTH_STATUS, default=AuthorizationStatus.accepted.value
        ): cv.string,
        vol.Optional(CONF_AUTH_LIST, default={}): vol.Schema(
            {cv.string: AUTH_LIST_SCHEMA}
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType):
    """Read configuration from yaml."""

    ocpp_config = config.get(DOMAIN, {})
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN][CONFIG] = ocpp_config
    _LOGGER.info(f"config = {ocpp_config}")
    return True


def _iter_central_systems(hass: HomeAssistant):
    """Yield every active CentralSystem instance registered in hass.data."""
    domain_data = hass.data.get(DOMAIN, {})
    for _key, value in domain_data.items():
        if isinstance(value, CentralSystem):
            yield value


def _resolve_central_system(hass: HomeAssistant, devid: str):
    """Return the CentralSystem that owns *devid*.

    *devid* is matched against the HA charger id (``cpid``) first and the raw
    OCPP charger id (``cp_id``) only afterwards.  The order matters: the config
    flow keeps ``cpid`` unique across every charge point of every entry, while
    ``cp_id`` is reported by the charger itself and two central systems can
    each own one of the same name.  Matching the unique identifier first stops
    a coincidental ``cp_id`` in one system from shadowing the real ``cpid`` of
    another.

    An omitted *devid* falls back to the only active CentralSystem if exactly
    one is loaded, which keeps working the legacy service calls that never
    supplied a target.  A *devid* that is supplied but matches nothing is an
    error: the caller named a charger, and quietly running the action against
    a different one is worse than failing.
    """
    central_systems = list(_iter_central_systems(hass))

    if devid:
        # Pass 1: cpid, the identifier the config flow keeps globally unique.
        owners = [
            cs for cs in central_systems if cs.cpids.get(devid) in cs.charge_points
        ]
        # Pass 2: cp_id as reported over OCPP, which carries no such guarantee.
        if not owners:
            owners = [cs for cs in central_systems if devid in cs.charge_points]

        if len(owners) == 1:
            return owners[0]
        if len(owners) > 1:
            # Several systems answer to this identifier, and picking one would
            # be a coin flip on a mutating service call.  Fail with something
            # the user can act on: their cpid is unique, so it always resolves.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ambiguous_devid",
                translation_placeholders={"message": devid},
            )

    # Backwards compatibility, scoped to an omitted target: legacy service
    # calls did not always supply a devid, and with exactly one CentralSystem
    # loaded the intended system is unambiguous.  A devid that was supplied
    # and did not resolve above never reaches this point.
    elif len(central_systems) == 1:
        return central_systems[0]

    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="not_found",
        translation_placeholders={"message": devid},
    )


def _register_domain_services(hass: HomeAssistant) -> list[str]:
    """Register global domain services that route to the correct CentralSystem.

    Services are registered exactly once for the whole domain.  When multiple
    central systems are active each call is routed to the instance that owns
    the charger identified by *devid*, preventing handler collisions and
    cross-system misrouting.

    Returns the list of service names registered by this integration so they
    can be removed precisely during unload without relying on newer HA APIs.
    """

    async def _route(method_name: str, call: ServiceCall) -> ServiceResponse:
        devid = call.data.get("devid") or ""
        cs = _resolve_central_system(hass, devid)
        return await getattr(cs, method_name)(call)

    async def _route_configure(call: ServiceCall) -> ServiceResponse:
        return await _route("handle_configure", call)

    async def _route_get_configuration(call: ServiceCall) -> ServiceResponse:
        return await _route("handle_get_configuration", call)

    async def _route_data_transfer(call: ServiceCall) -> None:
        await _route("handle_data_transfer", call)

    async def _route_trigger_custom_message(call: ServiceCall) -> None:
        await _route("handle_trigger_custom_message", call)

    async def _route_clear_profile(call: ServiceCall) -> None:
        await _route("handle_clear_profile", call)

    async def _route_set_charge_rate(call: ServiceCall) -> None:
        await _route("handle_set_charge_rate", call)

    async def _route_update_firmware(call: ServiceCall) -> None:
        await _route("handle_update_firmware", call)

    async def _route_get_diagnostics(call: ServiceCall) -> None:
        await _route("handle_get_diagnostics", call)

    services = [
        csvcs.service_configure,
        csvcs.service_get_configuration,
        csvcs.service_data_transfer,
        csvcs.service_trigger_custom_message,
        csvcs.service_clear_profile,
        csvcs.service_set_charge_rate,
        csvcs.service_update_firmware,
        csvcs.service_get_diagnostics,
    ]

    hass.services.async_register(
        DOMAIN,
        csvcs.service_configure,
        _route_configure,
        CONF_SERVICE_DATA_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        csvcs.service_get_configuration,
        _route_get_configuration,
        GCONF_SERVICE_DATA_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        csvcs.service_data_transfer,
        _route_data_transfer,
        TRANS_SERVICE_DATA_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        csvcs.service_trigger_custom_message,
        _route_trigger_custom_message,
        CUSTMSG_SERVICE_DATA_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        csvcs.service_clear_profile,
        _route_clear_profile,
        CLEAR_PROFILE_SERVICE_DATA_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        csvcs.service_set_charge_rate,
        _route_set_charge_rate,
        CHRGR_SERVICE_DATA_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        csvcs.service_update_firmware,
        _route_update_firmware,
        UFW_SERVICE_DATA_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        csvcs.service_get_diagnostics,
        _route_get_diagnostics,
        GDIAG_SERVICE_DATA_SCHEMA,
    )

    return services


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration from config entry."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(entry.data)

    central_sys = await CentralSystem.create(hass, entry)

    dr = device_registry.async_get(hass)

    # Create Central System device
    dr.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, central_sys.id)},
        name=central_sys.id,
        model="OCPP Central System",
    )

    # Create charger devices
    for cp_data in entry.data[CONF_CPIDS]:
        for cp_id, cp_settings in cp_data.items():
            cpid = cp_settings[CONF_CPID]
            dr.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, cp_id), (DOMAIN, cpid)},
                name=cpid,
                suggested_area="Garage",
                via_device=(DOMAIN, central_sys.id),
            )

    hass.data[DOMAIN][entry.entry_id] = central_sys

    # Register domain-wide services exactly once across all config entries.
    # The global handlers route each call to the correct CentralSystem by
    # resolving *devid* against every active instance's charger registry.
    if not hass.data[DOMAIN].get(_DOMAIN_SERVICE_NAMES):
        hass.data[DOMAIN][_DOMAIN_SERVICE_NAMES] = _register_domain_services(hass)

    if entry.data[CONF_CPIDS]:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        central_sys.platforms_forwarded = True

    # Registered after the forward deliberately: a discovery-driven
    # entry update must not be able to trigger a reload in the window
    # between the platforms being forwarded and the flag being set.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.info(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    if config_entry.version > 2:
        # This means the user has downgraded from a future version
        return False

    if config_entry.version == 1:
        old_data = {**config_entry.data}
        csid_data = {}
        cpid_data = {}
        cpid_keys = {
            CONF_CPID: DEFAULT_CPID,
            CONF_IDLE_INTERVAL: DEFAULT_IDLE_INTERVAL,
            CONF_MAX_CURRENT: DEFAULT_MAX_CURRENT,
            CONF_METER_INTERVAL: DEFAULT_METER_INTERVAL,
            CONF_MONITORED_VARIABLES: DEFAULT_MONITORED_VARIABLES,
            CONF_MONITORED_VARIABLES_AUTOCONFIG: DEFAULT_MONITORED_VARIABLES_AUTOCONFIG,
            CONF_SKIP_SCHEMA_VALIDATION: DEFAULT_SKIP_SCHEMA_VALIDATION,
            CONF_FORCE_SMART_CHARGING: DEFAULT_FORCE_SMART_CHARGING,
            CONF_ENABLE_HA_NOTIFICATIONS: DEFAULT_ENABLE_HA_NOTIFICATIONS,
        }
        csid_keys = {
            CONF_HOST: DEFAULT_HOST,
            CONF_PORT: DEFAULT_PORT,
            CONF_CSID: DEFAULT_CSID,
            CONF_SSL: DEFAULT_SSL,
            CONF_SSL_CERTFILE_PATH: DEFAULT_SSL_CERTFILE_PATH,
            CONF_SSL_KEYFILE_PATH: DEFAULT_SSL_KEYFILE_PATH,
            CONF_WEBSOCKET_CLOSE_TIMEOUT: DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
            CONF_WEBSOCKET_PING_TRIES: DEFAULT_WEBSOCKET_PING_TRIES,
            CONF_WEBSOCKET_PING_INTERVAL: DEFAULT_WEBSOCKET_PING_INTERVAL,
            CONF_WEBSOCKET_PING_TIMEOUT: DEFAULT_WEBSOCKET_PING_TIMEOUT,
        }
        for key, value in cpid_keys.items():
            cpid_data.update({key: old_data.get(key, value)})

        for key, value in csid_keys.items():
            csid_data.update({key: old_data.get(key, value)})

        new_data = csid_data
        # slugify, not lower(): the sensor platform slugifies its object
        # ids, so any cpid that is not already a slug ("Garage Charger",
        # "charger-1") would never resolve here.
        cp_id_state = hass.states.get(f"sensor.{slugify(cpid_data[CONF_CPID])}_id")
        if cp_id_state is None or cp_id_state.state in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            # A sentinel state is a plain string, so without this guard it
            # would be stored as the charge point key - serialisable, but
            # matching no charger, the same broken entry by another route.
            _LOGGER.warning(
                "Could not find charger id during migration, try a clean install"
            )
            return False
        # The charge point id is the sensor's VALUE. Storing the State
        # object itself - as this did until now - produces entry data that
        # cannot be JSON-serialised and a key no connecting charger can
        # ever match, so every v1 migration was broken. The test suite
        # could not see it: a global StateMachine.get patch fed it a
        # synthetic State, and the assertion checked top-level keys only.
        new_data.update({CONF_CPIDS: [{cp_id_state.state: cpid_data}]})

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, minor_version=0, version=2
        )

    if config_entry.version == 2 and config_entry.minor_version < 2:
        data = {**config_entry.data}
        cpids = list(data.get(CONF_CPIDS, []))
        for idx, cp_map in enumerate(cpids):
            if not isinstance(cp_map, dict) or not cp_map:
                continue

            migrated_cp_map = {}
            for cp_id, cp_data in cp_map.items():
                if not isinstance(cp_data, dict):
                    migrated_cp_map[cp_id] = cp_data
                    continue

                migrated_cp_data = {**cp_data}
                if config_entry.minor_version == 0:
                    migrated_cp_data.setdefault(
                        CONF_NUM_CONNECTORS, DEFAULT_NUM_CONNECTORS
                    )
                migrated_cp_data.setdefault(
                    CONF_ENABLE_HA_NOTIFICATIONS,
                    DEFAULT_ENABLE_HA_NOTIFICATIONS,
                )
                migrated_cp_map[cp_id] = migrated_cp_data
            cpids[idx] = migrated_cp_map

        data[CONF_CPIDS] = cpids
        hass.config_entries.async_update_entry(
            config_entry,
            data=data,
            version=2,
            minor_version=2,
        )

    _LOGGER.info(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unloaded = False
    if DOMAIN in hass.data:
        if entry.entry_id in hass.data[DOMAIN]:
            # Close server
            central_sys = hass.data[DOMAIN][entry.entry_id]
            central_sys._server.close()
            await central_sys._server.wait_closed()
            # Unload the platforms if - and only if - setup forwarded them.
            # Deciding from the live connection count skipped the unload
            # whenever every configured charger happened to be offline, and
            # the next setup's forward then collided ("has already been
            # setup!"), killing every entity platform until Core restarted.
            # A reload with the charger offline is exactly the reconfigure-
            # to-fix-the-connection case, so the two met constantly.
            if central_sys.platforms_forwarded:
                unloaded = await hass.config_entries.async_unload_platforms(
                    entry, PLATFORMS
                )
                _LOGGER.debug(
                    "Unloaded entity platforms for %s: %s", entry.title, unloaded
                )
            else:
                # Setup never forwarded them (no charger was configured),
                # so there is nothing to tear down - and asking Home
                # Assistant to unload never-forwarded platforms fails.
                _LOGGER.debug(
                    "No entity platforms were forwarded for %s; skipping unload",
                    entry.title,
                )
                unloaded = True
            # Remove entry
            if unloaded:
                hass.data[DOMAIN].pop(entry.entry_id)
                # Remove domain-wide services only when this is the last active
                # CentralSystem.  Removing them while another CS is still loaded
                # would break all service calls for that remaining instance.
                remaining_cs = list(_iter_central_systems(hass))
                if not remaining_cs:
                    for service in hass.data[DOMAIN].pop(_DOMAIN_SERVICE_NAMES, []):
                        hass.services.async_remove(DOMAIN, service)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
