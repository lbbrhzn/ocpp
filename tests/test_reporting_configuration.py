"""Consumer checks for secondary reporting configuration and safe diagnostics."""

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_MONITORED_VARIABLES,
    CONF_NUM_CONNECTORS,
    CONF_OCPP_VERSION,
    CONF_REPORTING_ENABLED,
    CONF_REPORTING_PASSWORD,
    CONF_REPORTING_QUEUE_LIMIT,
    CONF_REPORTING_URL,
    CONF_REPORTING_USERNAME,
    DOMAIN,
)
from custom_components.ocpp.diagnostics import async_get_config_entry_diagnostics
from custom_components.ocpp.sensor import ReportingSensor

from .const import MOCK_CONFIG_CP, MOCK_CONFIG_CS


@pytest.fixture(autouse=True)
def bypass_setup():
    """Exercise real config flows without starting the OCPP listener."""
    with (
        patch("custom_components.ocpp.async_setup", return_value=True),
        patch("custom_components.ocpp.async_setup_entry", return_value=True),
    ):
        yield


def _entry(hass, *, version="auto", charger=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="reporting_settings",
        data={
            **MOCK_CONFIG_CS,
            CONF_OCPP_VERSION: version,
            CONF_CPIDS: [] if charger is None else [{"private_station": charger}],
        },
    )
    entry.add_to_hass(hass)
    return entry


def _charger(**changes):
    charger = {
        **MOCK_CONFIG_CP,
        CONF_NUM_CONNECTORS: 2,
        CONF_MONITORED_VARIABLES: "Power.Active.Import,Voltage",
    }
    charger.update(changes)
    return charger


async def _options(hass, entry, **changes):
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["step_id"] == "cp_settings"
    # Supply all visible values, as the HA frontend does when saving the form.
    values = {str(key): key.default() for key in form["data_schema"].schema}
    values.update(changes)
    return await hass.config_entries.options.async_configure(
        form["flow_id"], user_input=values
    )


async def test_discovery_enables_reporting_with_separate_auth(hass):
    """The discovered charger retains its local settings and separate credentials."""
    entry = _entry(hass)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"entry": entry, "cp_id": "private_station", CONF_NUM_CONNECTORS: 2},
    )
    assert flow["step_id"] == "cp_user"
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        user_input={
            **MOCK_CONFIG_CP,
            CONF_REPORTING_ENABLED: True,
            CONF_REPORTING_URL: "wss://provider.example/ocpp",
            CONF_REPORTING_USERNAME: "provider-user",
            CONF_REPORTING_PASSWORD: "private-password",
            CONF_REPORTING_QUEUE_LIMIT: 345,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    stored = entry.data[CONF_CPIDS][0]["private_station"]
    assert stored[CONF_CPID] == MOCK_CONFIG_CP[CONF_CPID]
    assert stored[CONF_NUM_CONNECTORS] == 2
    assert stored[CONF_REPORTING_URL] == "wss://provider.example/ocpp"
    assert stored[CONF_REPORTING_USERNAME] == "provider-user"
    assert stored[CONF_REPORTING_PASSWORD] == "private-password"
    assert stored[CONF_REPORTING_QUEUE_LIMIT] == 345
    assert stored[CONF_MONITORED_VARIABLES]


@pytest.mark.parametrize("version", ["2.0.1", "2.1"])
async def test_discovery_rejects_reporting_for_2x(hass, version):
    """Refuse reporting when discovery belongs to a pinned 2.x controller."""
    entry = _entry(hass, version=version)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"entry": entry, "cp_id": "private_station"},
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        user_input={
            **MOCK_CONFIG_CP,
            CONF_REPORTING_ENABLED: True,
            CONF_REPORTING_URL: "wss://provider.example/ocpp",
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "reporting_requires_v16"
    assert entry.data[CONF_CPIDS] == []


async def test_options_enable_then_disable_strips_credentials(hass):
    """Save credentials only while reporting is enabled, preserving local options."""
    entry = _entry(hass, charger=_charger())
    original = dict(entry.data[CONF_CPIDS][0]["private_station"])
    result = await _options(
        hass, entry,
        reporting_enabled=True,
        reporting_url="wss://provider.example/base",
        reporting_username="private-user",
        reporting_password="private-secret",
        reporting_queue_limit=20,
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    stored = entry.data[CONF_CPIDS][0]["private_station"]
    assert all(stored[key] == value for key, value in original.items())
    assert stored[CONF_REPORTING_ENABLED] is True
    assert stored[CONF_REPORTING_PASSWORD] == "private-secret"

    result = await _options(hass, entry, reporting_enabled=False)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    stored = entry.data[CONF_CPIDS][0]["private_station"]
    assert stored == original
    assert entry.options == {}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://provider.example/ocpp", "invalid_reporting_url"),
        ("wss://user:pass@provider.example/ocpp", "invalid_reporting_url"),
        ("wss://provider.example/ocpp?token=private", "invalid_reporting_url"),
        ("wss://provider.example/ocpp#private", "invalid_reporting_url"),
        ("", "invalid_reporting_url"),
    ],
)
async def test_options_reject_invalid_destination(hass, url, expected):
    """Refuse destinations that are not explicit, credential-free WebSockets."""
    entry = _entry(hass, charger=_charger())
    result = await _options(hass, entry, reporting_enabled=True, reporting_url=url)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == expected
    assert entry.data[CONF_CPIDS][0]["private_station"].get(CONF_REPORTING_ENABLED) is None


