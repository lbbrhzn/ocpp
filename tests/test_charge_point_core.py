"""Test various chargepoint core functions/exceptions."""

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from homeassistant.setup import async_setup_component

from custom_components.ocpp.chargepoint import (
    ChargePoint,
    OcppVersion,
    Metric,
    _ConnectorAwareMetrics as CAM,
    MeasurandValue,
)
from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_POWER_UNIT,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
)
from custom_components.ocpp.enums import (
    HAChargerDetails as cdet,
    HAChargerSession as csess,
)
from ocpp.messages import CallError
from ocpp.charge_point import ChargePoint as LibCP
from ocpp.exceptions import NotImplementedError as OcppNotImplementedError

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


# -----------------------------
# Helpers to build a CP instance
# -----------------------------
def _mk_entry_data():
    return {
        "host": "127.0.0.1",
        "port": 0,
        "csid": "cs",
        "cpids": [{"CP_A": {"cpid": "test_cpid"}}],
        "subprotocols": ["ocpp1.6"],
        "websocket_close_timeout": 5,
        "ssl": False,
        # required ping fields:
        "websocket_ping_interval": 0.0,
        "websocket_ping_timeout": 0.01,
        "websocket_ping_tries": 0,
        "ssl_certfile_path": CONF_SSL_CERTFILE_PATH,
        "ssl_keyfile_path": CONF_SSL_KEYFILE_PATH,
    }


def _mk_cp(hass, *, version=OcppVersion.V201):
    entry = MockConfigEntry(domain=DOMAIN, data=_mk_entry_data())
    centr = CentralSystemSettings(**entry.data)
    chg = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32.0,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    # Minimal fake connection
    conn = SimpleNamespace(state=State.CLOSED, close=lambda: asyncio.sleep(0))
    cp = ChargePoint("CP_A", conn, version, hass, entry, centr, chg)
    cp._metrics[(0, csess.meter_start.value)].value = None
    return cp


def test_connector_aware_metrics_core():
    """Test _ConnectorAwareMetrics API."""
    m = CAM()

    # set/get flat
    m["Voltage"] = Metric(230.0, "V")
    assert isinstance(m["Voltage"], Metric)
    assert m["Voltage"].value == 230.0

    # set/get per connector
    m[(2, "Voltage")] = Metric(231.0, "V")
    assert m[(2, "Voltage")].value == 231.0

    # connector mapping view
    assert isinstance(m[2], dict)
    assert "Voltage" in m[2]

    # __contains__ for tuple/int/str keys
    assert "Voltage" in m
    assert (2, "Voltage") in m
    assert 2 in m

    # type checks
    with pytest.raises(TypeError):
        m[(3, "X")] = ("not", "metric")
    with pytest.raises(TypeError):
        m[3] = Metric(1, "A")


@pytest.mark.asyncio
async def test_get_specific_response_raises_callerror(hass, monkeypatch):
    """Test _get_specific_response “unsilence” of CallError."""
    cp = _mk_cp(hass)

    async def fake_super(self, unique_id, timeout):
        # Simulate that the lib returns a CallError object
        # (which is normally "silenced" in the lib).
        return CallError(unique_id, "SomeError", "details")

    # Patch the lib's _get_specific_response so that our wrapper is hit
    monkeypatch.setattr(LibCP, "_get_specific_response", fake_super, raising=True)

    with pytest.raises(Exception) as ei:
        await cp._get_specific_response("uid-1", 1)
    assert "SomeError" in str(ei.value)


@pytest.mark.asyncio
async def test_async_update_device_info_updates_metrics_and_registry(hass):
    """Test async_update_device_info."""
    await async_setup_component(hass, "device_tracker", {})

    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="e1", title="e1")
    entry.add_to_hass(hass)
    await hass.async_block_till_done()

    central = CentralSystemSettings(
        csid="cs",
        host="127.0.0.1",
        port=9999,
        subprotocols=["ocpp1.6"],
        ssl=False,
        ssl_certfile_path="",
        ssl_keyfile_path="",
        websocket_close_timeout=1,
        websocket_ping_interval=0.1,
        websocket_ping_timeout=0.1,
        websocket_ping_tries=0,
    )
    charger = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32.0,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )

    class DummyConn:
        """Dummy connection."""

        state = None

    cp = ChargePoint(
        id="CP_ID",
        connection=DummyConn(),
        version=OcppVersion.V201,
        hass=hass,
        entry=entry,
        central=central,
        charger=charger,
    )

    await cp.async_update_device_info(
        serial="SER123",
        vendor="Acme",
        model="Model X",
        firmware_version="1.2.3",
    )

    assert cp._metrics[(0, cdet.model.value)].value == "Model X"
    assert cp._metrics[(0, cdet.vendor.value)].value == "Acme"
    assert cp._metrics[(0, cdet.firmware_version.value)].value == "1.2.3"
    assert cp._metrics[(0, cdet.serial.value)].value == "SER123"

    from homeassistant.helpers import device_registry

    dr = device_registry.async_get(hass)
    dev = dr.async_get_device({(DOMAIN, "CP_ID"), (DOMAIN, "test_cpid")})
    assert dev is not None
    assert dev.manufacturer == "Acme"
    assert dev.model == "Model X"
    assert dev.sw_version == "1.2.3"


