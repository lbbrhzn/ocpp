"""A capability probe that goes unanswered must cost only its own profile.

get_supported_features probes the charger with UpdateFirmware and
TriggerMessage. The ocpp library raises asyncio.TimeoutError - not an
OCPPError - when a charger accepts the call and then never replies, so
catching OCPPError alone let the timeout escape get_supported_features and
skip the assignment in fetch_supported_features. post_connect swallowed it
at DEBUG, so a charger that ignored one optional probe ended up with no
feature profiles at all and no feature metric, after a silent 10s stall.

SMART is the consequence that bites: without it number.<cpid>_maximum_current
is never created, so charge current cannot be set.
"""

import asyncio
from types import SimpleNamespace

import pytest
from ocpp.exceptions import OCPPError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import HAChargerDetails as cdet
from custom_components.ocpp.enums import Profiles
from custom_components.ocpp.ocppv201 import ChargePoint, InventoryReport

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH

# What ocpp.charge_point.ChargePoint.call raises when a charger accepts the
# request and never answers it. The library names it asyncio.TimeoutError,
# which has been an alias of the builtin since Python 3.11.
_TIMEOUT = TimeoutError("Waited 10s for response on ...")


def _mk_cp(hass, stalls_on=(), raises=None):
    """Build a v201 ChargePoint whose charger stalls on the named probes.

    `stalls_on` holds request class names that never get a reply; `raises`
    replaces the timeout with another exception, to check the handler is not
    wider than it should be.
    """
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
        max_current=32,
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
    # Seeding the inventory short-circuits _get_inventory, so the only calls
    # made are the two capability probes. SMART and AUTH come from the report,
    # and are what a probe timeout used to take down with it.
    cp._inventory = InventoryReport(
        evse_count=1,
        connector_count=[1],
        smart_charging_available=True,
        local_auth_available=True,
    )

    async def answer(req):
        if type(req).__name__ in stalls_on:
            raise raises if raises is not None else _TIMEOUT
        return SimpleNamespace(status="Accepted")

    cp.call = answer
    return cp


@pytest.mark.asyncio
async def test_a_stalled_firmware_probe_costs_only_that_profile(hass):
    """FW is the probed capability, so FW is all that may be lost."""
    cp = _mk_cp(hass, stalls_on=["UpdateFirmware"])

    features = await cp.get_supported_features()

    assert Profiles.FW not in features
    assert Profiles.REM in features
    assert Profiles.SMART in features
    assert Profiles.AUTH in features
    assert Profiles.CORE in features


@pytest.mark.asyncio
async def test_a_stalled_trigger_probe_costs_only_that_profile(hass):
    """The second probe has to be independent of the first."""
    cp = _mk_cp(hass, stalls_on=["TriggerMessage"])

    features = await cp.get_supported_features()

    assert Profiles.REM not in features
    assert Profiles.FW in features
    assert Profiles.SMART in features


@pytest.mark.asyncio
async def test_both_probes_stalling_still_yields_the_detected_profiles(hass):
    """A charger answering neither probe keeps what the report established."""
    cp = _mk_cp(hass, stalls_on=["UpdateFirmware", "TriggerMessage"])

    features = await cp.get_supported_features()

    assert features == Profiles.CORE | Profiles.SMART | Profiles.AUTH


@pytest.mark.asyncio
async def test_the_feature_metric_is_still_written(hass):
    """The user-visible damage was here, not in get_supported_features.

    The exception escaped before the assignment in fetch_supported_features,
    so sensor.<cpid>_features stayed empty and _attr_supported_features stayed
    NONE - and post_connect logged it at DEBUG, so nothing said why.
    """
    cp = _mk_cp(hass, stalls_on=["UpdateFirmware"])

    await cp.fetch_supported_features()

    assert cp._attr_supported_features == cp._metrics[(0, cdet.features.value)].value
    assert Profiles.SMART in cp._attr_supported_features


@pytest.mark.asyncio
async def test_post_connect_is_not_aborted_by_a_stalled_probe(hass):
    """post_connect must get past features to the rest of its setup.

    It catches Exception and logs at DEBUG, so an escaping timeout left the
    connector count uninitialised too - visible as missing connector entities
    rather than as an error.
    """
    cp = _mk_cp(hass, stalls_on=["UpdateFirmware"])

    await cp.post_connect()

    assert cp.post_connect_success is True


@pytest.mark.asyncio
async def test_a_refusal_is_still_handled(hass):
    """Control: a charger that answers "not supported" behaved correctly.

    This is the path that already worked, and the new handler must not be
    reached for it - a refusal is a definite no, not a stall.
    """
    cp = _mk_cp(hass, stalls_on=["UpdateFirmware"], raises=OCPPError("not supported"))

    features = await cp.get_supported_features()

    assert Profiles.FW not in features
    assert Profiles.REM in features


@pytest.mark.asyncio
async def test_a_stall_is_logged_louder_than_a_refusal(hass, caplog):
    """Silence was half the bug: 10s of nothing, then a DEBUG line.

    A refusal is a definite answer and stays at INFO. A stall is a different
    event - the charger is unwell, or the probe is unsupported in a way it
    will not admit to - and the dropped profile has real consequences, so it
    warrants a warning that names what was lost.
    """
    cp = _mk_cp(hass, stalls_on=["UpdateFirmware"])

    with caplog.at_level("INFO", logger="custom_components.ocpp"):
        await cp.get_supported_features()

    stalls = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(stalls) == 1
    assert "UpdateFirmware" in stalls[0].getMessage()

    caplog.clear()
    refusing = _mk_cp(
        hass, stalls_on=["UpdateFirmware"], raises=OCPPError("not supported")
    )

    with caplog.at_level("INFO", logger="custom_components.ocpp"):
        await refusing.get_supported_features()

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


@pytest.mark.asyncio
async def test_a_closed_connection_still_propagates(hass):
    """The handler is widened for stalls only, deliberately.

    A dropped websocket means every later call fails too, so there is nothing
    to be gained by carrying on and recording a feature set derived from a
    charger that has gone. Pinning this keeps the fix from being generalised
    into a bare except that would hide it.
    """
    cp = _mk_cp(
        hass,
        stalls_on=["UpdateFirmware"],
        raises=ConnectionClosedError(None, None),
    )

    with pytest.raises(ConnectionClosedError):
        await cp.get_supported_features()
