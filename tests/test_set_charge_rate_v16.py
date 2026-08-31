"""Tests for the simplified OCPP 1.6 set_charge_rate implementation.

These tests use the production ChargePoint class (v1.6) and monkeypatch only
the collaborators set_charge_rate depends on:
- get_configuration(...)
- call(...)
- notify_ha(...)

They avoid any parallel/dummy implementation of ChargePoint.
"""

from types import SimpleNamespace

import pytest
from ocpp.v16.enums import (
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    Measurand,
)

from custom_components.ocpp.chargepoint import (
    Metric,
    _ConnectorAwareMetrics,
)
from custom_components.ocpp.const import DEFAULT_MAX_CURRENT
from custom_components.ocpp.enums import (
    ConfigurationKey as ckey,
    OcppMisc as om,
    Profiles as prof,
)
from custom_components.ocpp.ocppv16 import (
    ChargePoint as ChargePointv16,
    _allowed_charging_rate_units,
)


@pytest.fixture
def cp_v16():
    """Provide a minimally-initialized v1.6 ChargePoint instance.

    We bypass __init__ and set only the attributes used by set_charge_rate.
    """
    cp = object.__new__(ChargePointv16)  # type: ignore[misc]
    # What set_charge_rate reads:
    cp._attr_supported_features = prof.SMART  # can be overridden in tests
    cp._ocpp_version = "1.6"
    cp.active_transaction_id = 0
    cp._active_tx = {}
    cp._metrics = _ConnectorAwareMetrics()
    # set_charge_rate calls these (we’ll monkeypatch per-test):
    # - cp.get_configuration(key)
    # - cp.call(req)
    # - cp.notify_ha(msg)
    return cp


@pytest.mark.asyncio
async def test_custom_profile_path_exception_triggers_notify_and_returns_false(
    cp_v16, monkeypatch
):
    """1) When a custom profile is provided and the CP call raises, return False and notify HA."""
    # notify capture
    notices = []

    async def fake_notify(msg, title="Ocpp integration"):
        notices.append(msg)
        return True

    async def fake_call(_req):
        raise RuntimeError("boom")

    # get_configuration shouldn't be touched in this path
    async def fake_get_conf(_key):
        pytest.fail("get_configuration should not be called for custom profile")

    monkeypatch.setattr(cp_v16, "notify_ha", fake_notify)
    monkeypatch.setattr(cp_v16, "call", fake_call)
    monkeypatch.setattr(cp_v16, "get_configuration", fake_get_conf)

    profile = {
        "chargingProfileId": 123,
        "stackLevel": 1,
        "chargingProfileKind": ChargingProfileKindType.relative.value,
        "chargingProfilePurpose": ChargingProfilePurposeType.charge_point_max_profile.value,
        "chargingSchedule": {
            "chargingRateUnit": ChargingRateUnitType.amps.value,
            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16}],
        },
    }

    ok = await cp_v16.set_charge_rate(profile=profile, conn_id=2)
    assert ok is False
    assert len(notices) == 1
    assert "Set charging profile failed" in notices[0]


@pytest.mark.asyncio
async def test_smart_charging_not_supported_returns_false_no_notify(
    cp_v16, monkeypatch
):
    """2) If the charger doesn't advertise SMART profile, return False without notifications."""
    cp_v16._attr_supported_features = prof.NONE

    notices = []

    async def fake_notify(msg, title="Ocpp integration"):
        notices.append(msg)
        return True

    # get_configuration and call should not be called
    async def fake_get_conf(_key):
        pytest.fail("get_configuration should not be called when SMART not supported")

    async def fake_call(_req):
        pytest.fail("call should not be called when SMART not supported")

    monkeypatch.setattr(cp_v16, "notify_ha", fake_notify)
    monkeypatch.setattr(cp_v16, "get_configuration", fake_get_conf)
    monkeypatch.setattr(cp_v16, "call", fake_call)

    ok = await cp_v16.set_charge_rate(limit_amps=16, conn_id=2)
    assert ok is False
    assert notices == []


