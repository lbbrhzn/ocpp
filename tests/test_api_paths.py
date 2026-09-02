"""Test exceptions paths in api.py."""

import contextlib
import json
import logging
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from ocpp.v16.enums import ChargePointStatus

from homeassistant.const import STATE_OK, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import template as template_helper
from homeassistant.util.yaml.objects import NodeStrClass
from websockets import NegotiationError

from custom_components.ocpp.api import CHRGR_SERVICE_DATA_SCHEMA, CentralSystem
from custom_components.ocpp.const import DOMAIN
from custom_components.ocpp.enums import (
    HAChargerServices as csvcs,
    HAChargerStatuses as cstat,
)
from custom_components.ocpp.chargepoint import Metric as M
from custom_components.ocpp.chargepoint import SetVariableResult
from custom_components.ocpp.switch import ChargePointSwitch, SWITCHES

from tests.const import MOCK_CONFIG_DATA


class DummyCP:
    """Minimal fake ChargePoint for exercising CentralSystem API paths."""

    def __init__(self, *, status=STATE_OK, num_connectors=3, supported_features=0b101):
        """Initialize."""
        self.status = status
        self.num_connectors = num_connectors
        self.supported_features = supported_features
        self._metrics = {}
        # service call sinks
        self.calls = []

    # ---- services the API calls into ----
    async def set_charge_rate(self, **kw):
        """Set charge rate."""
        self.calls.append(("set_charge_rate", kw))
        return True

    async def set_availability(self, state, connector_id=None):
        """Set availability."""
        self.calls.append(
            ("set_availability", {"state": state, "connector_id": connector_id})
        )
        return True

    async def start_transaction(self, connector_id=None):
        """Start transaction."""
        self.calls.append(("start_transaction", {"connector_id": connector_id}))
        return True

    async def stop_transaction(self, connector_id: int | None = None):
        """Stop transaction."""
        self.calls.append(("stop_transaction", {}))
        return True

    async def reset(self):
        """Reset."""
        self.calls.append(("reset", {}))
        return True

    async def unlock(self, connector_id=None):
        """Unlock."""
        self.calls.append(("unlock", {"connector_id": connector_id}))
        return True

    async def trigger_custom_message(self, requested_message):
        """Trigger custom message."""
        self.calls.append(
            ("trigger_custom_message", {"requested_message": requested_message})
        )
        return True

    async def clear_profile(self):
        """Clear profile."""
        self.calls.append(("clear_profile", {}))
        return True

    async def update_firmware(self, url, delay):
        """Update firmware."""
        self.calls.append(("update_firmware", {"url": url, "delay": delay}))
        return True

    async def get_diagnostics(self, url):
        """Get diagnostics."""
        self.calls.append(("get_diagnostics", {"url": url}))
        return True

    async def data_transfer(self, vendor, message, data):
        """Handle data transfer."""
        self.calls.append(
            ("data_transfer", {"vendor": vendor, "message": message, "data": data})
        )
        return True

    async def configure(self, key, value):
        """Configure."""
        self.calls.append(("configure", {"key": key, "value": value}))
        # alternate responses by key to cover both branches
        return (
            SetVariableResult.reboot_required
            if key == "needs_reboot"
            else SetVariableResult.accepted
        )

    async def get_configuration(self, key):
        """Get configuration."""
        self.calls.append(("get_configuration", {"key": key}))
        return f"value-for:{key}"


def _install_dummy_cp(
    cs: CentralSystem, *, cpid="test_cpid", cp_id="CP_DUMMY", **kw
) -> DummyCP:
    cp = DummyCP(**kw)
    cp.id = cp_id
    cs.charge_points[cp_id] = cp
    cs.cpids[cpid] = cp_id
    return cp


