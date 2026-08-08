"""The one-connector floor for OCPP 2.0.1 chargers with unusable inventories.

The websocket-level scenarios in test_charge_point_v201.py cover the FoxESS
shape (inventory with no connector components), refused and unsupported
GetBaseReport, and a first status arriving only after setup. These unit tests
cover the one shape a test charge point cannot conveniently fake: a charger
that accepts GetBaseReport and then never finishes reporting.
"""

import asyncio
from types import SimpleNamespace

import pytest
from ocpp.exceptions import OCPPError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import HAChargerStatuses as cstat
from custom_components.ocpp.ocppv201 import ChargePoint

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


def _mk_cp(hass):
    """Build a v201 ChargePoint detached from any real connection."""
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
        max_current=32.0,
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
    cp._response_timeout = 0.05
    return cp


@pytest.mark.asyncio
async def test_silent_charger_still_gets_the_floor(hass):
    """Accepting GetBaseReport and never reporting must not abort setup.

    The report-wait TimeoutError previously escaped get_number_of_connectors
    and post_connect swallowed it, so the connector slots were never created:
    no units, a spurious units_changed repair, and statuses buffered forever.
    """
    cp = _mk_cp(hass)

    async def accept_and_never_report(req):
        return SimpleNamespace(status="Accepted")

    cp.call = accept_and_never_report

    # buffered during the (silent) inventory exchange
    cp._pending_status_notifications = [("2026-01-01T00:00:00Z", "Available", 1, 1)]

    total = await cp.get_number_of_connectors()  # must not raise

    assert total == 1
    assert cp._pending_status_notifications == []
    assert cp._metrics[(1, cstat.status_connector.value)].value == "Available"


@pytest.mark.asyncio
async def test_refused_inventory_still_gets_the_floor(hass):
    """A refused GetBaseReport lands in the same place: one logical connector."""
    cp = _mk_cp(hass)

    async def refuse(req):
        raise OCPPError("refused")

    cp.call = refuse

    total = await cp.get_number_of_connectors()

    assert total == 1


@pytest.mark.asyncio
async def test_second_post_connect_during_slow_inventory_does_not_poison_map(hass):
    """A concurrent setup pass must not flush statuses mid-inventory.

    post_connect can run twice (boot notification + the 10s monitor
    backstop). With a slow multipart inventory, the first NotifyReport part
    makes _inventory truthy before any connector counts arrive, so the second
    pass skips the wait, sees zero connectors, and - if it flushed - would
    install a dynamic map from buffered statuses. Arrived in the order
    (2,1), (1,1), that map is reversed, and _build_connector_map keeps an
    already-populated map when the real inventory lands: a working
    two-connector charger's sensors stay swapped until the integration is
    reloaded.
    """
    cp = _mk_cp(hass)

    # owner attempt in flight; first report part arrived, counts still absent
    cp._wait_inventory = asyncio.Event()
    from custom_components.ocpp.ocppv201 import InventoryReport

    cp._inventory = InventoryReport()
    cp._pending_status_notifications = [
        ("2026-01-01T00:00:00Z", "Faulted", 2, 1),
        ("2026-01-01T00:00:01Z", "Available", 1, 1),
    ]

    total = await cp.get_number_of_connectors()  # the second, concurrent pass

    assert total == 1  # the floor still applies to the returned count
    assert (
        cp._evse_to_global == {}
    ), "no map may be installed while the inventory attempt is in flight"
    assert (
        len(cp._pending_status_notifications) == 2
    ), "statuses must stay buffered for the real map"

    # the real inventory finishes: two EVSEs, one connector each
    cp._inventory.evse_count = 2
    cp._inventory.connector_count = [1, 1]
    cp._wait_inventory = None
    assert cp._build_connector_map()
    cp._flush_pending_status_notifications()

    assert cp._evse_to_global == {(1, 1): 1, (2, 1): 2}
    assert cp._metrics[(1, cstat.status_connector.value)].value == "Available"
    assert cp._metrics[(2, cstat.status_connector.value)].value == "Faulted"


@pytest.mark.asyncio
async def test_status_buffered_during_refetch_is_drained_when_it_times_out(hass):
    """A status held for an in-flight attempt must be applied when it settles.

    stop_transaction re-fetches the inventory when none is cached, so an
    attempt can be in flight after setup succeeded. A status arriving then
    buffers - it must not route dynamically past the attempt - and the
    attempt's settling must drain it: a silently timed-out refetch previously
    left the entry buffered forever, and repeated silent refetches would
    accumulate more.
    """
    cp = _mk_cp(hass)
    cp.post_connect_success = True
    started = asyncio.Event()

    async def accept_then_go_quiet(req):
        started.set()
        return SimpleNamespace(status="Accepted")

    cp.call = accept_then_go_quiet

    updates: list[str] = []

    async def record_update(cpid):
        updates.append(cpid)

    cp.update = record_update

    refetch = asyncio.create_task(cp._get_inventory())
    await started.wait()

    # connector 2 of an EVSE whose connector 1 has no known status: the
    # EVSE aggregation in _apply_status_notification skips its HA update in
    # exactly this shape, so only the drain's own notify covers it
    cp.on_status_notification(
        timestamp="2026-01-01T00:00:00Z",
        connector_status="Available",
        evse_id=1,
        connector_id=2,
    )
    assert cp._pending_status_notifications == [
        ("2026-01-01T00:00:00Z", "Available", 1, 2)
    ], "a status must not route dynamically while an attempt is in flight"
    assert cp._evse_to_global == {}

    await refetch  # the attempt times out and settles

    assert (
        cp._pending_status_notifications == []
    ), "settling the attempt must drain the buffer"
    assert cp._metrics[(1, cstat.status_connector.value)].value == "Available"
    assert cp._evse_to_global == {(1, 2): 1}
    await asyncio.sleep(0)  # let the scheduled update task run
    assert updates, (
        "the drain must notify HA itself - _report_evse_status skips while "
        "any connector in the EVSE has no known status"
    )

    # and a later status now routes directly
    cp.on_status_notification(
        timestamp="2026-01-01T00:00:02Z",
        connector_status="Occupied",
        evse_id=1,
        connector_id=2,
    )
    assert cp._pending_status_notifications == []
    # The per-connector metric speaks the OCPP 1.6 vocabulary, as the
    # charging-station-level one already did: Occupied maps to Preparing.
    assert cp._metrics[(1, cstat.status_connector.value)].value == "Preparing"


