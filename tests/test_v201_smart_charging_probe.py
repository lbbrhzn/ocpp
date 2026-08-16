"""SmartCharging detection when the charger omits the optional Available flag.

SmartChargingCtrlr/Available is optional in OCPP 2.0.1. Treating its absence
as a denial dropped the SMART profile for chargers that plainly do implement
smart charging - the FoxESS A-series reports ProfileStackLevel, RateUnit and
PeriodsPerSchedule, and no Available - which in turn leaves
number.<cpid>_maximum_current uncreated, so charge current cannot be set.

Absence means unknown, so it is resolved by asking: GetCompositeSchedule is
read-only, where SetChargingProfile would mutate charger state on every
connect. An explicit false is a definite answer and is left alone.

Reservation is deliberately untouched: ReservationCtrlr is absent from such a
report entirely rather than merely missing Available, there is no read-only
probe for it (ReserveNow would create a reservation), and an absent component
is fair evidence the feature is genuinely unsupported.
"""

import asyncio
from types import SimpleNamespace

import pytest
from ocpp.exceptions import NotSupportedError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import Profiles
from custom_components.ocpp.ocppv201 import ChargePoint, InventoryReport

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


def _mk_cp(hass, force_smart_charging=False, refuses=(), answers=None):
    """Build a v201 ChargePoint with a scriptable charger.

    `refuses` holds request class names answered with a CallError, as an
    unimplemented message comes back; `answers` overrides the reply for a
    named request, to check a non-Accepted status still counts.
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
        force_smart_charging=force_smart_charging,
    )
    conn = SimpleNamespace(
        state=State.CLOSED,
        close=lambda: asyncio.sleep(0),
        subprotocol="ocpp2.0.1",
    )
    cp = ChargePoint("CP_A", conn, hass, entry, central, charger)
    cp.sent = []

    async def answer(req):
        name = type(req).__name__
        cp.sent.append(req)
        if name in refuses:
            raise NotSupportedError("not implemented")
        if answers and name in answers:
            return answers[name]
        return SimpleNamespace(status="Accepted")

    cp.call = answer
    return cp


def _sent(cp):
    """Return the request types sent, so the probe is visible."""
    return [type(r).__name__ for r in cp.sent]


@pytest.mark.asyncio
async def test_an_unreported_flag_is_resolved_by_probing(hass):
    """The reported case: the component is there, Available is not."""
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    features = await cp.get_supported_features()

    assert Profiles.SMART in features
    assert "GetCompositeSchedule" in _sent(cp)


@pytest.mark.asyncio
async def test_a_charger_refusing_the_probe_gets_no_smart(hass):
    """Control: the probe has to be capable of saying no.

    Without this the test above would pass even if SMART were simply assumed
    whenever the flag was absent - which is the behaviour rejected on #2035.
    """
    cp = _mk_cp(hass, refuses=["GetCompositeSchedule"])
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    features = await cp.get_supported_features()

    assert Profiles.SMART not in features


@pytest.mark.asyncio
async def test_a_rejected_schedule_still_proves_support(hass):
    """Rejected answers the schedule request, not the question of support.

    A charger may legitimately decline to compute a schedule - no profile
    installed, or no grid-connection total for evseId 0 - while implementing
    smart charging perfectly well. Only a CallError denies the message.
    """
    cp = _mk_cp(
        hass,
        answers={"GetCompositeSchedule": SimpleNamespace(status="Rejected")},
    )
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    features = await cp.get_supported_features()

    assert Profiles.SMART in features


@pytest.mark.asyncio
async def test_an_explicit_false_is_respected_without_probing(hass):
    """A charger that says no has answered; do not talk over it."""
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(
        evse_count=1, connector_count=[1], smart_charging_available=False
    )

    features = await cp.get_supported_features()

    assert Profiles.SMART not in features
    assert "GetCompositeSchedule" not in _sent(cp)


@pytest.mark.asyncio
async def test_an_explicit_true_is_taken_without_probing(hass):
    """Detection that already worked must not gain a round trip."""
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(
        evse_count=1, connector_count=[1], smart_charging_available=True
    )

    features = await cp.get_supported_features()

    assert Profiles.SMART in features
    assert "GetCompositeSchedule" not in _sent(cp)


@pytest.mark.asyncio
async def test_the_override_still_skips_the_probe(hass):
    """force_smart_charging already settles it, so asking is wasted."""
    cp = _mk_cp(hass, force_smart_charging=True)
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    features = await cp.get_supported_features()

    assert Profiles.SMART in features
    assert "GetCompositeSchedule" not in _sent(cp)


@pytest.mark.asyncio
async def test_the_override_still_rescues_a_charger_refusing_the_probe(hass):
    """The escape hatch has to outrank a probe that says no."""
    cp = _mk_cp(hass, force_smart_charging=True, refuses=["GetCompositeSchedule"])
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    features = await cp.get_supported_features()

    assert Profiles.SMART in features


@pytest.mark.asyncio
async def test_a_stalled_probe_costs_only_smart(hass):
    """Consistent with the other two probes: a stall drops its own profile.

    This probe is added after the timeout handling in #2039, so it must not
    reintroduce the escape that fix closed.
    """
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    async def stall(req):
        cp.sent.append(req)
        if type(req).__name__ == "GetCompositeSchedule":
            raise TimeoutError("Waited 10s for response on ...")
        return SimpleNamespace(status="Accepted")

    cp.call = stall

    features = await cp.get_supported_features()

    assert Profiles.SMART not in features
    assert Profiles.FW in features
    assert Profiles.REM in features


@pytest.mark.asyncio
async def test_a_missing_inventory_is_still_probed(hass):
    """No report at all is the same kind of unknown, not a denial."""
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._wait_inventory = asyncio.Event()
    cp._wait_inventory.set()

    features = await cp.get_supported_features()

    assert Profiles.SMART in features


@pytest.mark.asyncio
async def test_reservation_is_left_alone(hass):
    """RES stays keyed on the flag: no probe, and no assumption either.

    ReservationCtrlr is absent from these reports entirely, and there is no
    read-only way to test for it. Scoped this way on #2035.
    """
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])

    features = await cp.get_supported_features()

    assert Profiles.RES not in features
    assert "ReserveNow" not in _sent(cp)


@pytest.mark.asyncio
async def test_detection_from_a_real_report_still_short_circuits(hass):
    """Drive the actual parser, so the tri-state does not rot.

    smart_charging_available carries three states now. A regression that made
    the parser leave it None on an explicit "false" would be invisible above,
    because the probe would then answer and SMART would appear anyway.
    """
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._wait_inventory = asyncio.Event()

    cp.on_report(
        1,
        "2026-01-01T00:00:00Z",
        0,
        report_data=[
            {
                "component": {"name": "SmartChargingCtrlr"},
                "variable": {"name": "Available"},
                "variable_attribute": [{"value": "false"}],
            }
        ],
    )

    assert cp._inventory.smart_charging_available is False

    features = await cp.get_supported_features()

    assert Profiles.SMART not in features
    assert "GetCompositeSchedule" not in _sent(cp)