def _available_central_system(hass) -> tuple[CentralSystem, DummyCP]:
    """Create a central system with one available charge point."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    return cs, _install_dummy_cp(cs, cpid="ok", cp_id="CP_OK")


@pytest.mark.asyncio
async def test_select_subprotocol_variants(hass):
    """Test select subprotocol variants."""
    # Create a MockConfigEntry with existing standard config
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    # client offers none -> None
    assert cs.select_subprotocol(None, []) is None

    # overlap -> pick shared
    shared = cs.subprotocols[0]
    assert cs.select_subprotocol(None, [shared, "other"]) == shared

    with pytest.raises(NegotiationError):
        cs.select_subprotocol(None, ["nope1", "nope2"])


@pytest.mark.asyncio
async def test_get_metric_all_fallbacks(hass):
    """Test all fallbacks in get_metric."""

    # Create a MockConfigEntry with existing standard config
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cp = _install_dummy_cp(cs, num_connectors=3)

    meas = "Voltage"
    # 1) explicit connector
    cp._metrics[(2, meas)] = M(230.0, "V")
    assert cs.get_metric("test_cpid", meas, connector_id=2) == 230.0

    # 2) charger level (0)
    cp._metrics[(0, meas)] = M(231.0, "V")
    assert cs.get_metric("test_cpid", meas) == 231.0

    # 3) flat legacy key
    cp._metrics[meas] = M(232.0, "V")
    # delete (0,measurand) so flat is used
    cp._metrics.pop((0, meas), None)
    assert cs.get_metric("test_cpid", meas) == 232.0

    # 4) fallback connector 1
    cp._metrics.pop(meas, None)
    cp._metrics[(1, meas)] = M(233.0, "V")
    assert cs.get_metric("test_cpid", meas) == 233.0

    # 5) scan 2..N
    # del_metric: remove via (0, meas) and flat fallback

    # Make sure earlier fallbacks don't win
    for k in [(0, meas), (1, meas), (2, meas)]:
        if k in cp._metrics:
            cp._metrics[k].value = None

    # Also remove/neutralize the legacy flat key if present
    with contextlib.suppress(KeyError):
        cp._metrics.pop(meas)

    # Now seed the value only on connector 3
    cp._metrics[(3, meas)] = cp._metrics.get((3, meas), M(None, None))
    cp._metrics[(3, meas)].value = 234.0
    cp._metrics[(3, meas)].unit = "V"

    # Ensure the CS thinks there are at least 3 connectors
    srv = cs.charge_points[cs.cpids["test_cpid"]]
    srv.num_connectors = max(getattr(srv, "num_connectors", 1) or 1, 3)

    assert cs.get_metric("test_cpid", meas) == 234.0


@pytest.mark.asyncio
async def test_get_units_and_attrs_fallbacks(hass):
    """Test fallbacks in get_units and get_extra_attrs."""

    # Create a MockConfigEntry with existing standard config
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cp = _install_dummy_cp(cs, num_connectors=3)

    meas = "Power.Active.Import"
    # units via (3, meas)
    cp._metrics[(3, meas)] = M(10.0, "W")
    cp._metrics[(3, meas)].__dict__["_ha_unit"] = "W"
    cp._metrics[(3, meas)].extra_attr = {"ctx": "Sample.Periodic"}

    # ensure earlier probes are empty/missing so it scans to c>=2
    assert cs.get_unit("test_cpid", meas) == "W"
    assert cs.get_ha_unit("test_cpid", meas) == "W"
    assert cs.get_extra_attr("test_cpid", meas) == {"ctx": "Sample.Periodic"}

    # explicit connector wins
    cp._metrics[(1, meas)] = M(11.0, "kW")
    cp._metrics[(3, meas)].__dict__["_ha_unit"] = "kW"
    cp._metrics[(1, meas)].extra_attr = {"src": "conn1"}
    assert cs.get_unit("test_cpid", meas, connector_id=1) == "kW"
    assert cs.get_ha_unit("test_cpid", meas, connector_id=1) == "kW"
    assert cs.get_extra_attr("test_cpid", meas, connector_id=1) == {"src": "conn1"}


@pytest.mark.asyncio
async def test_get_available_paths(hass):
    """Test paths in get_available."""

    # Create a MockConfigEntry with existing standard config
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    # charger unavailable by status for connector 0
    cp = _install_dummy_cp(cs, status=STATE_UNAVAILABLE)
    assert cs.get_available("test_cpid", connector_id=0) is False

    # specific connector via per-connector metric, charger available
    cp = _install_dummy_cp(cs, status=STATE_OK)
    meas = cstat.status_connector
    cp._metrics[(1, meas)] = M("Charging", None)
    assert cs.get_available("test_cpid", connector_id=1) is True

    # via flat extra_attr aggregator
    cp2 = _install_dummy_cp(cs, cpid="agg", cp_id="CP_AGG", status=STATE_OK)
    flat = M("Available", None)
    flat.extra_attr = {2: "Finishing"}
    cp2._metrics[meas] = flat
    assert cs.get_available("agg", connector_id=2) is True

    # fall back to charger status if no info
    assert cs.get_available("agg", connector_id=3) is True  # charger STATE_OK


@pytest.mark.asyncio
async def test_single_connector_availability_status_missing(hass):
    """Keep the switch off until either status source reports."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    _install_dummy_cp(cs, num_connectors=1)
    switch_desc = next(desc for desc in SWITCHES if desc.key == "availability")
    entity = ChargePointSwitch(cs, "test_cpid", switch_desc)

    assert cs.get_availability_status("test_cpid") is None
    assert entity.is_on is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(ChargePointStatus))
async def test_single_connector_availability_status_fallback(hass, status):
    """Fallback changes the status source without redefining switch semantics."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cp = _install_dummy_cp(cs, num_connectors=1)
    switch_desc = next(desc for desc in SWITCHES if desc.key == "availability")
    entity = ChargePointSwitch(cs, "test_cpid", switch_desc)

    cp._metrics[(1, cstat.status_connector)] = M(status.value, None)

    assert cs.get_availability_status("test_cpid") == status.value
    assert entity.is_on is (status is ChargePointStatus.available)


@pytest.mark.asyncio
async def test_station_status_takes_precedence_for_availability(hass):
    """Never override a reported station status with connector 1."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cp = _install_dummy_cp(cs, num_connectors=1)
    switch_desc = next(desc for desc in SWITCHES if desc.key == "availability")
    entity = ChargePointSwitch(cs, "test_cpid", switch_desc)

    cp._metrics[(0, cstat.status)] = M("Unavailable", None)
    cp._metrics[(1, cstat.status_connector)] = M("Available", None)
    assert cs.get_availability_status("test_cpid") == "Unavailable"
    assert entity.is_on is False

    cp._metrics[(0, cstat.status)].value = "Available"
    cp._metrics[(1, cstat.status_connector)].value = "Unavailable"
    assert cs.get_availability_status("test_cpid") == "Available"
    assert entity.is_on is True