@pytest.mark.asyncio
async def test_partial_topology_timeout_still_drains_buffered_statuses(hass):
    """A timed-out report that yielded SOME connectors must still drain.

    A first NotifyReport part can supply real EVSE/connector counts before the
    charger goes quiet. The floor is then bypassed (total > 0), so the drain
    cannot live only in the zero-connector fallback: the boot status buffered
    during the exchange previously stayed unapplied forever even though setup
    succeeded.
    """
    cp = _mk_cp(hass)
    started = asyncio.Event()

    async def accept_then_go_quiet(req):
        started.set()
        return SimpleNamespace(status="Accepted")

    cp.call = accept_then_go_quiet

    setup = asyncio.create_task(cp.get_number_of_connectors())
    await started.wait()

    # the charger's boot status lands first, before any report part exists to
    # build a map from - so it buffers
    cp.on_status_notification(
        timestamp="2026-01-01T00:00:01Z",
        connector_status="Available",
        evse_id=1,
        connector_id=1,
    )
    assert cp._pending_status_notifications, "status buffers during the exchange"

    # then the first report part arrives, describing one EVSE with one
    # connector - and the final part never comes
    cp.on_report(
        1,
        "2026-01-01T00:00:00Z",
        0,
        report_data=[
            {
                "component": {"name": "EVSE", "evse": {"id": 1}},
                "variable": {"name": "AvailabilityState"},
            },
            {
                "component": {
                    "name": "Connector",
                    "evse": {"id": 1, "connector_id": 1},
                },
                "variable": {"name": "AvailabilityState"},
            },
            {
                "component": {
                    "name": "Connector",
                    "evse": {"id": 1, "connector_id": 2},
                },
                "variable": {"name": "AvailabilityState"},
            },
        ],
        tbc=True,
    )

    total = await setup  # report wait times out; partial inventory kept

    # two connectors proves the partial topology was RETAINED - the
    # one-connector floor would have produced 1
    assert total == 2, "the partial inventory bypasses the floor"
    assert (
        cp._pending_status_notifications == []
    ), "the drain must not live only in the zero-connector fallback"
    assert cp._metrics[(1, cstat.status_connector.value)].value == "Available"
    assert cp._evse_to_global == {(1, 1): 1, (1, 2): 2}


@pytest.mark.asyncio
async def test_concurrent_get_inventory_has_a_single_owner(hass):
    """A second concurrent _get_inventory must not take over the attempt.

    Both callers used to pass the cached-inventory guard before any report
    part arrived, so the second overwrote the owner's event - and once the
    owner settled and cleared it, the second caller's accepted response
    dereferenced None at _wait_inventory.wait(). Only one GetBaseReport may
    go out, and the second caller must return without touching the attempt.
    """
    cp = _mk_cp(hass)
    calls: list[int] = []

    async def slow_accept(req):
        calls.append(len(calls) + 1)
        await asyncio.sleep(0.02)
        return SimpleNamespace(status="Accepted")

    cp.call = slow_accept

    async def second_caller():
        await asyncio.sleep(0.005)  # enter while the first call is pending
        await cp._get_inventory()

    await asyncio.gather(cp._get_inventory(), second_caller())

    assert len(calls) == 1, "only the owning attempt may send GetBaseReport"
    assert cp._wait_inventory is None


@pytest.mark.asyncio
async def test_failed_attempt_releases_ownership_for_the_next_one(hass):
    """An escaping request failure must not leave the attempt gate locked.

    The ocpp library's response timeout raises a bare TimeoutError that the
    inventory handlers don't cover. With an in-flight event now turning other
    callers away, leaking it set would make every future attempt - including
    post_connect re-running after the charger's next boot - silently return
    forever.
    """
    cp = _mk_cp(hass)
    calls: list[int] = []

    async def timeout_then_refuse(req):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise TimeoutError("lib response timeout")
        raise OCPPError("refused")

    cp.call = timeout_then_refuse

    # buffered while the failing attempt is in flight
    cp._pending_status_notifications = [("2026-01-01T00:00:00Z", "Available", 1, 1)]

    with pytest.raises(TimeoutError):
        await cp.get_number_of_connectors()

    assert (
        cp._wait_inventory is None
    ), "a failed attempt must release ownership on its way out"
    assert cp._pending_status_notifications == [], (
        "even a failed attempt must drain on its way out - on a persistently "
        "failing charger the next attempt would strand these again"
    )
    assert cp._metrics[(1, cstat.status_connector.value)].value == "Available"

    total = await cp.get_number_of_connectors()

    assert len(calls) == 2, "the next attempt must be able to run"
    assert total == 1