@pytest.mark.asyncio
async def test_cpmax_exception_falls_back_to_txdefault_accepted_returns_true(
    cp_v16, monkeypatch
):
    """3) CPMax path raises -> fallback to TxDefault which is accepted -> return True."""

    # Allow both A and stack level
    async def fake_get_conf(key: str):
        if key == ckey.charging_schedule_allowed_charging_rate_unit:
            return "Current"  # supports Amps
        if key == ckey.charge_profile_max_stack_level:
            return "2"
        pytest.fail(f"Unexpected get_configuration key: {key}")

    # First SetChargingProfile (CPMax connectorId=0) raises, second (TxDefault connectorId=2) accepted
    async def fake_call(req):
        purpose = req.cs_charging_profiles["chargingProfilePurpose"]
        if purpose == ChargingProfilePurposeType.charge_point_max_profile.value:
            raise RuntimeError("transport error")
        if purpose == ChargingProfilePurposeType.tx_default_profile.value:
            return SimpleNamespace(status=ChargingProfileStatus.accepted)
        return SimpleNamespace(status=ChargingProfileStatus.rejected)

    notices = []

    async def fake_notify(msg, title="Ocpp integration"):
        notices.append(msg)
        return True

    monkeypatch.setattr(cp_v16, "get_configuration", fake_get_conf)
    monkeypatch.setattr(cp_v16, "call", fake_call)
    monkeypatch.setattr(cp_v16, "notify_ha", fake_notify)

    ok = await cp_v16.set_charge_rate(limit_amps=16, conn_id=2)
    assert ok is True
    # No user-facing warning necessary when fallback succeeds
    assert notices == []


@pytest.mark.asyncio
async def test_cpmax_rejected_txdefault_accepted_returns_true(cp_v16, monkeypatch):
    """4) CPMax rejected -> TxDefault accepted -> return True."""

    async def fake_get_conf(key: str):
        if key == ckey.charging_schedule_allowed_charging_rate_unit:
            return "Current"
        if key == ckey.charge_profile_max_stack_level:
            return "3"
        pytest.fail(f"Unexpected get_configuration key: {key}")

    async def fake_call(req):
        purpose = req.cs_charging_profiles["chargingProfilePurpose"]
        if purpose == ChargingProfilePurposeType.charge_point_max_profile.value:
            return SimpleNamespace(status=ChargingProfileStatus.rejected)
        if purpose == ChargingProfilePurposeType.tx_default_profile.value:
            return SimpleNamespace(status=ChargingProfileStatus.accepted)
        return SimpleNamespace(status=ChargingProfileStatus.rejected)

    notices = []

    async def fake_notify(msg, title="Ocpp integration"):
        notices.append(msg)
        return True

    monkeypatch.setattr(cp_v16, "get_configuration", fake_get_conf)
    monkeypatch.setattr(cp_v16, "call", fake_call)
    monkeypatch.setattr(cp_v16, "notify_ha", fake_notify)

    ok = await cp_v16.set_charge_rate(limit_amps=10, conn_id=2)
    assert ok is True
    assert notices == []


def test_allowed_charging_rate_units_tokens():
    """Chargers report Current/Power, A/W, or mixed, with inconsistent case."""
    assert _allowed_charging_rate_units(None) == (True, False)
    assert _allowed_charging_rate_units("") == (True, False)
    assert _allowed_charging_rate_units("Unknown") == (True, False)
    assert _allowed_charging_rate_units("Current") == (True, False)
    assert _allowed_charging_rate_units("power") == (False, True)
    assert _allowed_charging_rate_units("Power") == (False, True)
    assert _allowed_charging_rate_units("A") == (True, False)
    assert _allowed_charging_rate_units("W") == (False, True)
    assert _allowed_charging_rate_units("Current,Power") == (True, True)
    assert _allowed_charging_rate_units("Current, Power") == (True, True)