@pytest.mark.asyncio
async def test_availability_status_does_not_fallback_for_multiple_connectors(hass):
    """Keep station status unknown when either topology source says multi."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cp = _install_dummy_cp(cs, num_connectors=2)
    cp._metrics[(1, cstat.status_connector)] = M("Available", None)

    assert cs.get_availability_status("test_cpid") is None

    # Runtime discovery starts at one; the configured count must still prevent
    # a transient fallback while a known multi-connector charger reconnects.
    cp.num_connectors = 1
    cp.settings = SimpleNamespace(num_connectors=2)
    assert cs.get_availability_status("test_cpid") is None
    assert cs.get_availability_status("missing") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("switch_key", "connector_status"),
    [
        ("charge_control", ChargePointStatus.charging.value),
        ("connnector_availability", ChargePointStatus.preparing.value),
    ],
)
async def test_other_switches_keep_using_their_metric_path(
    hass, monkeypatch, switch_key, connector_status
):
    """The availability resolver must not intercept other switch types."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cp = _install_dummy_cp(cs, num_connectors=1)
    cp._metrics[(1, cstat.status_connector)] = M(connector_status, None)

    def unexpected_availability_lookup(*args, **kwargs):
        raise AssertionError("availability resolver used by another switch")

    monkeypatch.setattr(cs, "get_availability_status", unexpected_availability_lookup)
    switch_desc = next(desc for desc in SWITCHES if desc.key == switch_key)
    entity = ChargePointSwitch(
        cs, "test_cpid", switch_desc, connector_id=1, flatten_single=True
    )

    assert entity.is_on is True


@pytest.mark.asyncio
async def test_supported_features_and_device_info(hass):
    """Test supported features and device info."""

    # Create a MockConfigEntry with existing standard config
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cp = _install_dummy_cp(cs)
    assert cs.get_supported_features("test_cpid") == cp.supported_features
    assert cs.get_supported_features("unknown") == 0
    assert cs.device_info() == {"identifiers": {(DOMAIN, cs.id)}}


@pytest.mark.asyncio
async def test_setters_when_missing_and_present(hass):
    """Test set_charger_state various conditions."""

    # Create a MockConfigEntry with existing standard config
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    # missing -> False
    assert await cs.set_max_charge_rate_amps("missing", 10.0) is False

    # present -> routes and returns True
    cp = _install_dummy_cp(cs)
    assert await cs.set_max_charge_rate_amps("test_cpid", 16.0, connector_id=2) is True
    assert ("set_charge_rate", {"limit_amps": 16.0, "conn_id": 2}) in cp.calls

    # set_charger_state branches
    await cs.set_charger_state(
        "test_cpid", csvcs.service_availability.name, True, connector_id=1
    )
    await cs.set_charger_state(
        "test_cpid", csvcs.service_charge_start.name, connector_id=2
    )
    await cs.set_charger_state("test_cpid", csvcs.service_charge_stop.name)
    await cs.set_charger_state("test_cpid", csvcs.service_reset.name)
    await cs.set_charger_state("test_cpid", csvcs.service_unlock.name, connector_id=3)
    kinds = [
        k
        for k, _ in cp.calls
        if k
        in {
            "set_availability",
            "start_transaction",
            "stop_transaction",
            "reset",
            "unlock",
        }
    ]
    assert set(kinds) == {
        "set_availability",
        "start_transaction",
        "stop_transaction",
        "reset",
        "unlock",
    }


@pytest.mark.asyncio
async def test_check_charger_available_decorator_and_services(hass):
    """Test the check_charger_available and services when cp not available."""

    # 1) CentralSystem without websocket
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    # 2) Register two CP: one OK and one UNAVAILABLE
    _install_dummy_cp(cs, cpid="ok", cp_id="CP_OK", status=STATE_OK)
    _install_dummy_cp(cs, cpid="bad", cp_id="CP_BAD", status=STATE_UNAVAILABLE)

    # 3) Minimal hass.data-structure (some handlers read config)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("config", {})

    # 4) Unavailable -> should throw HomeAssistantError
    with pytest.raises(HomeAssistantError):
        await cs.handle_clear_profile(
            SimpleNamespace(data={"devid": "bad"}),
        )

    # 5) Available -> handlers reach CP methods without exception
    await cs.handle_trigger_custom_message(
        SimpleNamespace(
            data={"devid": "ok", "requested_message": "StatusNotification"}
        ),
    )
    await cs.handle_clear_profile(
        SimpleNamespace(data={"devid": "ok"}),
    )
    await cs.handle_update_firmware(
        SimpleNamespace(
            data={"devid": "ok", "firmware_url": "http://x/fw.bin", "delay_hours": 2}
        ),
    )
    await cs.handle_get_diagnostics(
        SimpleNamespace(data={"devid": "ok", "upload_url": "http://u/diag"}),
    )
    await cs.handle_data_transfer(
        SimpleNamespace(
            data={"devid": "ok", "vendor_id": "V", "message_id": "M", "data": "D"}
        ),
    )

    # 6) set_charge_rate – test all three variants
    await cs.handle_set_charge_rate(
        SimpleNamespace(
            data={"devid": "ok", "custom_profile": "{'foo': 1, 'bar': 'x'}"}
        ),
    )
    await cs.handle_set_charge_rate(
        SimpleNamespace(data={"devid": "ok", "limit_watts": 3500, "conn_id": 1}),
    )
    await cs.handle_set_charge_rate(
        SimpleNamespace(data={"devid": "ok", "limit_amps": 10.5}),
    )

    # 7) configure + get_configuration – check return format
    resp = await cs.handle_configure(
        SimpleNamespace(data={"devid": "ok", "ocpp_key": "needs_reboot", "value": "1"}),
    )
    assert resp == {"reboot_required": True}

    resp = await cs.handle_configure(
        SimpleNamespace(data={"devid": "ok", "ocpp_key": "just_apply", "value": "x"}),
    )
    assert resp == {"reboot_required": False}

    resp = await cs.handle_get_configuration(
        SimpleNamespace(data={"devid": "ok", "ocpp_key": "Foo"}),
    )
    assert resp == {"value": "value-for:Foo"}


@pytest.mark.asyncio
async def test_custom_profile_mapping_bypasses_string_parsing(hass):
    """A structured profile reaches the charge point as the same mapping."""
    cs, cp = _available_central_system(hass)
    profile = {"id": 1, "transactionId": "session'42"}

    await cs.handle_set_charge_rate(
        SimpleNamespace(data={"devid": "ok", "conn_id": 2, "custom_profile": profile})
    )

    assert cp.calls == [("set_charge_rate", {"profile": profile, "conn_id": 2})]
    assert cp.calls[0][1]["profile"] is profile