def test_get_ha_metric_prefers_exact_entity(hass):
    """Test get_ha_metric lookup logic."""
    cp = _mk_cp(hass)
    # Seed states
    hass.states.async_set("sensor.test_cpid_voltage", "n/a")
    hass.states.async_set("sensor.test_cpid_connector_1_voltage", "229.5")

    # With connector_id=1 we should resolve the child entity
    assert cp.get_ha_metric("Voltage", connector_id=1) == "229.5"
    # With connector_id=None -> root entity
    assert cp.get_ha_metric("Voltage", connector_id=None) == "n/a"


def _mv(measurand, value, phase=None, unit=None, context=None, location=None):
    return MeasurandValue(measurand, value, phase, unit, context, location)


def test_process_phases_voltage_and_current_branches(hass):
    """Test process_phases: l-l → l-n conversion, summation and unit normalization."""
    cp = _mk_cp(hass)

    # Voltage line-to-line values (should average and divide by sqrt(3))
    bucket = [
        _mv("Voltage", 400.0, phase="L1-L2", unit="V"),
        _mv("Voltage", 399.0, phase="L2-L3", unit="V"),
        _mv("Voltage", 401.0, phase="L3-L1", unit="V"),
    ]
    cp.process_phases(bucket, connector_id=1)
    v_ln = cp._metrics[(1, "Voltage")].value
    assert pytest.approx(v_ln, rel=1e-3) == (400.0 + 399.0 + 401.0) / 3 / math.sqrt(3)
    assert cp._metrics[(1, "Voltage")].unit == "V"

    # Power.Active.Import in W should become kW when aggregated
    bucket2 = [
        _mv("Power.Active.Import", 1000.0, phase="L1", unit=DEFAULT_POWER_UNIT),
        _mv("Power.Active.Import", 2000.0, phase="L2", unit=DEFAULT_POWER_UNIT),
        _mv("Power.Active.Import", 3000.0, phase="L3", unit=DEFAULT_POWER_UNIT),
    ]
    cp.process_phases(bucket2, connector_id=2)
    p_kw = cp._metrics[(2, "Power.Active.Import")].value
    assert p_kw == (1000 + 2000 + 3000) / 1000  # -> 6 kW
    assert cp._metrics[(2, "Power.Active.Import")].unit == HA_POWER_UNIT


def test_get_energy_kwh_and_session_derive(hass):
    """Test get_energy_kwh + process_measurands path (EAIR Wh → kWh, derive Energy.Session)."""
    cp = _mk_cp(hass, version=OcppVersion.V201)  # != 1.6 to enable session derive

    # Starting meter (kWh)
    cp._metrics[(1, csess.meter_start.value)].value = 10.0
    cp._metrics[(1, csess.meter_start.value)].unit = HA_ENERGY_UNIT

    # Send EAIR in Wh (should normalize to kWh)
    mv = _mv(
        "Energy.Active.Import.Register", 10500.0, unit=DEFAULT_ENERGY_UNIT
    )  # 10.5 kWh
    cp.process_measurands([[mv]], is_transaction=True, connector_id=1)

    # EAIR normalized
    assert cp._metrics[(1, "Energy.Active.Import.Register")].value == 10.5
    assert cp._metrics[(1, "Energy.Active.Import.Register")].unit == HA_ENERGY_UNIT

    # Session energy derived = EAIR - meter_start
    assert cp._metrics[(1, csess.session_energy.value)].value == pytest.approx(
        0.5, 1e-12
    )
    assert cp._metrics[(1, csess.session_energy.value)].unit == HA_ENERGY_UNIT


@pytest.mark.asyncio
async def test_handle_call_wraps_notimplementederror_and_sends(hass):
    """Test _handle_call Path: NotImplementedError → _send(...)."""
    central = CentralSystemSettings(
        csid="cs",
        host="127.0.0.1",
        port=9999,
        subprotocols=["ocpp1.6"],
        ssl=False,
        ssl_certfile_path="",
        ssl_keyfile_path="",
        websocket_close_timeout=1,
        websocket_ping_interval=0.1,
        websocket_ping_timeout=0.1,
        websocket_ping_tries=0,
    )
    charger = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32.0,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )

    conn = SimpleNamespace(state=State.OPEN, close=lambda: None)

    cp = ChargePoint(
        "CP_ID",
        conn,
        OcppVersion.V201,
        hass,
        SimpleNamespace(entry_id="e1", data={}),
        central,
        charger,
    )

    # Patch the PARENT _handle_call to raise the OCPP NotImplementedError
    async def parent_raises(self, msg):
        raise OcppNotImplementedError("nope")

    sent = {}

    async def fake_send(payload):
        sent["payload"] = payload

    class DummyMsg:
        """Dummy message class."""

        def create_call_error(self, exc):
            """Create call error."""
            assert isinstance(exc, OcppNotImplementedError)
            return SimpleNamespace(to_json=lambda: "ERR_JSON")

    with (
        patch.object(LibCP, "_handle_call", parent_raises, create=True),
        patch.object(cp, "_send", fake_send),
    ):
        # Wrapper should CATCH the OCPP NotImplementedError and send CallError JSON
        await cp._handle_call(DummyMsg())

    assert sent.get("payload") == "ERR_JSON"