def _schedule_limit(req):
    profile = req.cs_charging_profiles
    schedule = profile[om.charging_schedule]
    period = schedule[om.charging_schedule_period][0]
    return schedule[om.charging_rate_unit], period[om.limit]


async def _accept_first_profile(cp, monkeypatch, units: str):
    captured = []

    async def fake_get_conf(key: str):
        if key == ckey.charging_schedule_allowed_charging_rate_unit:
            return units
        if key == ckey.charge_profile_max_stack_level:
            return "1"
        pytest.fail(f"Unexpected get_configuration key: {key}")

    async def fake_call(req):
        captured.append(req)
        return SimpleNamespace(status=ChargingProfileStatus.accepted)

    monkeypatch.setattr(cp, "get_configuration", fake_get_conf)
    monkeypatch.setattr(cp, "call", fake_call)
    return captured


@pytest.mark.asyncio
async def test_power_only_charger_converts_slider_amps_to_watts(cp_v16, monkeypatch):
    """number.*_maximum_current sends amps; watt-only chargers must get watts.

    Regression for Huawei FusionCharge (ChargingScheduleAllowedChargingRateUnit=power):
    the slider used to send the default 22000 W, so the car never slowed down.
    """
    captured = await _accept_first_profile(cp_v16, monkeypatch, "power")
    voltage = Metric(230.0, "V")
    voltage.extra_attr = {"L1-N": 230.0, "L2-N": 230.0, "L3-N": 230.0}
    cp_v16._metrics[(1, Measurand.voltage.value)] = voltage

    ok = await cp_v16.set_charge_rate(limit_amps=16, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 11040.0  # 16 A * 230 V * 3 phases


@pytest.mark.asyncio
async def test_power_only_charger_does_not_send_default_22000_w(cp_v16, monkeypatch):
    """Without conversion the charger accepted 22000 W and kept offering 32 A."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "Power")

    ok = await cp_v16.set_charge_rate(limit_amps=10, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 2300.0  # 10 A * 230 V * 1 conservative default phase
    assert limit != 22000


@pytest.mark.asyncio
async def test_power_only_single_phase_uses_one_phase(cp_v16, monkeypatch):
    """A single L1-N voltage sample must convert as 1-phase, not 3-phase."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "power")
    voltage = Metric(230.0, "V")
    voltage.extra_attr = {"L1-N": 230.0}
    cp_v16._metrics[(1, Measurand.voltage.value)] = voltage

    ok = await cp_v16.set_charge_rate(limit_amps=16, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 3680.0  # 16 A * 230 V * 1 phase


@pytest.mark.asyncio
async def test_power_only_ignores_uncorrelated_cached_power_and_current(
    cp_v16, monkeypatch
):
    """Do not infer W/A from leftover Power.Offered and Current.Offered."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "power")
    cp_v16._metrics[(1, Measurand.power_offered.value)] = Metric(22.0, "kW")
    cp_v16._metrics[(1, Measurand.current_offered.value)] = Metric(16.0, "A")

    ok = await cp_v16.set_charge_rate(limit_amps=10, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 2300.0


@pytest.mark.asyncio
async def test_power_only_does_not_use_another_connectors_metrics(cp_v16, monkeypatch):
    """Connector 2 must not inherit connector 1 voltage or phase count."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "power")
    connector_1_voltage = Metric(400.0, "V")
    connector_1_voltage.extra_attr = {
        "L1-N": 230.0,
        "L2-N": 230.0,
        "L3-N": 230.0,
    }
    cp_v16._metrics[(1, Measurand.voltage.value)] = connector_1_voltage

    ok = await cp_v16.set_charge_rate(limit_amps=10, conn_id=2)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 2300.0


@pytest.mark.asyncio
async def test_power_only_limit_watts_passthrough(cp_v16, monkeypatch):
    """An explicit watt limit is sent unchanged to a Power-only charger."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "power")

    ok = await cp_v16.set_charge_rate(limit_watts=5000, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 5000.0


@pytest.mark.asyncio
async def test_current_only_still_sends_amps(cp_v16, monkeypatch):
    """Amp-capable chargers keep receiving the slider value in amps."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "Current")

    ok = await cp_v16.set_charge_rate(limit_amps=16, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.amps.value
    assert limit == 16.0


@pytest.mark.asyncio
async def test_current_only_converts_watts_to_amps(cp_v16, monkeypatch):
    """A watt service call is converted when the charger only accepts amps."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "Current")
    voltage = Metric(230.0, "V")
    voltage.extra_attr = {"L1-N": 230.0}
    cp_v16._metrics[(1, Measurand.voltage.value)] = voltage

    ok = await cp_v16.set_charge_rate(limit_watts=4600, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.amps.value
    assert limit == 20.0  # 4600 W / (230 V * 1 phase)


@pytest.mark.asyncio
async def test_unknown_rate_unit_defaults_to_current(cp_v16, monkeypatch):
    """Unrecognized ChargingScheduleAllowedChargingRateUnit falls back to amps."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "Unknown")

    ok = await cp_v16.set_charge_rate(limit_watts=2300, conn_id=1)
    assert ok is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.amps.value
    assert limit == 10.0


def test_lookup_metric_without_metrics_returns_none(cp_v16):
    """A ChargePoint with no metric store must not raise."""
    cp_v16._metrics = None
    assert cp_v16._lookup_metric(Measurand.voltage.value, 1) is None


def test_lookup_metric_invalid_connector_falls_back_to_one(cp_v16):
    """A non-integer connector id is treated as connector 1."""
    voltage = Metric(240.0, "V")
    cp_v16._metrics[(1, Measurand.voltage.value)] = voltage
    assert cp_v16._lookup_metric(Measurand.voltage.value, "bad") is voltage


def test_lookup_metric_skips_empty_values(cp_v16):
    """A present metric with no value is ignored."""
    cp_v16._metrics[(1, Measurand.voltage.value)] = Metric(None, "V")
    assert cp_v16._lookup_metric(Measurand.voltage.value, 1) is None


def test_line_voltage_rejects_non_numeric_and_out_of_range(cp_v16):
    """Implausible voltages fall back to 230 V."""
    cp_v16._metrics[(1, Measurand.voltage.value)] = Metric("n/a", "V")
    assert cp_v16._line_voltage(1) == 230.0
    cp_v16._metrics[(1, Measurand.voltage.value)] = Metric(12.0, "V")
    assert cp_v16._line_voltage(1) == 230.0


def test_watts_to_amps_zero_denominator_uses_default(cp_v16, monkeypatch):
    """A zero volt-amp product must not divide by zero."""
    monkeypatch.setattr(cp_v16, "_line_voltage", lambda _conn: 0.0)
    monkeypatch.setattr(cp_v16, "_phase_count", lambda _conn: 0)
    assert cp_v16._watts_to_amps(5000, 1) == float(DEFAULT_MAX_CURRENT)


@pytest.mark.asyncio
async def test_amp_charger_default_limit_when_no_value_given(cp_v16, monkeypatch):
    """Calling set_charge_rate() with no limit uses the default amp cap."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "Current")

    assert await cp_v16.set_charge_rate() is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.amps.value
    assert limit == float(DEFAULT_MAX_CURRENT)


@pytest.mark.asyncio
async def test_power_charger_default_limit_when_no_value_given(cp_v16, monkeypatch):
    """Calling set_charge_rate() with no limit uses the default watt cap."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "Power")

    assert await cp_v16.set_charge_rate() is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 22000.0


@pytest.mark.asyncio
async def test_dual_unit_charger_sends_explicit_watts(cp_v16, monkeypatch):
    """When both units are allowed, an explicit watt limit stays in watts."""
    captured = await _accept_first_profile(cp_v16, monkeypatch, "Current,Power")

    assert await cp_v16.set_charge_rate(limit_watts=7000, conn_id=1) is True
    unit, limit = _schedule_limit(captured[0])
    assert unit == ChargingRateUnitType.watts.value
    assert limit == 7000.0
