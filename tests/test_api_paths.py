"""Test exceptions paths in api.py."""

import contextlib
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_OK, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from websockets import NegotiationError

from custom_components.ocpp.api import CentralSystem
from custom_components.ocpp.const import DOMAIN
from custom_components.ocpp.enums import (
    HAChargerServices as csvcs,
    HAChargerStatuses as cstat,
)
from custom_components.ocpp.chargepoint import Metric as M
from custom_components.ocpp.chargepoint import SetVariableResult

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
    cs.charge_points[cp_id] = cp
    cs.cpids[cpid] = cp_id
    return cp


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
    meas = cstat.status_connector.value
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
