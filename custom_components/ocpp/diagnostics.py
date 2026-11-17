"""Privacy-preserving diagnostics for the optional secondary reporter."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CPIDS, CONF_REPORTING_ENABLED, DOMAIN

_STATUSES = frozenset({
    "starting", "connected", "disconnected", "waiting_for_charger",
    "unsupported_protocol", "blocked",
})
_ACTIONS = frozenset({
    "Authorize", "StatusNotification", "StartTransaction", "MeterValues",
    "StopTransaction", "DiagnosticsStatusNotification", "FirmwareStatusNotification",
})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return only allowlisted health metadata, never config or OCPP payloads.

    Both charger identities and reporting URL (including paths and possible
    embedded tokens), usernames, passwords, meter values and session bodies
    are deliberately absent. Indexes are scoped to this config entry only.
    """
    central = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    reporters = getattr(central, "reporters", {})
    chargers = []
    for group in entry.data.get(CONF_CPIDS, []):
        for cp_id, settings in group.items():
            enabled = settings.get(CONF_REPORTING_ENABLED) is True
            record = {"reporting_enabled": enabled}
            reporter = reporters.get(cp_id) if enabled else None
            if reporter is not None:
                health = reporter.diagnostics()
                status = health.get("status")
                record["status"] = status if isinstance(status, str) and status in _STATUSES else "unknown"
                for key in ("pending", "buffered"):
                    value = health.get(key)
                    if type(value) is int and value >= 0:
                        record[key] = value
                record["source_connected"] = health.get("source_connected") is True
                timestamp = health.get("last_acknowledged")
                if type(timestamp) in (int, float) and timestamp >= 0:
                    record["last_acknowledged"] = timestamp
                blocked = health.get("blocked_event")
                if isinstance(blocked, dict):
                    event = {}
                    event_id = blocked.get("id")
                    if type(event_id) is int and event_id > 0:
                        event["id"] = event_id
                    action = blocked.get("action")
                    if isinstance(action, str) and action in _ACTIONS:
                        event["action"] = action
                    state = blocked.get("state")
                    if isinstance(state, str) and state in ("uncertain", "blocked"):
                        event["state"] = state
                    if event:
                        record["blocked_event"] = event
            chargers.append(record)
    return {"chargers": chargers}