@pytest.mark.asyncio
async def test_custom_profile_literal_yaml_string_is_parsed(hass):
    """Annotated YAML strings must not be sent raw to the OCPP layer."""
    cs, cp = _available_central_system(hass)
    profile = NodeStrClass('{"id":1,"transactionId":"session\'42"}')
    data = CHRGR_SERVICE_DATA_SCHEMA(
        {"devid": "ok", "conn_id": 1, "custom_profile": profile}
    )

    assert data["custom_profile"] is profile
    await cs.handle_set_charge_rate(SimpleNamespace(data=data))

    assert cp.calls == [
        (
            "set_charge_rate",
            {
                "profile": {"id": 1, "transactionId": "session'42"},
                "conn_id": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_valid_json_custom_profile_preserves_apostrophe(hass):
    """A legal apostrophe in a JSON string must not prevent dispatch."""
    cs, cp = _available_central_system(hass)

    await cs.handle_set_charge_rate(
        SimpleNamespace(
            data={
                "devid": "ok",
                "custom_profile": '{"id":1,"transactionId":"session\'42"}',
            }
        )
    )

    assert cp.calls == [
        (
            "set_charge_rate",
            {
                "profile": {"id": 1, "transactionId": "session'42"},
                "conn_id": 0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_valid_json_custom_profile_cannot_redirect_fields(hass):
    """Charger text inside valid JSON remains data rather than structure."""
    cs, cp = _available_central_system(hass)
    transaction_id = "x','id':2,'transactionId':'y"

    await cs.handle_set_charge_rate(
        SimpleNamespace(
            data={
                "devid": "ok",
                "custom_profile": (
                    "{\"id\":1,\"transactionId\":\"x','id':2,'transactionId':'y\"}"
                ),
            }
        )
    )

    assert cp.calls == [
        (
            "set_charge_rate",
            {
                "profile": {"id": 1, "transactionId": transaction_id},
                "conn_id": 0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_template_result_mapping_stays_structured(hass):
    """The service schema must not turn a rendered mapping back into text."""
    cs, cp = _available_central_system(hass)
    transaction_id = "x','id':2,'transactionId':'y"
    profile = {"id": 1, "transactionId": transaction_id}
    wrapper = template_helper._parse_result(repr(profile))

    assert isinstance(wrapper, dict)
    data = CHRGR_SERVICE_DATA_SCHEMA(
        {"devid": "ok", "conn_id": 2, "custom_profile": wrapper}
    )
    assert data["custom_profile"] is wrapper

    await cs.handle_set_charge_rate(SimpleNamespace(data=data))

    assert cp.calls == [("set_charge_rate", {"profile": profile, "conn_id": 2})]
    assert cp.calls[0][1]["profile"] is wrapper


@pytest.mark.asyncio
async def test_legacy_custom_profile_logs_payload_free_debug(hass, caplog):
    """Keep legacy parsing while making its use diagnosable without payloads."""
    cs, cp = _available_central_system(hass)
    transaction_id = "legacy-session"
    custom_profile = f"{{'id':2,'transactionId':'{transaction_id}'}}"

    with caplog.at_level(logging.DEBUG, logger="custom_components.ocpp"):
        await cs.handle_set_charge_rate(
            SimpleNamespace(data={"devid": "ok", "custom_profile": custom_profile})
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if "legacy single-quote compatibility" in record.getMessage()
    ]
    assert cp.calls == [
        (
            "set_charge_rate",
            {
                "profile": {"id": 2, "transactionId": transaction_id},
                "conn_id": 0,
            },
        )
    ]
    assert len(messages) == 1
    assert "CP_OK" in messages[0]
    assert custom_profile not in messages[0]
    assert transaction_id not in messages[0]


@pytest.mark.asyncio
async def test_invalid_custom_profile_reports_original_json_position(hass):
    """Syntax errors describe the caller's JSON and never dispatch a limit."""
    cs, cp = _available_central_system(hass)

    with pytest.raises(HomeAssistantError) as exc_info:
        await cs.handle_set_charge_rate(
            SimpleNamespace(
                data={
                    "devid": "ok",
                    "custom_profile": "{'id':",
                    "limit_amps": 16,
                }
            )
        )

    assert exc_info.value.translation_key == "invalid_custom_profile"
    message = exc_info.value.translation_placeholders["message"]
    assert "Expecting property name enclosed in double quotes" in message
    assert "line 1, column 2" in message
    assert "legacy single-quote compatibility parsing also failed" in message
    assert cp.calls == []


@pytest.mark.asyncio
async def test_invalid_json_without_apostrophe_skips_legacy_fallback(hass):
    """Plain malformed JSON reports its location without claiming a fallback."""
    cs, cp = _available_central_system(hass)

    with pytest.raises(HomeAssistantError) as exc_info:
        await cs.handle_set_charge_rate(
            SimpleNamespace(data={"devid": "ok", "custom_profile": '{"id":'})
        )

    message = exc_info.value.translation_placeholders["message"]
    assert exc_info.value.translation_key == "invalid_custom_profile"
    assert "Expecting value" in message
    assert "line 1, column 7" in message
    assert "legacy" not in message
    assert cp.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, RecursionError])
async def test_json_parser_limit_error_is_translated(hass, monkeypatch, error_type):
    """Non-syntax decoder limits must not escape as unexpected errors."""
    cs, cp = _available_central_system(hass)

    def exceed_parser_limit(_custom_profile):
        raise error_type("parser detail must not reach the caller")

    monkeypatch.setattr("custom_components.ocpp.api.json.loads", exceed_parser_limit)

    with pytest.raises(HomeAssistantError) as exc_info:
        await cs.handle_set_charge_rate(
            SimpleNamespace(data={"devid": "ok", "custom_profile": '{"id":1}'})
        )

    assert exc_info.value.translation_key == "invalid_custom_profile"
    assert exc_info.value.translation_placeholders["message"] == (
        "JSON could not be decoded within parser limits"
    )
    assert cp.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, RecursionError])
async def test_legacy_json_parser_limit_error_is_translated(
    hass, monkeypatch, error_type
):
    """A parser limit in the legacy candidate retains the original location."""
    cs, cp = _available_central_system(hass)
    parse_attempts = 0

    def exceed_legacy_parser_limit(custom_profile):
        nonlocal parse_attempts
        parse_attempts += 1
        if parse_attempts == 1:
            raise json.JSONDecodeError(
                "Expecting property name enclosed in double quotes",
                custom_profile,
                1,
            )
        raise error_type("parser detail must not reach the caller")

    monkeypatch.setattr(
        "custom_components.ocpp.api.json.loads", exceed_legacy_parser_limit
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await cs.handle_set_charge_rate(
            SimpleNamespace(data={"devid": "ok", "custom_profile": "{'id':1}"})
        )

    message = exc_info.value.translation_placeholders["message"]
    assert exc_info.value.translation_key == "invalid_custom_profile"
    assert "Expecting property name enclosed in double quotes" in message
    assert "line 1, column 2" in message
    assert "compatibility parsing exceeded JSON parser limits" in message
    assert "parser detail" not in message
    assert parse_attempts == 2
    assert cp.calls == []


@pytest.mark.asyncio
async def test_unexpected_custom_profile_type_is_rejected(hass):
    """Keep direct handler calls from forwarding an unsupported value type."""
    cs, cp = _available_central_system(hass)

    with pytest.raises(HomeAssistantError) as exc_info:
        await cs.handle_set_charge_rate(
            SimpleNamespace(data={"devid": "ok", "custom_profile": ["profile"]})
        )

    assert exc_info.value.translation_key == "invalid_custom_profile"
    assert exc_info.value.translation_placeholders["message"] == (
        "expected a mapping or JSON object, got list"
    )
    assert cp.calls == []


@pytest.mark.asyncio
async def test_legacy_non_object_is_rejected_without_success_log(hass, caplog):
    """A syntactically compatible scalar is not a successful legacy profile."""
    cs, cp = _available_central_system(hass)

    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.ocpp"),
        pytest.raises(HomeAssistantError),
    ):
        await cs.handle_set_charge_rate(
            SimpleNamespace(data={"devid": "ok", "custom_profile": "['profile']"})
        )

    assert not any(
        "legacy single-quote compatibility" in record.getMessage()
        for record in caplog.records
    )
    assert cp.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_profile", ["null", "[]", "42", "true", '"text"'])
async def test_non_object_custom_profile_is_rejected(hass, custom_profile):
    """Only JSON objects can cross the custom-profile service boundary."""
    cs, cp = _available_central_system(hass)

    with pytest.raises(HomeAssistantError) as exc_info:
        await cs.handle_set_charge_rate(
            SimpleNamespace(data={"devid": "ok", "custom_profile": custom_profile})
        )

    assert exc_info.value.translation_key == "invalid_custom_profile"
    assert "expected a JSON object" in str(
        exc_info.value.translation_placeholders["message"]
    )
    assert cp.calls == []


def test_del_metric_variants(hass):
    """Test the del_metric function."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)
    cpid = "test_cpid"
    cp = _install_dummy_cp(cs, cpid=cpid, num_connectors=3)

    # --- Case A: connector-scoped metric exists -> set to None
    meas_conn = "Voltage"
    cp._metrics[(1, meas_conn)] = M(230.0, "V")
    # sanity
    assert cs.get_metric(cpid, meas_conn, connector_id=1) == 230.0

    cs.del_metric(cpid, meas_conn, connector_id=1)
    assert cs.get_metric(cpid, meas_conn, connector_id=1) is None

    # --- Case B: (0, meas) missing => fallback to legacy flat key when conn==0
    meas_flat = "Power.Active.Import"
    if (0, meas_flat) in cp._metrics:
        del cp._metrics[(0, meas_flat)]
    cp._metrics[meas_flat] = M(123.0, "W")
    assert cs.get_metric(cpid, meas_flat) == 123.0

    cs.del_metric(cpid, meas_flat, connector_id=0)
    assert cs.get_metric(cpid, meas_flat) is None
    assert cp._metrics[meas_flat].value is None

    # --- Case C: unknown cpid -> returns None, no exception
    assert cs.del_metric("unknown_cpid", "Voltage") is None


@pytest.mark.asyncio
async def test_select_subprotocol_follows_server_order(hass):
    """Selection is deterministic and follows the server's preference order.

    This mirrors the documented websockets behaviour the override replaces:
    "pick the first one in the list declared the server". The previous code
    iterated a set() of the client's offer, so for a charger offering several
    subprotocols the result depended on set-iteration order and varied between
    handshakes (issue #2008).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    # DEFAULT_SUBPROTOCOLS order is ocpp1.6, ocpp2.0.1, ocpp2.1
    assert cs.subprotocols[0] == "ocpp1.6"
    # a dual-stack charger gets 1.6 regardless of the order it offers them in
    assert cs.select_subprotocol(None, ["ocpp1.6", "ocpp2.0.1"]) == "ocpp1.6"
    assert cs.select_subprotocol(None, ["ocpp2.0.1", "ocpp1.6"]) == "ocpp1.6"
    # a charger that cannot do 1.6 still gets its version
    assert cs.select_subprotocol(None, ["ocpp2.0.1"]) == "ocpp2.0.1"
    # unsupported entries are skipped
    assert cs.select_subprotocol(None, ["bogus", "ocpp2.0.1"]) == "ocpp2.0.1"
    # no subprotocol offered while 1.6 is advertised -> legacy 1.6 default
    assert cs.select_subprotocol(None, []) is None


@pytest.mark.asyncio
async def test_pinned_version_overrides_server_preference(hass):
    """Pinning 2.0.1 beats the default 1.6-first preference."""
    data = MOCK_CONFIG_DATA.copy()
    data["ocpp_version"] = "2.0.1"
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    cs = CentralSystem(hass, entry)

    assert cs.select_subprotocol(None, ["ocpp1.6", "ocpp2.0.1"]) == "ocpp2.0.1"
    assert cs.select_subprotocol(None, ["ocpp2.0.1", "ocpp1.6"]) == "ocpp2.0.1"


@pytest.mark.asyncio
async def test_resolve_subprotocols_auto_and_pinned(hass):
    """Pinning an OCPP version advertises only that version's subprotocol."""
    from custom_components.ocpp.const import (
        CentralSystemSettings,
        DEFAULT_SUBPROTOCOLS,
    )

    base = {
        "csid": "c",
        "host": "h",
        "port": "1",
        "ssl": False,
        "ssl_certfile_path": "",
        "ssl_keyfile_path": "",
        "websocket_close_timeout": 1,
        "websocket_ping_interval": 1,
        "websocket_ping_timeout": 1,
        "websocket_ping_tries": 1,
    }

    auto = CentralSystemSettings(**base, ocpp_version="auto")
    assert CentralSystem._resolve_subprotocols(auto) == DEFAULT_SUBPROTOCOLS

    for version in ("1.6", "2.0.1", "2.1"):
        pinned = CentralSystemSettings(**base, ocpp_version=version)
        assert CentralSystem._resolve_subprotocols(pinned) == [f"ocpp{version}"]


@pytest.mark.asyncio
async def test_pinned_version_restricts_negotiation(hass):
    """A pinned OCPP version forces negotiation and blocks fallback.

    Regression guard for issue #2008: a charger that advertises several OCPP
    versions must be held to the pinned one, and a charger that cannot offer
    the pinned version is rejected rather than silently falling back to a
    version the integration would then mis-validate.
    """
    data = MOCK_CONFIG_DATA.copy()
    data["ocpp_version"] = "2.0.1"
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    cs = CentralSystem(hass, entry)

    assert cs.subprotocols == ["ocpp2.0.1"]
    # charger offering both is held to 2.0.1 regardless of its own order
    assert cs.select_subprotocol(None, ["ocpp1.6", "ocpp2.0.1"]) == "ocpp2.0.1"
    assert cs.select_subprotocol(None, ["ocpp2.0.1", "ocpp1.6"]) == "ocpp2.0.1"
    # charger that can only do 1.6 is rejected (no silent fallback)
    with pytest.raises(NegotiationError):
        cs.select_subprotocol(None, ["ocpp1.6"])
    # charger offering NO subprotocol must also be rejected: accepting it
    # would default the connection to a v1.6 ChargePoint, poisoning the cache
    # for the follow-up pinned-version connection
    with pytest.raises(NegotiationError):
        cs.select_subprotocol(None, [])


def _make_ws(subprotocol, path="/CP_1"):
    """Build a minimal fake websocket for on_connect."""
    return SimpleNamespace(
        subprotocol=subprotocol,
        request=SimpleNamespace(path=path),
    )


@pytest.mark.asyncio
async def test_negotiated_ocpp_version(hass):
    """The negotiated version string is derived from the subprotocol."""
    assert CentralSystem._negotiated_ocpp_version(_make_ws("ocpp1.6")) == "1.6"
    assert CentralSystem._negotiated_ocpp_version(_make_ws("ocpp2.0.1")) == "2.0.1"
    assert CentralSystem._negotiated_ocpp_version(_make_ws("ocpp2.1")) == "2.1"
    # no subprotocol negotiated -> the 1.6 default
    assert CentralSystem._negotiated_ocpp_version(_make_ws(None)) == "1.6"


class _FakeReconnectCP:
    """Fake ChargePoint recording lifecycle calls for on_connect tests."""

    def __init__(self, ocpp_version):
        """Initialize."""
        self._ocpp_version = ocpp_version
        self.settings = SimpleNamespace(cpid="CP_1_cpid")
        self.stopped = False
        self.started = False
        self.reconnected_with = None

    async def stop(self):
        """Stop."""
        self.stopped = True

    async def start(self):
        """Start."""
        self.started = True

    async def reconnect(self, connection):
        """Reconnect."""
        self.reconnected_with = connection


@pytest.mark.asyncio
async def test_on_connect_rebuilds_on_version_change(hass, monkeypatch):
    """A reconnect with a different OCPP version rebuilds the ChargePoint.

    Regression test for issue #2008: the cached ChargePoint keeps the OCPP
    version (and validator/message set) it was first built with. Some chargers
    (e.g. FoxESS A-series) make a short-lived 1.6 probe connection right after
    a version switch, planting a v16 object; the real 2.0.1 connection must
    rebuild from its own negotiated subprotocol instead of reusing the stale
    object (which would validate 2.0.1 payloads against the 1.6 schema).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    old_cp = _FakeReconnectCP("1.6")
    cs.charge_points["CP_1"] = old_cp

    new_cp = _FakeReconnectCP("2.0.1")
    built = []

    def _fake_build(cp_id, websocket, cp_settings):
        built.append((cp_id, websocket.subprotocol, cp_settings))
        return new_cp

    monkeypatch.setattr(cs, "_build_charge_point", _fake_build)

    # charger reconnects negotiating 2.0.1 while cache holds a 1.6 CP
    await cs.on_connect(_make_ws("ocpp2.0.1"))

    assert old_cp.stopped is True
    assert built and built[0][1] == "ocpp2.0.1"
    assert built[0][2] is old_cp.settings  # settings carried over
    assert cs.charge_points["CP_1"] is new_cp
    assert new_cp.started is True
    # a rebuild is not a plain reconnect
    assert new_cp.reconnected_with is None


@pytest.mark.asyncio
async def test_on_connect_cancels_tasks_when_stop_fails(hass, monkeypatch):
    """A failing stop() must not leave the stale ChargePoint's tasks running.

    ChargePoint.stop() closes the websocket before cancelling its tasks, so if
    the close raises, cancellation never happens. The rebuild must cancel them
    itself, or the replaced instance keeps running monitor_connection against a
    charger that now belongs to a new ChargePoint.
    """

    class _StuckCP(_FakeReconnectCP):
        """Fake whose stop() fails the way a failed socket close would."""

        def __init__(self, ocpp_version):
            super().__init__(ocpp_version)
            self.tasks = [SimpleNamespace(cancelled=False)]
            for task in self.tasks:
                task.cancel = lambda t=task: setattr(t, "cancelled", True)

        async def stop(self):
            """Raise where a failed websocket close would, before cancelling."""
            raise OSError("connection close failed")

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    old_cp = _StuckCP("1.6")
    cs.charge_points["CP_1"] = old_cp
    new_cp = _FakeReconnectCP("2.0.1")
    monkeypatch.setattr(cs, "_build_charge_point", lambda *a, **kw: new_cp)

    # must not raise, and must still replace the charge point
    await cs.on_connect(_make_ws("ocpp2.0.1"))

    assert all(task.cancelled for task in old_cp.tasks), (
        "stale charge point's tasks must be cancelled when stop() fails"
    )
    assert cs.charge_points["CP_1"] is new_cp
    assert new_cp.started is True


@pytest.mark.asyncio
async def test_on_connect_reconnects_when_version_unchanged(hass, monkeypatch):
    """A reconnect with the same OCPP version reuses the cached ChargePoint."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    cp = _FakeReconnectCP("2.0.1")
    cs.charge_points["CP_1"] = cp

    def _fail_build(*args, **kwargs):
        raise AssertionError(
            "_build_charge_point must not be called on same-version reconnect"
        )

    monkeypatch.setattr(cs, "_build_charge_point", _fail_build)

    ws = _make_ws("ocpp2.0.1")
    await cs.on_connect(ws)

    assert cs.charge_points["CP_1"] is cp
    assert cp.reconnected_with is ws
    assert cp.stopped is False


# ---------------------------------------------------------------------------
# Regression tests: service routing across multiple central systems
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supplied_but_unresolved_devid_raises(hass):
    """A devid that was supplied and matches nothing must not pick a charger.

    The caller named a target, so running the action against a different one
    is worse than failing. Only an omitted devid falls back - see
    test_single_cp_fallback_for_missing_devid.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    first = _install_dummy_cp(cs, cpid="cp_a", cp_id="CP_A", status=STATE_OK)
    second = _install_dummy_cp(cs, cpid="cp_b", cp_id="CP_B", status=STATE_OK)

    with pytest.raises(HomeAssistantError):
        await cs.handle_clear_profile(
            SimpleNamespace(data={"devid": "completely_unknown_charger"}),
        )

    # Neither charger may be touched by a call that could not be resolved.
    assert not first.calls
    assert not second.calls


@pytest.mark.asyncio
async def test_supplied_but_unresolved_devid_raises_with_a_single_charger(hass):
    """The strict rule holds even when there is only one charger to pick."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    only = _install_dummy_cp(cs, cpid="only_cpid", cp_id="CP_ONLY", status=STATE_OK)

    with pytest.raises(HomeAssistantError):
        await cs.handle_clear_profile(SimpleNamespace(data={"devid": "not_this_one"}))

    assert not only.calls


@pytest.mark.asyncio
async def test_service_call_raises_when_no_charge_points(hass):
    """With no charger to fall back to the call must fail explicitly."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    with pytest.raises(HomeAssistantError):
        await cs.handle_clear_profile(SimpleNamespace(data={"devid": "anything"}))


@pytest.mark.asyncio
async def test_single_cp_fallback_for_missing_devid(hass):
    """Backwards compatibility: a single CP falls back when devid is missing.

    Legacy service calls did not always include a devid.  With exactly one
    charge point on the central system the intended target is unambiguous,
    so keep the historical fallback behaviour.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    cp = _install_dummy_cp(cs, cpid="only_cpid", cp_id="CP_ONLY", status=STATE_OK)

    # Key absent entirely, and present but blank: both mean "no target given".
    await cs.handle_clear_profile(SimpleNamespace(data={}))
    await cs.handle_clear_profile(SimpleNamespace(data={"devid": ""}))
    assert [k for k, _ in cp.calls] == ["clear_profile", "clear_profile"]


@pytest.mark.asyncio
async def test_devid_resolves_by_cpid(hass):
    """Service call with devid equal to the HA cpid routes to the correct charger."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    cp = _install_dummy_cp(cs, cpid="garage_charger", cp_id="CP_001", status=STATE_OK)

    await cs.handle_clear_profile(SimpleNamespace(data={"devid": "garage_charger"}))
    assert any(k == "clear_profile" for k, _ in cp.calls)


@pytest.mark.asyncio
async def test_devid_resolves_by_cp_id(hass):
    """Service call with devid equal to the raw OCPP cp_id routes to the correct charger."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    cp = _install_dummy_cp(cs, cpid="garage_charger", cp_id="CP_001", status=STATE_OK)

    await cs.handle_clear_profile(SimpleNamespace(data={"devid": "CP_001"}))
    assert any(k == "clear_profile" for k, _ in cp.calls)


@pytest.mark.asyncio
async def test_multi_central_system_routing_via_global_resolver(hass):
    """Two CentralSystem instances must not interfere.

    _resolve_central_system must route a devid to exactly the CS that owns
    the charger.
    """
    from custom_components.ocpp import _resolve_central_system

    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry_b = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs_a = CentralSystem(hass, entry_a)
    cs_b = CentralSystem(hass, entry_b)

    _install_dummy_cp(cs_a, cpid="charger_a", cp_id="CP_A", status=STATE_OK)
    _install_dummy_cp(cs_b, cpid="charger_b", cp_id="CP_B", status=STATE_OK)

    # Register both in hass.data so the resolver can find them.
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry_a.entry_id] = cs_a
    hass.data[DOMAIN][entry_b.entry_id] = cs_b

    # devid "charger_a" (cpid) must resolve to cs_a
    assert _resolve_central_system(hass, "charger_a") is cs_a
    # devid "CP_B" (cp_id) must resolve to cs_b
    assert _resolve_central_system(hass, "CP_B") is cs_b
    # devid "charger_b" (cpid in cs_b) must resolve to cs_b
    assert _resolve_central_system(hass, "charger_b") is cs_b
    # unknown devid must raise
    with pytest.raises(HomeAssistantError):
        _resolve_central_system(hass, "nonexistent")
    # empty devid is ambiguous with multiple CSes and must raise
    with pytest.raises(HomeAssistantError):
        _resolve_central_system(hass, "")


@pytest.mark.asyncio
async def test_empty_devid_resolves_to_only_central_system(hass):
    """Backwards compatibility: empty devid resolves when only one CS is loaded."""
    from custom_components.ocpp import _resolve_central_system

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs = CentralSystem(hass, entry)

    _install_dummy_cp(cs, cpid="only_cpid", cp_id="CP_ONLY", status=STATE_OK)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = cs

    assert _resolve_central_system(hass, "") is cs


@pytest.mark.asyncio
async def test_unload_preserves_services_while_second_cs_active(hass):
    """Unloading one entry must not remove domain services while a second is still active."""
    from custom_components.ocpp import _resolve_central_system, _DOMAIN_SERVICE_NAMES

    hass.data.setdefault(DOMAIN, {})

    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry_b = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs_a = CentralSystem(hass, entry_a)
    cs_b = CentralSystem(hass, entry_b)

    _install_dummy_cp(cs_a, cpid="charger_a", cp_id="CP_A", status=STATE_OK)
    _install_dummy_cp(cs_b, cpid="charger_b", cp_id="CP_B", status=STATE_OK)

    hass.data[DOMAIN][entry_a.entry_id] = cs_a
    hass.data[DOMAIN][entry_b.entry_id] = cs_b
    hass.data[DOMAIN][_DOMAIN_SERVICE_NAMES] = ["configure"]

    # Simulate entry_a being removed (as async_unload_entry would do after
    # successful platform unload).
    hass.data[DOMAIN].pop(entry_a.entry_id)

    # cs_b is still registered, so devid resolution must still work.
    assert _resolve_central_system(hass, "charger_b") is cs_b
    assert _resolve_central_system(hass, "CP_B") is cs_b


@pytest.mark.asyncio
async def test_duplicate_cp_id_across_central_systems_is_rejected(hass):
    """The same OCPP cp_id in two systems must not resolve to an arbitrary one.

    cp_id is reported by the charger, so two central systems can each own one
    with the factory default name. Picking the first would be a coin flip on a
    mutating service call, so the resolver refuses and the user is expected to
    use their unique cpid instead.
    """
    from custom_components.ocpp import _resolve_central_system

    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry_b = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs_a = CentralSystem(hass, entry_a)
    cs_b = CentralSystem(hass, entry_b)

    # Same OCPP id on both sides, distinct HA cpids - what the config flow allows.
    _install_dummy_cp(cs_a, cpid="garage", cp_id="CP_1", status=STATE_OK)
    _install_dummy_cp(cs_b, cpid="driveway", cp_id="CP_1", status=STATE_OK)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry_a.entry_id] = cs_a
    hass.data[DOMAIN][entry_b.entry_id] = cs_b

    with pytest.raises(HomeAssistantError):
        _resolve_central_system(hass, "CP_1")

    # The unique cpid still resolves each side unambiguously.
    assert _resolve_central_system(hass, "garage") is cs_a
    assert _resolve_central_system(hass, "driveway") is cs_b


@pytest.mark.asyncio
async def test_cpid_wins_over_a_colliding_cp_id(hass):
    """A cpid must not be shadowed by another system's identical cp_id.

    cpid is kept unique by the config flow, cp_id is not, so the unique
    identifier has to be matched first regardless of load order.
    """
    from custom_components.ocpp import _resolve_central_system

    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry_b = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    cs_a = CentralSystem(hass, entry_a)
    cs_b = CentralSystem(hass, entry_b)

    # cs_a owns a charger whose raw cp_id happens to equal cs_b's cpid.
    _install_dummy_cp(cs_a, cpid="charger_a", cp_id="shared_name", status=STATE_OK)
    _install_dummy_cp(cs_b, cpid="shared_name", cp_id="CP_B", status=STATE_OK)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry_a.entry_id] = cs_a
    hass.data[DOMAIN][entry_b.entry_id] = cs_b

    # cs_a is registered first, but the cpid owner must win.
    assert _resolve_central_system(hass, "shared_name") is cs_b
