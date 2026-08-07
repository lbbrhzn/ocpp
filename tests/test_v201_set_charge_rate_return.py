"""set_charge_rate must report what the charger actually did, on OCPP 2.0.1.

Every exit path returned None, while the OCPP 1.6 implementation returns
True/False and api.set_max_charge_rate_amps passes that straight through.
number.py treats a falsy result as a rejection, so every successful current
change logged "Set current limit rejected by CP" even though the charger had
accepted the profile - and a real rejection was indistinguishable from it.

Reporting success is only half of it: a path that clears the profile has to
report what the charger made of the clear, and the threshold for clearing has
to be the configured maximum rather than a literal 32, or every request in
between silently applies no limit at all.
"""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from ocpp.v201.enums import (
    ChargingProfilePurposeEnumType,
    ChargingProfileStatusEnumType,
    ClearChargingProfileStatusEnumType,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.ocppv201 import ChargePoint

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


def _mk_cp(hass, status=ChargingProfileStatusEnumType.accepted, max_current=32):
    """Build a v201 ChargePoint whose charger answers with `status`."""
    data = {
        "host": "127.0.0.1",
        "port": 0,
        "csid": "cs",
        "cpids": [{"CP_A": {"cpid": "test_cpid"}}],
        "subprotocols": ["ocpp2.0.1"],
        "websocket_close_timeout": 5,
        "ssl": False,
        "websocket_ping_interval": 0.0,
        "websocket_ping_timeout": 0.01,
        "websocket_ping_tries": 0,
        "ssl_certfile_path": CONF_SSL_CERTFILE_PATH,
        "ssl_keyfile_path": CONF_SSL_KEYFILE_PATH,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    central = CentralSystemSettings(**data)
    charger = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=max_current,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    conn = SimpleNamespace(
        state=State.CLOSED,
        close=lambda: asyncio.sleep(0),
        subprotocol="ocpp2.0.1",
    )
    cp = ChargePoint("CP_A", conn, hass, entry, central, charger)
    cp.sent = []

    async def record(req):
        cp.sent.append(req)
        return SimpleNamespace(status=status, status_info="charger said no")

    cp.call = record
    return cp


def _sent(cp):
    """Return the request types sent, so 'applied' and 'cleared' are distinct."""
    return [type(r).__name__ for r in cp.sent]


@pytest.mark.asyncio
async def test_an_accepted_amp_limit_reports_success(hass):
    """The common case: number.py must not log a rejection for this."""
    cp = _mk_cp(hass)

    assert await cp.set_charge_rate(limit_amps=16) is True
    assert _sent(cp) == ["SetChargingProfile"]


@pytest.mark.asyncio
async def test_an_accepted_watt_limit_reports_success(hass):
    """The watt path shares the same exit."""
    cp = _mk_cp(hass)

    assert await cp.set_charge_rate(limit_watts=5000) is True
    assert _sent(cp) == ["SetChargingProfile"]


@pytest.mark.asyncio
async def test_an_explicit_profile_reports_success(hass):
    """A caller-supplied profile returns through its own exit."""
    cp = _mk_cp(hass)

    assert await cp.set_charge_rate(profile={"id": 1}) is True
    assert _sent(cp) == ["SetChargingProfile"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit_amps": 32},  # at the configured maximum
        {"limit_watts": 22000},  # at the maximum
        {},  # no limit given at all
    ],
)
async def test_clearing_the_limit_reports_success(hass, kwargs):
    """Removing a limit is a successful outcome, not a failure to apply one.

    The clear also has to stay scoped to the profile this integration owns.
    Now that every request at or above max_current reaches it, an unfiltered
    ClearChargingProfile would take TxProfile and TxDefaultProfile entries
    installed by the charger or another system down with it.
    """
    cp = _mk_cp(hass)

    assert await cp.set_charge_rate(**kwargs) is True
    assert _sent(cp) == ["ClearChargingProfile"]
    assert cp.sent[0].charging_profile_id is None
    assert cp.sent[0].charging_profile_criteria == {
        "charging_profile_purpose": ChargingProfilePurposeEnumType.charging_station_max_profile.value
    }


@pytest.mark.asyncio
async def test_a_limit_below_a_raised_maximum_is_applied_not_cleared(hass):
    """The clear threshold has to follow the configured maximum.

    number.<cpid>_maximum_current is bounded by max_current, which the config
    flow accepts as an unbounded int. Against a literal 32 every request in
    the 32..max_current band became a bare profile clear: the charger ran
    unrestricted while the slider showed the figure the user had asked for.
    """
    cp = _mk_cp(hass, max_current=63)

    assert await cp.set_charge_rate(limit_amps=40) is True
    assert _sent(cp) == ["SetChargingProfile"]


@pytest.mark.asyncio
async def test_a_request_at_a_raised_maximum_still_clears(hass):
    """At the maximum the request genuinely means "no restriction"."""
    cp = _mk_cp(hass, max_current=63)

    assert await cp.set_charge_rate(limit_amps=63) is True
    assert _sent(cp) == ["ClearChargingProfile"]


@pytest.mark.asyncio
async def test_a_refused_clear_is_not_reported_as_success(hass):
    """Clearing is a request like any other, and can be refused."""
    cp = _mk_cp(hass, status=ChargingProfileStatusEnumType.rejected)

    assert await cp.set_charge_rate(limit_amps=32) is False
    assert _sent(cp) == ["ClearChargingProfile"]


@pytest.mark.asyncio
async def test_nothing_to_clear_counts_as_success(hass):
    """Unknown means no such profile, which is the end state we wanted."""
    cp = _mk_cp(hass, status=ClearChargingProfileStatusEnumType.unknown)

    assert await cp.set_charge_rate(limit_amps=32) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit_amps": 16},  # the built-profile path
        {"profile": {"id": 1}},  # the caller-supplied path
    ],
)
async def test_a_rejected_profile_still_raises(hass, kwargs):
    """A refusal must keep surfacing the charger's own status message.

    number.py catches this and warns; converting it to a False return would
    lose the reason the charger gave, which is the whole argument for raising
    here rather than returning False.
    """
    cp = _mk_cp(hass, status=ChargingProfileStatusEnumType.rejected)

    with pytest.raises(HomeAssistantError) as excinfo:
        await cp.set_charge_rate(**kwargs)

    assert "charger said no" in str(excinfo.value.translation_placeholders)
