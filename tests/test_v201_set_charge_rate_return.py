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
    ChargingProfileKindEnumType,
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
from custom_components.ocpp.ocppv201 import ChargePoint, InventoryReport

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


def _mk_cp(
    hass,
    status=ChargingProfileStatusEnumType.accepted,
    max_current=32,
    charge_point_max_profile_absolute=False,
):
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
        charge_point_max_profile_absolute=charge_point_max_profile_absolute,
    )
    conn = SimpleNamespace(
        state=State.CLOSED,
        close=lambda: asyncio.sleep(0),
        subprotocol="ocpp2.0.1",
    )
    cp = ChargePoint("CP_A", conn, hass, entry, central, charger)
    # A cached report keeps the on-use refresh out of the recorded traffic.
    cp._inventory = InventoryReport()
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
    cp._inventory = None

    async def inventory_must_not_be_read():
        raise AssertionError("clearing a profile does not need rate-unit inventory")

    cp._get_inventory = inventory_must_not_be_read

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


# --- #2101: the managed station profile must target EVSE 0 -------------------
#
# ChargingStationMaxProfile describes the whole station and OCPP 2.0.1 requires
# it on evseId 0. The managed path used to route it to whatever EVSE conn_id
# mapped to, so a compliant charger could refuse every call that passed a
# positive connector. The invariant: every integration-generated
# ChargingStationMaxProfile is sent with evse_id=0. A caller-supplied profile
# keeps the requested connector's mapping, because its purpose decides which
# EVSE is valid.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [{"limit_amps": 16}, {"limit_watts": 5000}],
    ids=["amps", "watts"],
)
async def test_a_managed_limit_targets_the_station_whatever_the_connector(hass, kwargs):
    """A managed limit below the maximum goes to EVSE 0, whichever connector is named.

    Seeding global 2 -> EVSE 7 makes the wrong answer distinct from both 0
    and the mapper's own (n, 1) fallback, so this cannot pass by coincidence:
    the old code sent this request to EVSE 7.
    """
    cp = _mk_cp(hass)
    cp._global_to_evse = {2: (7, 1)}

    assert await cp.set_charge_rate(conn_id=2, **kwargs) is True

    # Exactly one request and nothing behind it: a future rejection fallback
    # must be a deliberate change that updates this line.
    assert _sent(cp) == ["SetChargingProfile"]
    req = cp.sent[0]
    assert req.evse_id == 0
    assert (
        req.charging_profile["charging_profile_purpose"]
        == ChargingProfilePurposeEnumType.charging_station_max_profile.value
    )


@pytest.mark.asyncio
async def test_station_max_profile_relative_by_default(hass):
    """Default settings keep the ChargingStationMaxProfile relative, with no startSchedule."""
    cp = _mk_cp(hass)

    assert await cp.set_charge_rate(limit_amps=16) is True

    profile = cp.sent[0].charging_profile
    assert (
        profile["charging_profile_kind"] == ChargingProfileKindEnumType.relative.value
    )
    assert "start_schedule" not in profile["charging_schedule"][0]


@pytest.mark.asyncio
async def test_station_max_profile_absolute_when_enabled(hass):
    """The advanced option anchors the ChargingStationMaxProfile at a fixed absolute start.

    Some chargers (e.g. Autel MaxiCharger) reject a relative
    ChargingStationMaxProfile outright; this lets a user opt into the
    absolute form those chargers accept.
    """
    cp = _mk_cp(hass, charge_point_max_profile_absolute=True)

    assert await cp.set_charge_rate(limit_amps=16) is True

    profile = cp.sent[0].charging_profile
    assert (
        profile["charging_profile_kind"] == ChargingProfileKindEnumType.absolute.value
    )
    assert profile["charging_schedule"][0]["start_schedule"] == "2020-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_a_custom_profile_keeps_the_requested_connectors_evse(hass):
    """A caller-supplied profile still goes where conn_id maps.

    The escape hatch is unchanged: a TxDefaultProfile may legitimately
    target a positive EVSE, so it is sent to the mapped EVSE, here 7.
    """
    cp = _mk_cp(hass)
    cp._global_to_evse = {2: (7, 1)}
    profile = {
        "id": 5,
        "stack_level": 1,
        "charging_profile_purpose": (
            ChargingProfilePurposeEnumType.tx_default_profile.value
        ),
        "charging_profile_kind": "Relative",
        "charging_schedule": [
            {
                "id": 5,
                "charging_rate_unit": "A",
                "charging_schedule_period": [{"start_period": 0, "limit": 10}],
            }
        ],
    }

    assert await cp.set_charge_rate(conn_id=2, profile=profile) is True

    assert _sent(cp) == ["SetChargingProfile"]
    assert cp.sent[0].evse_id == 7
    assert cp.sent[0].charging_profile is profile


@pytest.mark.asyncio
async def test_watt_only_station_receives_converted_amp_limit(hass):
    """RateUnit=W turns a managed amp request into the equivalent power."""
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(charging_rate_units=frozenset({"W"}))

    assert await cp.set_charge_rate(limit_amps=16) is True

    period = cp.sent[0].charging_profile["charging_schedule"][0][
        "charging_schedule_period"
    ][0]
    assert (
        cp.sent[0].charging_profile["charging_schedule"][0]["charging_rate_unit"] == "W"
    )
    assert period["limit"] == 3680


