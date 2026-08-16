"""Adds config flow for ocpp."""

from typing import Any
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    CONN_CLASS_LOCAL_PUSH,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_CSID,
    CONF_ENABLE_HA_NOTIFICATIONS,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_NUM_CONNECTORS,
    CONF_OCPP_VERSION,
    CONF_PORT,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    DEFAULT_CPID,
    DEFAULT_CSID,
    DEFAULT_ENABLE_HA_NOTIFICATIONS,
    DEFAULT_FORCE_SMART_CHARGING,
    DEFAULT_HOST,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MEASURAND,
    DEFAULT_METER_INTERVAL,
    DEFAULT_MONITORED_VARIABLES,
    DEFAULT_MONITORED_VARIABLES_AUTOCONFIG,
    DEFAULT_NUM_CONNECTORS,
    DEFAULT_OCPP_VERSION,
    DEFAULT_PORT,
    DEFAULT_SKIP_SCHEMA_VALIDATION,
    DEFAULT_SSL,
    DEFAULT_SSL_CERTFILE_PATH,
    DEFAULT_SSL_KEYFILE_PATH,
    DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_INTERVAL,
    DEFAULT_WEBSOCKET_PING_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_TRIES,
    DOMAIN,
    MEASURANDS,
    OCPP_VERSIONS,
)

STEP_USER_CS_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SSL, default=DEFAULT_SSL): bool,
        vol.Required(CONF_SSL_CERTFILE_PATH, default=DEFAULT_SSL_CERTFILE_PATH): str,
        vol.Required(CONF_SSL_KEYFILE_PATH, default=DEFAULT_SSL_KEYFILE_PATH): str,
        vol.Required(CONF_CSID, default=DEFAULT_CSID): vol.All(str, vol.Length(max=20)),
        vol.Required(CONF_OCPP_VERSION, default=DEFAULT_OCPP_VERSION): vol.In(
            OCPP_VERSIONS
        ),
        vol.Required(
            CONF_WEBSOCKET_CLOSE_TIMEOUT, default=DEFAULT_WEBSOCKET_CLOSE_TIMEOUT
        ): int,
        vol.Required(
            CONF_WEBSOCKET_PING_TRIES, default=DEFAULT_WEBSOCKET_PING_TRIES
        ): int,
        vol.Required(
            CONF_WEBSOCKET_PING_INTERVAL, default=DEFAULT_WEBSOCKET_PING_INTERVAL
        ): int,
        vol.Required(
            CONF_WEBSOCKET_PING_TIMEOUT, default=DEFAULT_WEBSOCKET_PING_TIMEOUT
        ): int,
    }
)

STEP_USER_CP_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CPID, default=DEFAULT_CPID): str,
        vol.Required(CONF_MAX_CURRENT, default=DEFAULT_MAX_CURRENT): int,
        vol.Required(
            CONF_MONITORED_VARIABLES_AUTOCONFIG,
            default=DEFAULT_MONITORED_VARIABLES_AUTOCONFIG,
        ): bool,
        vol.Required(CONF_METER_INTERVAL, default=DEFAULT_METER_INTERVAL): int,
        vol.Required(CONF_IDLE_INTERVAL, default=DEFAULT_IDLE_INTERVAL): int,
        vol.Required(
            CONF_SKIP_SCHEMA_VALIDATION, default=DEFAULT_SKIP_SCHEMA_VALIDATION
        ): bool,
        vol.Required(
            CONF_FORCE_SMART_CHARGING, default=DEFAULT_FORCE_SMART_CHARGING
        ): bool,
        vol.Required(
            CONF_ENABLE_HA_NOTIFICATIONS, default=DEFAULT_ENABLE_HA_NOTIFICATIONS
        ): bool,
    }
)

STEP_USER_MEASURANDS_SCHEMA = vol.Schema(
    {
        vol.Required(m, default=(True if m == DEFAULT_MEASURAND else False)): bool
        for m in MEASURANDS
    }
)


class ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OCPP."""

    VERSION = 2
    MINOR_VERSION = 2
    CONNECTION_CLASS = CONN_CLASS_LOCAL_PUSH

    def __init__(self):
        """Initialize."""
        self._data: dict[str, Any] = {}
        self._cp_id: str
        self._entry: ConfigEntry
        self._measurands: str = ""
        self._detected_num_connectors: int = DEFAULT_NUM_CONNECTORS

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "OCPPOptionsFlow":
        """Let the settings of an already-configured charge point be edited."""
        return OCPPOptionsFlow()

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Handle user central system initiated configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Don't allow servers to use same websocket port
            self._async_abort_entries_match({CONF_PORT: user_input[CONF_PORT]})
            self._data = user_input
            # Add placeholder for cpid settings
            self._data[CONF_CPIDS] = []
            return self.async_create_entry(title=self._data[CONF_CSID], data=self._data)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_CS_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"docs_url": "https://github.com/lbbrhzn/ocpp"},
        )

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        """Allow reconfiguring the central system settings of an existing entry.

        Without this, settings added after an entry was created (such as the
        OCPP version pin) could only be changed by deleting and re-adding the
        integration.
        """
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            # Don't allow servers to use same websocket port (the entry being
            # reconfigured is excluded from the match).
            self._async_abort_entries_match({CONF_PORT: user_input[CONF_PORT]})
            # Updating the entry already triggers a reload, via the
            # add_update_listener(async_reload_entry) registered in
            # async_setup_entry. async_update_reload_and_abort() would schedule
            # a second one on top of it, and the two overlap: the websocket
            # server is rebound while the first setup is still in flight and
            # the platform forwards then fail with "config entry ... has
            # already been setup". Update only, and let the listener reload.
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **user_input}
            )
            return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_CS_DATA_SCHEMA, entry.data
            ),
            description_placeholders={"docs_url": "https://github.com/lbbrhzn/ocpp"},
        )

    async def async_step_integration_discovery(
        self, discovery_info=None
    ) -> ConfigFlowResult:
        """Handle charger discovery initiated configuration."""

        self._entry = discovery_info["entry"]
        self._cp_id = discovery_info["cp_id"]
        self._data = {**self._entry.data}

        self._detected_num_connectors = discovery_info.get(
            CONF_NUM_CONNECTORS, DEFAULT_NUM_CONNECTORS
        )

        await self.async_set_unique_id(self._cp_id)
        # Abort the flow if a config entry with the same unique ID exists
        self._abort_if_unique_id_configured()
        return await self.async_step_cp_user()

    def _other_cpids_in_use(self) -> set[str]:
        """Return every cpid already configured for a charge point other than the current one.

        cpid (as opposed to cp_id, the OCPP-level charge point identity) is
        user-chosen and is what entities' unique_id is built from
        (DOMAIN.cpid.key...), so it must stay unique across every charge
        point of every OCPP config entry, not just within the current
        central system. Only the charge point currently being (re)configured
        -- matched on both its entry and its cp_id -- is excluded, so
        re-submitting its own cpid is not flagged as a duplicate of itself.
        """
        # Match on the entry as well as the charge point: cp_id is the
        # OCPP-level identity, so two central systems can each have one with
        # the same name. Skipping on cp_id alone would also skip the *other*
        # system's record and let a genuine duplicate through.
        own_entry_id = getattr(getattr(self, "_entry", None), "entry_id", None)
        cpids: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            for cp_data in entry.data.get(CONF_CPIDS, []):
                for cp_id, cp_settings in cp_data.items():
                    if entry.entry_id == own_entry_id and cp_id == self._cp_id:
                        continue
                    cpids.add(cp_settings[CONF_CPID])
        return cpids

    async def async_step_cp_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure charger by user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate cpid format against entity id requirements (lowercase letters, digits and _)
            schema = vol.Schema(
                {vol.Required(CONF_CPID): cv.matches_regex(r"^[\da-z_]+$")}
            )
            try:
                schema({CONF_CPID: user_input[CONF_CPID]})
            except vol.Invalid:
                errors["base"] = "invalid_cpid"
            else:
                # cpid is used to build entity unique_ids (DOMAIN.cpid.key...)
                # across every OCPP config entry, so it must be unique
                # integration-wide, not just within this central system.
                if user_input[CONF_CPID] in self._other_cpids_in_use():
                    errors["base"] = "duplicate_cpid"

            if not errors:
                cp_data = {
                    **user_input,
                    CONF_NUM_CONNECTORS: self._detected_num_connectors,
                }
                cpids_list = self._data.get(CONF_CPIDS, []).copy()
                cpids_list.append({self._cp_id: cp_data})
                self._data = {**self._data, CONF_CPIDS: cpids_list}

                if user_input[CONF_MONITORED_VARIABLES_AUTOCONFIG]:
                    self._data[CONF_CPIDS][-1][self._cp_id][
                        CONF_MONITORED_VARIABLES
                    ] = DEFAULT_MONITORED_VARIABLES
                    self.hass.config_entries.async_update_entry(
                        self._entry, data=self._data
                    )
                    return self.async_abort(reason="Added/Updated charge point")

                else:
                    return await self.async_step_measurands()

        return self.async_show_form(
            step_id="cp_user",
            data_schema=STEP_USER_CP_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"docs_url": "https://github.com/lbbrhzn/ocpp"},
        )

    async def async_step_measurands(self, user_input=None):
        """Select the measurands to be shown."""

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_measurands = [m for m, value in user_input.items() if value]
            if not set(selected_measurands).issubset(set(MEASURANDS)):
                errors["base"] = "no_measurands_selected"
                return self.async_show_form(
                    step_id="measurands",
                    data_schema=STEP_USER_MEASURANDS_SCHEMA,
                    errors=errors,
                )
            else:
                self._measurands = ",".join(selected_measurands)
                self._data[CONF_CPIDS][-1][self._cp_id][CONF_MONITORED_VARIABLES] = (
                    self._measurands
                )

                # With autoconfig off, the cpid was validated a step earlier and
                # nothing has been written yet, so another flow could have taken
                # it in the meantime. Re-check immediately before persisting:
                # there is no await between this and async_update_entry, so on
                # the single-threaded event loop nothing can slip in between.
                pending_cpid = self._data[CONF_CPIDS][-1][self._cp_id][CONF_CPID]
                if pending_cpid in self._other_cpids_in_use():
                    errors["base"] = "duplicate_cpid"
                    return self.async_show_form(
                        step_id="measurands",
                        data_schema=STEP_USER_MEASURANDS_SCHEMA,
                        errors=errors,
                    )

                self.hass.config_entries.async_update_entry(
                    self._entry, data=self._data
                )
                return self.async_abort(reason="Added/Updated charge point")

        return self.async_show_form(
            step_id="measurands",
            data_schema=STEP_USER_MEASURANDS_SCHEMA,
            errors=errors,
        )


class OCPPOptionsFlow(OptionsFlow):
    """Edit the settings of an already-configured charge point.

    The initial charger form (async_step_cp_user) is reachable only from
    integration discovery, which aborts for a charger that is already
    configured - so without this flow every per-charger setting was
    write-once (#2047). cpid stays read-only here: entity unique_ids
    derive from it, so changing it would orphan every existing entity.
    """

    def __init__(self) -> None:
        """Initialize."""
        self._cp_id: str = ""
        self._settings: dict[str, Any] = {}

    def _charge_points(self) -> dict[str, dict[str, Any]]:
        """Map cp_id to its stored settings for this entry."""
        points: dict[str, dict[str, Any]] = {}
        for item in self.config_entry.data.get(CONF_CPIDS, []):
            for cp_id, settings in item.items():
                points[cp_id] = settings
        return points

    def _finalize(self) -> ConfigFlowResult:
        """Overlay the edited fields onto the charge point and write it back.

        The overlay reads the entry as it is now, not as it was when the
        first form was submitted: while the user sits on the measurands
        form, a reconnecting charger's post_connect can update the entry
        (connector count, detected measurands), and writing back a snapshot
        taken earlier would erase that. Only the fields the user actually
        edited are replaced.
        """
        cpids = [
            {
                cp_id: (
                    {**stored, **self._settings} if cp_id == self._cp_id else stored
                )
                for cp_id, stored in item.items()
            }
            for item in self.config_entry.data.get(CONF_CPIDS, [])
        ]
        # Updating the entry already triggers a reload via the
        # add_update_listener(async_reload_entry) registered in
        # async_setup_entry - the same single-reload rule the reconfigure
        # step follows. The create_entry below writes entry.options, which
        # stays {} and therefore fires nothing on top of it.
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_CPIDS: cpids},
        )
        return self.async_create_entry(data={})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which charge point to edit, when there is a choice."""
        points = self._charge_points()
        if not points:
            return self.async_abort(reason="no_charge_points")

        if user_input is not None:
            self._cp_id = user_input["cp_id"]
            return await self.async_step_cp_settings()

        if len(points) == 1:
            self._cp_id = next(iter(points))
            return await self.async_step_cp_settings()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("cp_id"): vol.In(sorted(points))}),
        )

    async def async_step_cp_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the behavioural settings of the chosen charge point."""
        current = self._charge_points()[self._cp_id]

        if user_input is not None:
            # Hold only what the form edited; _finalize overlays it onto the
            # stored record. cpid, the connector count and the measurand
            # list are untouched by construction - in particular the
            # measurand list is NOT reseeded when autoconfig is left on: it
            # holds what detection accepted, and rewriting it here would
            # create sensors for every measurand as a side effect of
            # editing an unrelated setting.
            self._settings = dict(user_input)
            if user_input[CONF_MONITORED_VARIABLES_AUTOCONFIG]:
                return self._finalize()
            return await self.async_step_measurands()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAX_CURRENT,
                    default=current.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT),
                ): int,
                vol.Required(
                    CONF_MONITORED_VARIABLES_AUTOCONFIG,
                    default=current.get(
                        CONF_MONITORED_VARIABLES_AUTOCONFIG,
                        DEFAULT_MONITORED_VARIABLES_AUTOCONFIG,
                    ),
                ): bool,
                vol.Required(
                    CONF_METER_INTERVAL,
                    default=current.get(CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL),
                ): int,
                vol.Required(
                    CONF_IDLE_INTERVAL,
                    default=current.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL),
                ): int,
                vol.Required(
                    CONF_SKIP_SCHEMA_VALIDATION,
                    default=current.get(
                        CONF_SKIP_SCHEMA_VALIDATION, DEFAULT_SKIP_SCHEMA_VALIDATION
                    ),
                ): bool,
                vol.Required(
                    CONF_FORCE_SMART_CHARGING,
                    default=current.get(
                        CONF_FORCE_SMART_CHARGING, DEFAULT_FORCE_SMART_CHARGING
                    ),
                ): bool,
                vol.Required(
                    CONF_ENABLE_HA_NOTIFICATIONS,
                    default=current.get(
                        CONF_ENABLE_HA_NOTIFICATIONS,
                        DEFAULT_ENABLE_HA_NOTIFICATIONS,
                    ),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="cp_settings",
            data_schema=schema,
            description_placeholders={
                "cp_id": self._cp_id,
                "cpid": current.get(CONF_CPID, ""),
            },
        )

    async def async_step_measurands(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select measurands manually, pre-filled with the stored set."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = [m for m, value in user_input.items() if value]
            if selected:
                self._settings[CONF_MONITORED_VARIABLES] = ",".join(selected)
                return self._finalize()
            errors["base"] = "no_measurands_selected"

        current = self._charge_points()[self._cp_id]
        stored = {
            m for m in current.get(CONF_MONITORED_VARIABLES, "").split(",") if m
        } or {DEFAULT_MEASURAND}
        schema = vol.Schema(
            {vol.Required(m, default=(m in stored)): bool for m in MEASURANDS}
        )
        return self.async_show_form(
            step_id="measurands", data_schema=schema, errors=errors
        )