@pytest.mark.parametrize("version", ["2.0.1", "2.1"])
async def test_options_reject_2x(hass, version):
    """Keep 2.x charger options unchanged when reporting cannot run."""
    entry = _entry(hass, version=version, charger=_charger())
    result = await _options(
        hass, entry, reporting_enabled=True, reporting_url="wss://provider.example/base"
    )
    assert result["errors"]["base"] == "reporting_requires_v16"
    assert not entry.data[CONF_CPIDS][0]["private_station"].get(CONF_REPORTING_ENABLED)


async def test_options_reject_invalid_queue_limit(hass):
    """Reject a queue bound that cannot retain a pending report."""
    entry = _entry(hass, charger=_charger())
    with pytest.raises(InvalidData):
        await _options(
            hass, entry, reporting_enabled=True,
            reporting_url="wss://provider.example/base", reporting_queue_limit=0,
        )
    assert not entry.data[CONF_CPIDS][0]["private_station"].get(CONF_REPORTING_ENABLED)


async def test_diagnostics_never_export_secrets_or_payloads(hass):
    """The HA download must not dump config, reporter internals or session data."""
    charger = _charger(
        **{
            CONF_CPID: "private_charger_id",
            CONF_REPORTING_ENABLED: True,
            CONF_REPORTING_URL: "wss://token-in-path.example/secret-token",
            CONF_REPORTING_USERNAME: "private-user",
            CONF_REPORTING_PASSWORD: "private-password",
        }
    )
    entry = _entry(hass, charger=charger)
    reporter = SimpleNamespace(
        diagnostics=lambda: {
            "status": "blocked", "pending": 3, "buffered": 1,
            "source_connected": False, "last_acknowledged": 1234.0,
            "blocked_event": {
                "id": 17, "action": "StartTransaction", "state": "uncertain",
                "error": "private-error", "body": {"meterValue": "private-meter"},
            },
            "url": "wss://token-in-path.example/secret-token",
            "authorization": "Basic private-authorization",
            "journal": "private-journal", "idTag": "private-id-tag",
        }
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = SimpleNamespace(
        reporters={"private_station": reporter}
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result == {"chargers": [{
        "reporting_enabled": True, "status": "blocked", "pending": 3,
        "buffered": 1, "source_connected": False,
        "last_acknowledged": 1234.0,
        "blocked_event": {"id": 17, "action": "StartTransaction", "state": "uncertain"},
    }]}
    exported = json.dumps(result)
    for secret in (
        "private_station", "private_charger_id", "private-user", "private-password",
        "secret-token", "private-meter", "private-error", "private-journal",
        "private-authorization", "private-id-tag",
    ):
        assert secret not in exported


async def test_diagnostics_safe_without_live_central_system(hass):
    """Allow downloading diagnostics before the listener has been set up."""
    entry = _entry(hass, charger=_charger())
    assert await async_get_config_entry_diagnostics(hass, entry) == {
        "chargers": [{"reporting_enabled": False}]
    }


def test_reporting_sensors_present_health_without_meter_payloads():
    """Keep offline health readable and timestamps native to HA."""
    reporter = SimpleNamespace(
        cpid="charger", diagnostics=lambda: {
            "status": "blocked", "pending": 2, "buffered": 0,
            "last_acknowledged": 1234.0, "source_connected": False,
            "blocked_event": {"id": 9, "action": "MeterValues", "state": "uncertain"},
            "integrity_error": None,
        }
    )
    status = ReportingSensor(reporter, "status")
    pending = ReportingSensor(reporter, "pending")
    last = ReportingSensor(reporter, "last_acknowledged")
    assert status.native_value == "blocked"
    assert status.extra_state_attributes["blocked_event"]["id"] == 9
    assert pending.native_value == 2
    assert pending.extra_state_attributes is None
    assert last.native_value == datetime.fromtimestamp(1234, UTC)
    assert last.unique_id == "ocpp.charger.reporting_last_acknowledged"


@pytest.mark.parametrize("version", ["2.0.1", "2.1"])
async def test_central_reconfigure_preserves_reporting_protocol(hass, version):
    """A central protocol change cannot silently invalidate an enabled reporter."""
    entry = _entry(hass, charger=_charger(
        reporting_enabled=True, reporting_url="wss://provider.example/ocpp",
    ))
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    values = {str(key): entry.data[str(key)] for key in form["data_schema"].schema}
    values[CONF_OCPP_VERSION] = version
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], user_input=values,
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "reporting_requires_v16"
    assert entry.data[CONF_OCPP_VERSION] == "auto"
