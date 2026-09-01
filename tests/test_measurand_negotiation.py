"""Tests for measurand negotiation when a charger rejects the full CSV.

Some chargers (e.g. Teltonika TeltoCharge EVC10100) reject the entire
MeterValuesSampledData CSV via ChangeConfiguration as soon as it contains a
single measurand they do not support, instead of accepting the subset they
do support. The previous behaviour fell back to whatever GetConfiguration
reported (often just the OCPP default, "Energy.Active.Import.Register"),
silently losing every measurand the charger actually supports.

These tests drive get_supported_measurands() against a fake charger that
mimics this all-or-nothing rejection, and expect the integration to
discover the maximal accepted subset via a divide-and-conquer search
instead of collapsing to the default.
"""

from types import SimpleNamespace

import pytest
from ocpp.v16 import call
from ocpp.v16.enums import ConfigurationStatus

from custom_components.ocpp.const import ChargerSystemSettings
from custom_components.ocpp.enums import ConfigurationKey as ckey
from custom_components.ocpp.ocppv16 import ChargePoint as ChargePointv16

# Mirrors what we found on a real Teltonika TeltoCharge EVC10100 unit.
TELTONIKA_SUPPORTED = {
    "Current.Import",
    "Current.Offered",
    "Energy.Active.Import.Register",
    "Power.Active.Import",
    "Temperature",
    "Voltage",
}


def _mk_cp(monitored_variables: str, autoconfig: bool = False):
    """Provide a minimally-initialized v1.6 ChargePoint for measurand tests."""
    cp = object.__new__(ChargePointv16)  # type: ignore[misc]
    cp.id = "teltonika_test"
    cp.settings = ChargerSystemSettings(
        cpid="teltonika_test",
        max_current=32,
        idle_interval=900,
        meter_interval=60,
        monitored_variables=monitored_variables,
        monitored_variables_autoconfig=autoconfig,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    return cp


def _teltonika_backend(cp, supported: set[str] = TELTONIKA_SUPPORTED):
    """Attach a fake call()/configure()/get_configuration() trio.

    ChangeConfiguration(MeterValuesSampledData=...) is Accepted only when
    every comma-separated token in the requested value is in `supported`;
    otherwise it is Rejected outright (all-or-nothing, like the real
    charger). configure() and get_configuration() record/report the last
    value the (fake) charger actually holds.
    """
    state = {"csv": "Energy.Active.Import.Register", "calls": 0, "configure_calls": []}

    async def fake_call(req):
        if (
            isinstance(req, call.ChangeConfiguration)
            and req.key == ckey.meter_values_sampled_data.value
        ):
            state["calls"] += 1
            tokens = [t for t in req.value.split(",") if t]
            if tokens and all(t in supported for t in tokens):
                state["csv"] = req.value
                return SimpleNamespace(status=ConfigurationStatus.accepted)
            return SimpleNamespace(status=ConfigurationStatus.rejected)
        return SimpleNamespace()

    async def fake_configure(key, value):
        state["configure_calls"].append((key, value))
        state["csv"] = value

    async def fake_get_configuration(key):
        return state["csv"]

    cp.call = fake_call
    cp.configure = fake_configure
    cp.get_configuration = fake_get_configuration
    cp._backend_state = state
    return state


@pytest.mark.asyncio
async def test_full_csv_rejected_negotiates_down_to_accepted_subset():
    """A charger rejecting the full list should still yield its real subset.

    Previously this collapsed to the charger's pre-existing default
    (Energy.Active.Import.Register alone) even though the charger actually
    supports 6 of the 22 requested measurands.
    """
    from custom_components.ocpp.const import MEASURANDS

    cp = _mk_cp(monitored_variables=",".join(MEASURANDS), autoconfig=False)
    state = _teltonika_backend(cp)

    result = await cp.get_supported_measurands()

    accepted = set(result.split(","))
    assert accepted == TELTONIKA_SUPPORTED
    # The negotiated subset must actually have been persisted on the charger.
    assert state["csv"] == result or set(state["csv"].split(",")) == accepted


@pytest.mark.asyncio
async def test_negotiation_is_not_used_when_full_list_is_accepted():
    """No need to bisect anything if the charger simply accepts everything."""
    desired = "Energy.Active.Import.Register,Voltage,Temperature"
    cp = _mk_cp(monitored_variables=desired, autoconfig=False)
    state = _teltonika_backend(
        cp, supported={"Energy.Active.Import.Register", "Voltage", "Temperature"}
    )

    result = await cp.get_supported_measurands()

    assert set(result.split(",")) == {
        "Energy.Active.Import.Register",
        "Voltage",
        "Temperature",
    }
    # A single ChangeConfiguration call should suffice for the happy path.
    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_negotiation_returns_empty_when_charger_supports_none_of_the_desired_set():
    """If literally nothing requested is supported, fall back to empty (not a crash)."""
    cp = _mk_cp(monitored_variables="RPM,SoC,Frequency", autoconfig=False)
    _teltonika_backend(cp, supported=set())

    result = await cp.get_supported_measurands()

    assert result == ""


@pytest.mark.asyncio
async def test_initial_call_raising_still_negotiates_a_subset():
    """A transport error on the first attempt should not abort negotiation."""
    from custom_components.ocpp.const import MEASURANDS

    cp = _mk_cp(monitored_variables=",".join(MEASURANDS), autoconfig=False)
    _teltonika_backend(cp)
    real_call = cp.call
    first = {"done": False}

    async def flaky_first_call(req):
        if not first["done"]:
            first["done"] = True
            raise TimeoutError("simulated transport failure")
        return await real_call(req)

    cp.call = flaky_first_call

    result = await cp.get_supported_measurands()

    assert set(result.split(",")) == TELTONIKA_SUPPORTED


@pytest.mark.asyncio
async def test_cross_measurand_interaction_falls_back_to_one_verified_half():
    """If the recombined subset is rejected as a whole, keep a verified half.

    Simulates a charger that accepts two independently-verified halves but
    rejects them recombined into a single request (an interaction/limit
    that only shows up when both are requested together).
    """
    cp = _mk_cp(
        monitored_variables="Voltage,Temperature,Current.Import,Power.Active.Import",
        autoconfig=False,
    )

    calls = []

    async def fake_call(req):
        if (
            isinstance(req, call.ChangeConfiguration)
            and req.key == ckey.meter_values_sampled_data.value
        ):
            tokens = tuple(sorted(t for t in req.value.split(",") if t))
            calls.append(tokens)
            full = tuple(
                sorted(
                    ["Voltage", "Temperature", "Current.Import", "Power.Active.Import"]
                )
            )
            if tokens == full:
                return SimpleNamespace(status=ConfigurationStatus.rejected)
            # Any strict, non-empty subset of the full list is accepted,
            # including each half individually - only the full recombined
            # set is refused.
            if tokens and set(tokens) < set(full):
                return SimpleNamespace(status=ConfigurationStatus.accepted)
            return SimpleNamespace(status=ConfigurationStatus.rejected)
        return SimpleNamespace()

    cp.call = fake_call
    cp.configure = lambda key, value: None

    async def _noop_configure(key, value):
        return None

    cp.configure = _noop_configure

    result = await cp.get_supported_measurands()

    # One of the two verified halves must survive; the full 4-item
    # recombination must never appear in the final accepted result.
    assert result != ""
    accepted = set(result.split(","))
    assert accepted.issubset(
        {"Voltage", "Temperature", "Current.Import", "Power.Active.Import"}
    )
    assert accepted != {
        "Voltage",
        "Temperature",
        "Current.Import",
        "Power.Active.Import",
    }
