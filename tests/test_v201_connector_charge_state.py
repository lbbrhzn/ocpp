"""The per-connector status metric must speak the OCPP 1.6 vocabulary.

switch.charge_control is per_connector and matches on Charging /
SuspendedEVSE / SuspendedEV. Those come from TransactionEvent's chargingState
in OCPP 2.0.1; a ConnectorStatusEnumType can never be any of them, so while
the per-connector metric carried raw 2.0.1 statuses the switch read off for
the whole of every charge - and any automation gating on it was silently
dead.

The tests assert against switch.py's own condition lists rather than
hard-coded strings, so they fail if either side of that contract moves.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ocpp.v16.enums import ChargePointStatus as ChargePointStatusv16
from ocpp.v201.enums import ChargingStateEnumType, ConnectorStatusEnumType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import (
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
)
from custom_components.ocpp.ocppv201 import ChargePoint, InventoryReport
from custom_components.ocpp.switch import SWITCHES

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH


def _switch(key: str):
    """Look up a switch description, failing legibly rather than at collection."""
    found = next((s for s in SWITCHES if s.key == key), None)
    assert found is not None, f"switch.py no longer defines a '{key}' switch"
    return found


CHARGE_CONTROL = _switch("charge_control")
# NB: three n's - the upstream key is spelled "connnector_availability".
CONNECTOR_AVAILABILITY = _switch("connnector_availability")


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
    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])
    cp._build_connector_map()
    return cp


def _connector_status(cp, global_idx: int = 1):
    return cp._metrics[(global_idx, cstat.status_connector.value)].value


def _station_status(cp):
    return cp._metrics[(0, cstat.status_connector.value)].value


def _tx_event(cp, charging_state: ChargingStateEnumType, connector_id: int = 1):
    """Drive a transaction event. charging_state is sent as the wire string."""
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:00:00Z",
        "ChargingStateChanged",
        1,
        {"transaction_id": "tx-1", "charging_state": charging_state.value},
        evse={"id": 1, "connector_id": connector_id},
    )


def _start_transaction(cp, global_idx: int = 1):
    """Mark a transaction live, as on_transaction_event does on Started."""
    cp._tx_start_time[global_idx] = datetime.now(tz=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("charging_state", "expected"),
    [
        (ChargingStateEnumType.charging, ChargePointStatusv16.charging),
        (ChargingStateEnumType.suspended_ev, ChargePointStatusv16.suspended_ev),
        (ChargingStateEnumType.suspended_evse, ChargePointStatusv16.suspended_evse),
    ],
)
async def test_charging_state_reaches_the_connector(hass, charging_state, expected):
    """Charge Control can only read on if these reach the per-connector metric."""
    cp = _mk_cp(hass)

    _tx_event(cp, charging_state)

    assert _connector_status(cp) == expected.value
    assert _connector_status(cp) in CHARGE_CONTROL.metric_condition


@pytest.mark.asyncio
async def test_status_notification_is_translated_not_raw(hass):
    """A raw 2.0.1 status must never reach the metric.

    Occupied satisfies no condition in switch.py, so leaving it raw is what
    made both per-connector switches unreadable on 2.0.1.
    """
    cp = _mk_cp(hass)

    cp._apply_status_notification(
        "2026-01-01T00:00:00Z", ConnectorStatusEnumType.occupied.value, 1, 1
    )

    assert _connector_status(cp) == ChargePointStatusv16.preparing.value
    assert _connector_status(cp) != ConnectorStatusEnumType.occupied.value
    # Not charging, so Charge Control stays off - but the connector is
    # operative, which Connector Availability must now be able to see.
    assert _connector_status(cp) not in CHARGE_CONTROL.metric_condition
    assert _connector_status(cp) in CONNECTOR_AVAILABILITY.metric_condition


@pytest.mark.asyncio
async def test_charging_survives_a_reconnect_status_notification(hass):
    """A status notification mid-charge must not turn Charge Control off.

    trigger_status_notification() re-requests statuses on every reconnect, so
    a charger legitimately resends Occupied part-way through a transaction.
    Occupied is less specific than Charging, and the periodic transaction
    events that follow only carry chargingState when it changes - so letting
    it through would leave the switch off for the rest of the session.
    """
    cp = _mk_cp(hass)
    _start_transaction(cp)
    _tx_event(cp, ChargingStateEnumType.charging)
    assert _connector_status(cp) in CHARGE_CONTROL.metric_condition

    cp._apply_status_notification(
        "2026-01-01T00:00:01Z", ConnectorStatusEnumType.occupied.value, 1, 1
    )

    assert _connector_status(cp) == ChargePointStatusv16.charging.value
    assert _connector_status(cp) in CHARGE_CONTROL.metric_condition
    # the charging-station-level metric is held for the same reason
    assert _station_status(cp) == ChargePointStatusv16.charging.value


@pytest.mark.asyncio
async def test_the_hold_lifts_once_the_transaction_ends(hass):
    """Holding a charging state must not outlive its transaction."""
    cp = _mk_cp(hass)
    _start_transaction(cp)
    _tx_event(cp, ChargingStateEnumType.charging)

    cp._tx_start_time.pop(1, None)  # as on_transaction_event does on Ended
    cp._apply_status_notification(
        "2026-01-01T00:00:02Z", ConnectorStatusEnumType.occupied.value, 1, 1
    )

    assert _connector_status(cp) == ChargePointStatusv16.preparing.value
    assert _connector_status(cp) not in CHARGE_CONTROL.metric_condition


@pytest.mark.asyncio
async def test_occupied_still_applies_when_no_charge_is_running(hass):
    """The hold must only cover a genuine downgrade, never an update."""
    cp = _mk_cp(hass)
    _start_transaction(cp)  # live transaction, but no charging state yet

    cp._apply_status_notification(
        "2026-01-01T00:00:00Z", ConnectorStatusEnumType.occupied.value, 1, 1
    )

    assert _connector_status(cp) == ChargePointStatusv16.preparing.value


@pytest.mark.asyncio
async def test_idle_does_not_free_an_occupied_connector(hass):
    """Idle in chargingState means no session, not an empty connector.

    Propagating its Available would contradict the occupancy that
    StatusNotification owns, while _connector_status still holds Occupied.
    """
    cp = _mk_cp(hass)
    cp._apply_status_notification(
        "2026-01-01T00:00:00Z", ConnectorStatusEnumType.occupied.value, 1, 1
    )

    _tx_event(cp, ChargingStateEnumType.idle)

    assert _connector_status(cp) == ChargePointStatusv16.preparing.value


@pytest.mark.asyncio
async def test_a_transaction_event_for_connector_zero_is_not_mapped(hass):
    """The real entry point must refuse the pair, not just the metric write.

    on_transaction_event reaches _pair_to_global and _set_meter_values well
    before it reports a status, so guarding only the status write still let a
    malformed pair allocate a phantom connector, record the transaction and
    its meter values against it, and strand the real connector.
    """
    cp = _mk_cp(hass)
    before = dict(cp._evse_to_global)

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "RemoteStart",
        0,
        {
            "transaction_id": "tx-bad",
            "charging_state": ChargingStateEnumType.charging.value,
        },
        evse={"id": 1, "connector_id": 0},
    )

    assert dict(cp._evse_to_global) == before
    assert cp._tx_start_time == {}
    assert cp._metrics[(1, csess.transaction_id.value)].value is None
    assert _connector_status(cp) is None
    # the charging state is still station-level news
    assert _station_status(cp) == ChargePointStatusv16.charging.value


@pytest.mark.asyncio
async def test_a_degenerate_connector_id_is_not_written(hass):
    """A transaction event for connector 0 must not strand the real one.

    _apply_status_notification drops these by design; _pair_to_global would
    otherwise allocate a phantom global index, write the charging state to a
    connector that does not exist and leave the real one empty.
    """
    cp = _mk_cp(hass)
    before = dict(cp._evse_to_global)

    cp._report_evse_status(1, ChargePointStatusv16.charging, connector_id=0)

    assert dict(cp._evse_to_global) == before
    assert _connector_status(cp) is None
    # the charging-station-level metric is still updated
    assert _station_status(cp) == ChargePointStatusv16.charging.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (ConnectorStatusEnumType.available, ChargePointStatusv16.available),
        (ConnectorStatusEnumType.faulted, ChargePointStatusv16.faulted),
        (ConnectorStatusEnumType.unavailable, ChargePointStatusv16.unavailable),
        (ConnectorStatusEnumType.reserved, ChargePointStatusv16.reserved),
        (ConnectorStatusEnumType.occupied, ChargePointStatusv16.preparing),
    ],
)
async def test_every_connector_status_maps_into_the_1_6_vocabulary(hass, raw, expected):
    """No 2.0.1 connector status may leak through untranslated."""
    cp = _mk_cp(hass)

    cp._apply_status_notification("2026-01-01T00:00:00Z", raw.value, 1, 1)

    assert _connector_status(cp) == expected.value
    assert _connector_status(cp) in [s.value for s in ChargePointStatusv16]
    # the EVSE aggregate uses the same mapping - Reserved previously fell
    # through to Preparing here
    assert _station_status(cp) == expected.value


def _mk_two_evse_cp(hass):
    """Build a charger with two single-connector EVSEs."""
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(evse_count=2, connector_count=[1, 1])
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._build_connector_map()
    return cp


@pytest.mark.asyncio
async def test_another_evse_does_not_clear_the_station_charging_state(hass):
    """The station has one status metric shared by every EVSE.

    A second EVSE plugging in, or reporting Idle, must not report the whole
    charging station as merely occupied - or worse, free - while another EVSE
    is still delivering.
    """
    cp = _mk_two_evse_cp(hass)
    _start_transaction(cp, 1)
    _tx_event(cp, ChargingStateEnumType.charging, connector_id=1)
    assert _station_status(cp) == ChargePointStatusv16.charging.value

    cp._apply_status_notification(
        "2026-01-01T00:00:01Z", ConnectorStatusEnumType.occupied.value, 2, 1
    )
    assert _station_status(cp) == ChargePointStatusv16.charging.value

    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:00:02Z",
        "ChargingStateChanged",
        1,
        {
            "transaction_id": "tx-2",
            "charging_state": ChargingStateEnumType.idle.value,
        },
        evse={"id": 2, "connector_id": 1},
    )

    assert _station_status(cp) == ChargePointStatusv16.charging.value
    assert _connector_status(cp, 1) == ChargePointStatusv16.charging.value


@pytest.mark.asyncio
async def test_the_connector_does_not_stay_charging_after_the_session_ends(hass):
    """A cable left in does not change the connector status.

    So the charger need not send another StatusNotification once the session
    ends, and the connector would otherwise keep reporting Charging for as
    long as the cable stayed in.
    """
    cp = _mk_cp(hass)
    cp._apply_status_notification(
        "2026-01-01T00:00:00Z", ConnectorStatusEnumType.occupied.value, 1, 1
    )
    _start_transaction(cp)
    _tx_event(cp, ChargingStateEnumType.charging)
    assert _connector_status(cp) in CHARGE_CONTROL.metric_condition

    cp._tx_start_time.pop(1, None)
    _tx_event(cp, ChargingStateEnumType.idle)

    assert _connector_status(cp) == ChargePointStatusv16.preparing.value
    assert _connector_status(cp) not in CHARGE_CONTROL.metric_condition


@pytest.mark.asyncio
async def test_a_faulted_evse_is_not_masked_by_another_charging(hass):
    """A fault must stay visible at station level.

    _apply_status_notification records that this metric must not mask a
    faulted connector; ranking a charge above a fault would reintroduce that
    masking as soon as a second EVSE is present.
    """
    cp = _mk_two_evse_cp(hass)
    _start_transaction(cp, 1)
    _tx_event(cp, ChargingStateEnumType.charging, connector_id=1)
    assert _station_status(cp) == ChargePointStatusv16.charging.value

    cp._apply_status_notification(
        "2026-01-01T00:00:01Z", ConnectorStatusEnumType.faulted.value, 2, 1
    )

    assert _station_status(cp) == ChargePointStatusv16.faulted.value
    # the charging EVSE's own connector is unaffected
    assert _connector_status(cp, 1) == ChargePointStatusv16.charging.value


@pytest.mark.asyncio
async def test_the_live_transaction_check_does_not_allocate(hass):
    """A read-only predicate must not create connector mappings."""
    cp = _mk_cp(hass)
    before = dict(cp._evse_to_global)

    assert cp._has_live_transaction(9, 9) is False

    assert dict(cp._evse_to_global) == before


@pytest.mark.asyncio
async def test_helpers_are_safe_before_any_status_is_known(hass):
    """The status helpers must cope with an EVSE they have heard nothing about.

    _report_evse_status can run before any StatusNotification has arrived -
    a TransactionEvent may be the first message about a connector - so each
    lookup has to answer "unknown" rather than raise.
    """
    cp = _mk_cp(hass)
    cp._connector_status = []
    cp._evse_status_v16 = {}

    # no connector statuses recorded yet
    assert cp._aggregate_evse_status(1) is None
    assert cp._known_occupancy(1, 1) is None
    # an EVSE beyond anything reported
    assert cp._aggregate_evse_status(99) is None
    assert cp._known_occupancy(99, 99) is None
    # nothing to derive the station value from
    assert cp._derive_station_status() is None
    # a charging state outside the ones we translate
    assert cp._charging_state_v16("SomethingElse") is None


@pytest.mark.asyncio
async def test_station_falls_back_when_nothing_is_derivable(hass):
    """With no per-EVSE state yet, the reported status is used as-is."""
    cp = _mk_cp(hass)
    cp._evse_status_v16 = {}

    cp._report_evse_status(0, ChargePointStatusv16.available)

    assert _station_status(cp) == ChargePointStatusv16.available.value