@pytest.mark.asyncio
async def test_amp_only_station_receives_converted_watt_limit(hass):
    """RateUnit=A turns a managed watt request into the equivalent current."""
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(charging_rate_units=frozenset({"A"}))

    assert await cp.set_charge_rate(limit_watts=3680) is True

    schedule = cp.sent[0].charging_profile["charging_schedule"][0]
    assert schedule["charging_rate_unit"] == "A"
    assert schedule["charging_schedule_period"][0]["limit"] == 16.0


@pytest.mark.asyncio
async def test_rate_unit_report_reads_the_supported_units_not_the_domain(hass):
    """A W-only station still lists the whole domain in valuesList."""
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._wait_inventory = asyncio.Event()

    def report(value, values_list):
        attribute = {"type": "Actual"}
        if value is not None:
            attribute["value"] = value
        characteristics = {"data_type": "MemberList"}
        if values_list is not None:
            characteristics["values_list"] = values_list
        cp.on_report(
            1,
            "2026-01-01T00:00:00Z",
            0,
            tbc=True,
            report_data=[
                {
                    "component": {"name": "SmartChargingCtrlr"},
                    "variable": {"name": "RateUnit"},
                    "variable_attribute": [attribute],
                    "variable_characteristics": characteristics,
                }
            ],
        )

    report("W", "A,W")
    assert cp._inventory.charging_rate_units == frozenset({"W"})
    report(None, "A,W")
    assert cp._inventory.charging_rate_units == frozenset({"A", "W"})
    report("A; W", None)
    assert cp._inventory.charging_rate_units == frozenset({"A", "W"})


@pytest.mark.asyncio
async def test_managed_set_refreshes_the_report_after_a_boot(hass):
    """A boot drops the cached report; the next set must read it again."""
    cp = _mk_cp(hass)
    cp._inventory = None

    async def refresh():
        cp._inventory = InventoryReport(charging_rate_units=frozenset({"W"}))

    cp._get_inventory = refresh
    assert await cp.set_charge_rate(limit_amps=16) is True
    schedule = cp.sent[0].charging_profile["charging_schedule"][0]
    assert schedule["charging_rate_unit"] == "W"
    assert schedule["charging_schedule_period"][0]["limit"] == 3680

    # A refresh that fails keeps the request going with today's amp default.
    cp._inventory = None

    async def failing_refresh():
        raise TimeoutError

    cp._get_inventory = failing_refresh
    assert await cp.set_charge_rate(limit_amps=16) is True
    schedule = cp.sent[-1].charging_profile["charging_schedule"][0]
    assert schedule["charging_rate_unit"] == "A"


@pytest.mark.asyncio
async def test_managed_set_waits_for_an_inventory_attempt_already_in_flight(hass):
    """An overlapping report settles before the rate unit is selected."""
    cp = _mk_cp(hass)
    cp._inventory = None
    sent = []

    async def record(req):
        sent.append(req)
        if type(req).__name__ == "GetBaseReport":
            return SimpleNamespace(status="Accepted")
        return SimpleNamespace(
            status=ChargingProfileStatusEnumType.accepted,
            status_info=None,
        )

    cp.call = record
    inventory_owner = asyncio.create_task(cp._get_inventory())
    await asyncio.sleep(0)
    assert cp._wait_inventory is not None

    managed_set = asyncio.create_task(cp.set_charge_rate(limit_amps=16))
    await asyncio.sleep(0)
    assert [type(req).__name__ for req in sent] == ["GetBaseReport"]

    cp.on_report(
        1,
        "2026-01-01T00:00:00Z",
        0,
        report_data=[
            {
                "component": {"name": "SmartChargingCtrlr"},
                "variable": {"name": "RateUnit"},
                "variable_attribute": [{"value": "W"}],
            }
        ],
    )
    await asyncio.gather(inventory_owner, managed_set)

    set_request = next(
        req for req in sent if type(req).__name__ == "SetChargingProfile"
    )
    schedule = set_request.charging_profile["charging_schedule"][0]
    assert schedule["charging_rate_unit"] == "W"
    assert schedule["charging_schedule_period"][0]["limit"] == 3680


@pytest.mark.asyncio
async def test_inventory_owner_wakes_waiters_when_no_report_will_arrive(hass):
    """A refused inventory request cannot strand callers sharing its attempt."""
    cp = _mk_cp(hass)
    cp._inventory = None
    owner_started = asyncio.Event()
    release_owner = asyncio.Event()

    async def refuse(_req):
        owner_started.set()
        await release_owner.wait()
        return SimpleNamespace(status="Rejected")

    cp.call = refuse
    owner = asyncio.create_task(cp._get_inventory())
    await owner_started.wait()
    waiter = asyncio.create_task(cp._get_inventory())
    await asyncio.sleep(0)
    assert not waiter.done()

    release_owner.set()
    await owner
    await asyncio.sleep(0)
    assert waiter.done()
    await waiter
    assert cp._wait_inventory is None
