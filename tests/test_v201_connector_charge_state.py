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
from unittest.mock import AsyncMock, Mock

import pytest
from ocpp.v16.enums import ChargePointStatus as ChargePointStatusv16
from ocpp.v201.enums import (
    AuthorizationStatusEnumType,
    ChargingStateEnumType,
    ConnectorStatusEnumType,
    MeasurandEnumType,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp import ocppv201 as ocppv201_module
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
    return cp._metrics[(global_idx, cstat.status_connector)].value


def _station_status(cp):
    return cp._metrics[(0, cstat.status_connector)].value


def _tx_event(
    cp,
    charging_state: ChargingStateEnumType,
    connector_id: int = 1,
    seq_no: int = 1,
):
    """Drive a transaction event. charging_state is sent as the wire string."""
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:00:00Z",
        "ChargingStateChanged",
        seq_no,
        {"transaction_id": "tx-1", "charging_state": charging_state.value},
        evse={"id": 1, "connector_id": connector_id},
    )


def _start_transaction(cp, global_idx: int = 1):
    """Mark a transaction live, as on_transaction_event does on Started."""
    cp._tx_start_time[global_idx] = datetime.now(tz=UTC)


@pytest.mark.asyncio
async def test_offline_and_ambiguous_starts_do_not_create_live_sessions(hass):
    """An offline Started records ordering only; the next online event adopts."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "offline"},
        evse={"id": 1, "connector_id": 1},
        offline=True,
    )
    cp.session_controller.on_transaction_start.assert_not_called()
    assert cp._metrics[(1, csess.transaction_id)].value is None
    assert 1 not in cp._tx_start_time
    assert cp._tx_event_state["offline"] == {"seq": 0, "ended": False}

    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:00:30Z",
        "MeterValuePeriodic",
        1,
        {"transaction_id": "offline"},
        evse={"id": 1, "connector_id": 1},
    )
    cp.session_controller.on_transaction_start.assert_not_called()
    assert cp._metrics[(1, csess.transaction_id)].value == "offline"

    cp.on_transaction_event(
        "Ended",
        "2026-01-01T00:01:00Z",
        "EVCommunicationLost",
        2,
        {"transaction_id": "offline"},
        evse={"id": 1, "connector_id": 1},
        offline=True,
    )
    cp.session_controller.on_transaction_end.assert_called_once_with(1, "offline")
    assert cp._metrics[(1, csess.transaction_id)].value == ""

    cp._inventory = InventoryReport(evse_count=1, connector_count=[2])
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._build_connector_map()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:01Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "ambiguous"},
        evse={"id": 1},
    )
    cp.session_controller.on_transaction_start.assert_not_called()


@pytest.mark.asyncio
async def test_offline_start_for_displayed_transaction_is_ordering_only(hass):
    """A replayed start cannot rewind live state for the same transaction id."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T01:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "same"},
        **evse,
    )
    online_start = cp._tx_start_time[1]

    cp._reset_protocol_generation_state()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "same"},
        offline=True,
        **evse,
    )

    assert cp._tx_event_state["same"] == {"seq": 0, "ended": False}
    assert cp._tx_start_time[1] == online_start
    assert cp._metrics[(1, csess.transaction_id)].value == "same"


@pytest.mark.asyncio
async def test_reconnect_keeps_seq_guard_against_an_older_offline_update(hass):
    """A websocket drop cannot let replayed meter state move backwards."""
    cp = _mk_cp(hass)
    energy = MeasurandEnumType.energy_active_import_register.value
    evse = {"evse": {"id": 1, "connector_id": 1}}

    def meter_value(value: int, timestamp: str) -> list[dict]:
        return [
            {
                "timestamp": timestamp,
                "sampled_value": [
                    {
                        "value": value,
                        "measurand": energy,
                        "unit_of_measure": {"unit": "Wh"},
                    }
                ],
            }
        ]

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "same"},
        meter_value=meter_value(1000, "2026-01-01T00:00:00Z"),
        **evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:10:00Z",
        "MeterValuePeriodic",
        5,
        {"transaction_id": "same"},
        meter_value=meter_value(2000, "2026-01-01T00:10:00Z"),
        **evse,
    )
    assert cp._metrics[(1, energy)].value == 2.0
    assert cp._metrics[(1, csess.session_time)].value == 10

    cp._reset_protocol_generation_state()
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:05:00Z",
        "MeterValuePeriodic",
        3,
        {"transaction_id": "same"},
        offline=True,
        meter_value=meter_value(1500, "2026-01-01T00:05:00Z"),
        **evse,
    )

    assert cp._tx_event_state["same"] == {"seq": 5, "ended": False}
    assert cp._metrics[(1, energy)].value == 2.0
    assert cp._metrics[(1, csess.session_time)].value == 10


@pytest.mark.asyncio
async def test_missing_connector_before_inventory_uses_single_connector_config(hass):
    """An early unambiguous Started event is replayed once inventory settles."""
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.session_controller = Mock()

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "early"},
        evse={"id": 1},
    )

    assert len(cp._pending_transaction_events) == 1
    cp.session_controller.on_transaction_start.assert_not_called()

    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])
    cp._flush_pending_transaction_events()

    cp.session_controller.on_transaction_start.assert_called_once_with(
        1, "early", (1, 1)
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "early"


@pytest.mark.asyncio
async def test_sequence_and_ended_guard_prevent_transaction_resurrection(hass):
    """Older events and all events following Ended are history, not liveness."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    common = (
        "2026-01-01T00:00:00Z",
        "ChargingStateChanged",
    )
    tx = {"transaction_id": "tx-ordered"}
    cp.on_transaction_event(
        "Started", *common, 2, tx, evse={"id": 1, "connector_id": 1}
    )
    cp.on_transaction_event("Ended", *common, 1, tx, evse={"id": 1, "connector_id": 1})
    cp.session_controller.on_transaction_end.assert_not_called()
    cp.on_transaction_event("Ended", *common, 3, tx, evse={"id": 1, "connector_id": 1})
    cp.session_controller.on_transaction_end.assert_called_once_with(1, "tx-ordered")
    cp.on_transaction_event(
        "Started", *common, 4, tx, evse={"id": 1, "connector_id": 1}
    )
    cp.session_controller.on_transaction_start.assert_called_once()


@pytest.mark.asyncio
async def test_full_ordering_guard_evicts_oldest_record_for_new_transaction(hass):
    """A broken history cannot permanently block all later transactions."""
    cp = _mk_cp(hass)
    cp._tx_event_state = {
        f"old-{index}": {"seq": 1, "ended": False} for index in range(1024)
    }

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "new"},
        evse={"id": 1, "connector_id": 1},
    )

    assert "new" in cp._tx_event_state
    assert "old-0" not in cp._tx_event_state
    assert len(cp._tx_event_state) == 1024


@pytest.mark.asyncio
async def test_delayed_offline_end_does_not_clear_newer_transaction(hass):
    """Buffered history for A must not overwrite live connector state for B."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    common = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-a"},
        **common,
    )
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:01:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-b"},
        **common,
    )
    start_b = cp._tx_start_time[1]
    energy = MeasurandEnumType.energy_active_import_register.value
    cp._metrics[(1, energy)].value = 9.0
    cp.session_controller.reset_mock()

    cp.on_transaction_event(
        "Ended",
        "2026-01-01T00:02:00Z",
        "EVCommunicationLost",
        1,
        {"transaction_id": "tx-a", "charging_state": "Idle"},
        offline=True,
        meter_value=[
            {
                "timestamp": "2026-01-01T00:02:00Z",
                "sampled_value": [
                    {
                        "value": 100,
                        "measurand": energy,
                        "unit_of_measure": {"unit": "Wh"},
                    }
                ],
            }
        ],
        **common,
    )

    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    assert cp._tx_start_time[1] == start_b
    assert cp._metrics[(1, energy)].value == 9.0
    cp.session_controller.on_transaction_end.assert_not_called()


@pytest.mark.asyncio
async def test_early_multi_connector_event_does_not_poison_evse_map(hass):
    """The first event seen must not get whichever global index is free."""
    cp = _mk_cp(hass)
    cp.settings.num_connectors = 2
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.session_controller = Mock()

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "early-evse-2"},
        evse={"id": 2, "connector_id": 1},
    )

    assert cp._evse_to_global == {}
    assert len(cp._pending_transaction_events) == 1
    cp.session_controller.on_transaction_start.assert_not_called()

    cp._inventory = InventoryReport(evse_count=2, connector_count=[1, 1])
    cp._flush_pending_transaction_events()
    assert cp._evse_to_global == {(1, 1): 1, (2, 1): 2}
    cp.session_controller.on_transaction_start.assert_called_once_with(
        2, "early-evse-2", (2, 1)
    )


@pytest.mark.asyncio
async def test_partial_inventory_cannot_route_event_before_final_report(hass):
    """A usable-looking report fragment must not install a partial map."""
    cp = _mk_cp(hass)
    cp.settings.num_connectors = 2
    cp._inventory = InventoryReport(evse_count=2, connector_count=[0, 1])
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._wait_inventory = asyncio.Event()
    cp.session_controller = Mock()

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "partial-map"},
        evse={"id": 2, "connector_id": 1},
    )

    assert cp._evse_to_global == {}
    assert len(cp._pending_transaction_events) == 1
    cp.session_controller.on_transaction_start.assert_not_called()

    cp._inventory.connector_count = [1, 1]
    cp._wait_inventory = None
    cp._inventory_mapping_pending = False
    assert cp._build_connector_map()
    cp._drain_pending_transaction_events()

    assert cp._evse_to_global == {(1, 1): 1, (2, 1): 2}
    cp.session_controller.on_transaction_start.assert_called_once_with(
        2, "partial-map", (2, 1)
    )


@pytest.mark.asyncio
async def test_buffered_event_falls_back_after_unusable_inventory(hass):
    """A failed inventory attempt must not strand transaction state forever."""
    cp = _mk_cp(hass)
    cp.settings.num_connectors = 2
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.session_controller = Mock()

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "fallback"},
        evse={"id": 2, "connector_id": 1},
    )
    cp._inventory = InventoryReport()
    cp._inventory_mapping_pending = False
    cp._drain_pending_transaction_events()

    assert cp._evse_to_global == {(2, 1): 1}
    assert cp._metrics[(1, csess.transaction_id)].value == "fallback"
    cp.session_controller.on_transaction_start.assert_called_once_with(
        1, "fallback", (2, 1)
    )


@pytest.mark.asyncio
async def test_event_after_failed_inventory_is_not_stranded_before_setup_finishes(
    hass,
):
    """The end of the attempt, not all of post_connect, closes buffering."""
    cp = _mk_cp(hass)
    cp.settings.num_connectors = 2
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.session_controller = Mock()

    async def reject_inventory(_request):
        return SimpleNamespace(status="Rejected")

    cp.call = reject_inventory
    await cp._get_inventory()
    assert not cp.post_connect_success
    assert not cp._inventory_mapping_pending

    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "after-inventory"},
        evse={"id": 2, "connector_id": 1},
    )

    assert cp._pending_transaction_events == []
    assert cp._evse_to_global == {(2, 1): 1}
    cp.session_controller.on_transaction_start.assert_called_once_with(
        1, "after-inventory", (2, 1)
    )


@pytest.mark.asyncio
async def test_reconnect_during_initial_inventory_reopens_mapping_window(hass):
    """A cancelled first attempt must not poison the next connection's map."""
    cp = _mk_cp(hass)
    cp.post_connect_success = False
    cp._inventory_mapping_pending = False
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.session_controller = Mock()

    cp._reset_protocol_generation_state()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "new-connection"},
        evse={"id": 2, "connector_id": 1},
    )

    assert cp._evse_to_global == {}
    assert len(cp._pending_transaction_events) == 1
    cp.session_controller.on_transaction_start.assert_not_called()


@pytest.mark.asyncio
async def test_pending_transaction_event_buffer_is_bounded(hass, monkeypatch, caplog):
    """A charger cannot grow the pre-inventory event buffer without limit."""
    monkeypatch.setattr(ocppv201_module, "_MAX_PENDING_TRANSACTION_EVENTS", 2)
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()

    for index in range(4):
        cp.on_transaction_event(
            "Updated",
            f"2026-01-01T00:00:0{index}Z",
            "MeterValuePeriodic",
            index,
            {"transaction_id": f"tx-{index}"},
            evse={"id": 1, "connector_id": 1},
        )

    assert len(cp._pending_transaction_events) == 2
    assert [
        args[4]["transaction_id"] for args, _ in cp._pending_transaction_events
    ] == [
        "tx-2",
        "tx-3",
    ]
    assert caplog.text.count("pending TransactionEvent buffer exceeded") == 1


@pytest.mark.asyncio
async def test_online_foreign_transaction_retires_displayed_session(hass):
    """Online evidence for B supersedes A without reporting a start for B."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    energy = MeasurandEnumType.energy_active_import_register.value
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-a"},
        **evse,
    )
    cp.session_controller.reset_mock()

    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:01:00Z",
        "MeterValuePeriodic",
        1,
        {"transaction_id": "tx-b", "charging_state": "Charging"},
        meter_value=[
            {
                "timestamp": "2026-01-01T00:01:00Z",
                "sampled_value": [
                    {
                        "value": 2000,
                        "measurand": energy,
                        "unit_of_measure": {"unit": "Wh"},
                    }
                ],
            }
        ],
        **evse,
    )

    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    assert cp._metrics[(1, energy)].value == 2.0
    assert 1 not in cp._tx_start_time
    assert cp._metrics[(1, csess.session_time)].value is None
    cp.session_controller.on_transaction_end.assert_called_once_with(1, "tx-a")
    cp.session_controller.on_transaction_start.assert_not_called()

    cp.on_transaction_event(
        "Ended",
        "2026-01-01T00:02:00Z",
        "EVCommunicationLost",
        2,
        {"transaction_id": "tx-b", "charging_state": "Idle"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == ""


@pytest.mark.asyncio
async def test_held_event_with_id_token_is_still_answered_with_id_token_info(hass):
    """A request held for the inventory window keeps its required reply field."""
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._inventory_mapping_pending = True

    response = cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "Authorized",
        0,
        {"transaction_id": "held"},
        evse={"id": 1, "connector_id": 1},
        id_token={"type": "ISO14443", "id_token": "ABCD"},
    )

    assert len(cp._pending_transaction_events) == 1
    assert response.id_token_info == {"status": AuthorizationStatusEnumType.accepted}


@pytest.mark.asyncio
async def test_inventory_attempt_settles_the_window_and_replays_held_events(hass):
    """The settle point of a real attempt, not a manual call, drains the buffer."""
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.session_controller = Mock()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "held"},
        evse={"id": 1, "connector_id": 1},
    )
    assert len(cp._pending_transaction_events) == 1

    async def reject_inventory(_request):
        return SimpleNamespace(status="Rejected")

    cp.call = reject_inventory
    await cp._get_inventory()

    assert cp._pending_transaction_events == []
    assert not cp._inventory_mapping_pending
    assert cp._metrics[(1, csess.transaction_id)].value == "held"
    cp.session_controller.on_transaction_start.assert_called_once_with(
        1, "held", (1, 1)
    )


@pytest.mark.asyncio
async def test_reconnect_after_failed_setup_with_cached_report_holds_nothing(hass):
    """A cached report means no attempt is coming, so nothing may wait for one."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp._inventory = InventoryReport()  # GetBaseReport unsupported: no map
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._inventory_mapping_pending = False
    cp.post_connect_success = False  # a later post_connect step failed

    cp._reset_protocol_generation_state()  # websocket blip -> reconnect()
    assert not cp._inventory_mapping_pending
    await cp._get_inventory()  # post_connect retry finds the cached report

    cp.on_status_notification("2026-01-01T00:00:00Z", "Occupied", 1, 1)
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:01Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 1, "connector_id": 1},
    )

    assert cp._pending_status_notifications == []
    assert cp._pending_transaction_events == []
    assert cp._metrics[(1, csess.transaction_id)].value == "T"
    cp.session_controller.on_transaction_start.assert_called_once_with(1, "T", (1, 1))


@pytest.mark.asyncio
async def test_cached_report_settles_a_window_armed_after_it(hass):
    """An armed window cannot outlive a cached report at the next attempt."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp._inventory = InventoryReport()
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._inventory_mapping_pending = True
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 1, "connector_id": 1},
    )
    assert len(cp._pending_transaction_events) == 1

    await cp._get_inventory()

    assert not cp._inventory_mapping_pending
    assert cp._pending_transaction_events == []
    cp.session_controller.on_transaction_start.assert_called_once_with(1, "T", (1, 1))


@pytest.mark.asyncio
async def test_partial_report_is_not_settled_by_a_concurrent_caller(hass):
    """The owner of an in-flight attempt keeps the settle, parts or not."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.settings.num_connectors = 2
    cp._inventory = InventoryReport(evse_count=2, connector_count=[0, 1])
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._wait_inventory = asyncio.Event()
    cp._response_timeout = 0.01
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "partial"},
        evse={"id": 2, "connector_id": 1},
    )
    assert len(cp._pending_transaction_events) == 1

    await cp._get_inventory()  # the racing second post_connect

    assert cp._inventory_mapping_pending
    assert cp._evse_to_global == {}
    assert len(cp._pending_transaction_events) == 1
    cp.session_controller.on_transaction_start.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_leaves_history_to_the_attempt_still_streaming(hass):
    """A drop mid-report must not replay held events through the partial map."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.settings.num_connectors = 2
    cp.post_connect_success = False
    cp._inventory = InventoryReport()  # first parts arrived, nothing usable yet
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._wait_inventory = asyncio.Event()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 2, "connector_id": 1},
    )

    cp._reset_protocol_generation_state()  # reconnect() while it streams
    assert len(cp._pending_transaction_events) == 1
    assert cp._evse_to_global == {}

    cp._wait_inventory = None  # the attempt settles with the complete report
    cp._inventory = InventoryReport(evse_count=2, connector_count=[1, 1])
    cp._settle_inventory_boundary()

    assert cp._evse_to_global == {(1, 1): 1, (2, 1): 2}
    assert cp._tx_event_state["T"] == {"seq": 0, "ended": False}
    assert cp._metrics[(2, csess.transaction_id)].value is None
    cp.session_controller.on_transaction_start.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_keeps_held_events_as_history(hass):
    """Events held on a dropped connection keep their order but move no state."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._inventory_mapping_pending = True
    cp.post_connect_success = False
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 1, "connector_id": 1},
    )

    cp._reset_protocol_generation_state()
    assert len(cp._pending_transaction_events) == 1
    assert cp._pending_transaction_events[0][1]["offline"] is True
    assert cp._inventory_mapping_pending

    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])
    cp.update = AsyncMock()
    cp._settle_inventory_boundary()
    await hass.async_block_till_done()

    assert cp._pending_transaction_events == []
    assert cp._metrics[(1, csess.transaction_id)].value is None
    assert cp._tx_event_state["T"] == {"seq": 0, "ended": False}
    cp.session_controller.on_transaction_start.assert_not_called()
    cp.update.assert_awaited()

    # The charger's own queued copy of the same event arrives after the
    # reconnect: the replay re-created its ordering record, so it is stale.
    energy = MeasurandEnumType.energy_active_import_register.value
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 1, "connector_id": 1},
        offline=True,
        meter_value=[
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sampled_value": [
                    {
                        "value": 9999,
                        "measurand": energy,
                        "unit_of_measure": {"unit": "Wh"},
                    }
                ],
            }
        ],
    )
    assert cp._metrics[(1, energy)].value is None

    # The transaction is still running: its next online event adopts it.
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:01:00Z",
        "MeterValuePeriodic",
        1,
        {"transaction_id": "T"},
        evse={"id": 1, "connector_id": 1},
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "T"


@pytest.mark.asyncio
async def test_late_foreign_ended_does_not_retire_the_displayed_transaction(hass):
    """An Ended for another transaction says nothing about the one running."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    energy = MeasurandEnumType.energy_active_import_register.value
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-a"},
        **evse,
    )
    cp.on_transaction_event(
        "Started",
        "2026-01-01T01:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-b"},
        **evse,
    )
    cp._metrics[(1, energy)].value = 5.0
    cp.session_controller.reset_mock()

    # The charger notices A's loss late: its Ended is stamped after B began
    # and carries no offline flag.
    cp.on_transaction_event(
        "Ended",
        "2026-01-01T01:30:00Z",
        "EVCommunicationLost",
        1,
        {"transaction_id": "tx-a", "charging_state": "Idle"},
        meter_value=[
            {
                "timestamp": "2026-01-01T01:30:00Z",
                "sampled_value": [
                    {
                        "value": 4000,
                        "measurand": energy,
                        "unit_of_measure": {"unit": "Wh"},
                    }
                ],
            }
        ],
        **evse,
    )

    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    assert cp._metrics[(1, energy)].value == 5.0
    assert 1 in cp._tx_start_time
    cp.session_controller.on_transaction_end.assert_not_called()


@pytest.mark.asyncio
async def test_predated_foreign_update_is_replayed_history_not_evidence(hass):
    """History without the offline flag must not flap the displayed session."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T01:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-b"},
        **evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:10:00Z",
        "MeterValuePeriodic",
        1,
        {"transaction_id": "tx-b"},
        **evse,
    )
    cp.session_controller.reset_mock()

    response = cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:30:00Z",
        "MeterValuePeriodic",
        7,
        {"transaction_id": "tx-a"},
        id_token={"type": "ISO14443", "id_token": "ABCD"},
        **evse,
    )

    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    assert 1 in cp._tx_start_time
    assert response.id_token_info == {"status": AuthorizationStatusEnumType.accepted}
    cp.session_controller.on_transaction_end.assert_not_called()

    # A naive timestamp is read as UTC rather than defeating the comparison.
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:45:00",
        "MeterValuePeriodic",
        8,
        {"transaction_id": "tx-a"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    cp.session_controller.on_transaction_end.assert_not_called()

    # Stamped after B began but before B's latest event: still a replay.
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:05:00Z",
        "MeterValuePeriodic",
        9,
        {"transaction_id": "tx-a"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    cp.session_controller.on_transaction_end.assert_not_called()

    # Genuinely newer evidence for another transaction still supersedes B.
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T02:00:00Z",
        "MeterValuePeriodic",
        10,
        {"transaction_id": "tx-c"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-c"
    cp.session_controller.on_transaction_end.assert_called_once_with(1, "tx-b")


@pytest.mark.asyncio
async def test_adopted_display_is_guarded_by_its_latest_event(hass):
    """A transaction adopted without a Started still rejects older replays."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:00:00Z",
        "MeterValuePeriodic",
        5,
        {"transaction_id": "tx-b"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"

    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:30:00Z",
        "MeterValuePeriodic",
        9,
        {"transaction_id": "tx-a"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    cp.session_controller.on_transaction_end.assert_not_called()


@pytest.mark.asyncio
async def test_a_new_start_or_end_resets_the_history_bound(hass):
    """The bound belongs to the displayed transaction, not the connector."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T01:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-b"},
        **evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:10:00Z",
        "MeterValuePeriodic",
        1,
        {"transaction_id": "tx-b"},
        **evse,
    )
    # The charger clock steps back before C starts; B is retired by C's
    # Started, so D's later evidence must not be judged against B's events.
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:50:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-c"},
        **evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:00:00Z",
        "MeterValuePeriodic",
        3,
        {"transaction_id": "tx-d"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-d"

    # The same after a transaction ends normally, with D's bound already
    # later than anything the next transaction will report.
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:25:00Z",
        "MeterValuePeriodic",
        4,
        {"transaction_id": "tx-d"},
        **evse,
    )
    cp.on_transaction_event(
        "Ended",
        "2026-01-01T01:30:00Z",
        "EVDisconnected",
        5,
        {"transaction_id": "tx-d"},
        **evse,
    )
    cp.on_transaction_event(
        "Started",
        "2026-01-01T01:15:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "tx-e"},
        **evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:20:00Z",
        "MeterValuePeriodic",
        2,
        {"transaction_id": "tx-f"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-f"


@pytest.mark.asyncio
async def test_online_update_with_nothing_displayed_adopts_the_transaction_id(hass):
    """After a restart the first online Updated names the transaction to stop."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:01:00Z",
        "MeterValuePeriodic",
        3,
        {"transaction_id": "tx-b"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"
    cp.session_controller.on_transaction_start.assert_not_called()

    cp.on_transaction_event(
        "Updated",
        "2026-01-01T01:02:00Z",
        "MeterValuePeriodic",
        9,
        {"transaction_id": "history"},
        offline=True,
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "tx-b"


@pytest.mark.asyncio
async def test_invalid_sequence_number_is_ignored(hass, caplog):
    """A seqNo that is not a number cannot advance any transaction state."""
    cp = _mk_cp(hass)
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        "first",
        {"transaction_id": "T"},
        evse={"id": 1, "connector_id": 1},
    )
    assert cp._metrics[(1, csess.transaction_id)].value is None
    assert cp._tx_event_state == {}
    assert "invalid seqNo" in caplog.text


@pytest.mark.asyncio
async def test_malformed_evse_or_connector_ids_are_not_attributed(hass):
    """Ids that are not integers cannot allocate or touch a connector."""
    cp = _mk_cp(hass)
    for evse in ({"id": "x", "connector_id": 1}, {"id": 1, "connector_id": "x"}):
        cp.on_transaction_event(
            "Started",
            "2026-01-01T00:00:00Z",
            "CablePluggedIn",
            0,
            {"transaction_id": "T", "charging_state": "Charging"},
            evse=evse,
        )
    assert cp._metrics[(1, csess.transaction_id)].value is None
    assert cp._tx_event_state == {}
    assert cp._evse_to_global == {(1, 1): 1}


@pytest.mark.asyncio
async def test_missing_connector_resolves_from_the_configured_count_after_settle(
    hass,
):
    """With no usable report, a single configured connector is unambiguous."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.settings.num_connectors = 1
    cp._inventory = None
    cp._inventory_mapping_pending = False
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 1},
    )
    cp.session_controller.on_transaction_start.assert_called_once_with(1, "T", (1, 1))


@pytest.mark.asyncio
async def test_missing_connector_resolves_from_a_complete_dynamic_map(hass):
    """Once every configured connector is mapped, the map counts an EVSE's."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.settings.num_connectors = 2
    cp._inventory = None
    cp._inventory_mapping_pending = False
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._evse_to_global.update({(1, 1): 1, (2, 1): 2})
    cp._global_to_evse.update({1: (1, 1), 2: (2, 1)})
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 2},
    )
    cp.session_controller.on_transaction_start.assert_called_once_with(2, "T", (2, 1))


@pytest.mark.asyncio
async def test_ambiguous_missing_connector_still_reports_the_evse_state(hass):
    """The EVSE-level state is real even when the connector cannot be named."""
    cp = _mk_cp(hass)
    cp._inventory = InventoryReport(evse_count=1, connector_count=[2])
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._build_connector_map()
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:00:00Z",
        "ChargingStateChanged",
        1,
        {"transaction_id": "T", "charging_state": "Charging"},
        evse={"id": 1},
    )
    assert cp._metrics[(1, csess.transaction_id)].value is None
    assert cp._evse_status_v16[1] == ChargePointStatusv16.charging


@pytest.mark.asyncio
async def test_full_ordering_guard_prefers_evicting_an_ended_record(hass):
    """An ended transaction's record is the one to give up, never a live one."""
    cp = _mk_cp(hass)
    cp._tx_event_state = {
        f"old-{index}": {"seq": 1, "ended": False} for index in range(1023)
    }
    cp._tx_event_state["done"] = {"seq": 9, "ended": True}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "new"},
        evse={"id": 1, "connector_id": 1},
    )
    assert "new" in cp._tx_event_state
    assert "done" not in cp._tx_event_state
    assert "old-0" in cp._tx_event_state
    assert len(cp._tx_event_state) == 1024


@pytest.mark.asyncio
async def test_boot_resets_the_history_bound_for_a_restarted_clock(hass):
    """After a boot the previous generation's bound must not judge new events."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "before-boot"},
        **evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:30:00Z",
        "MeterValuePeriodic",
        1,
        {"transaction_id": "before-boot"},
        **evse,
    )

    cp.on_boot_notification({}, "PowerUp")
    await hass.async_block_till_done()

    # The charger's clock restarted behind its previous value.
    cp.on_transaction_event(
        "Updated",
        "1970-01-01T00:00:10Z",
        "MeterValuePeriodic",
        3,
        {"transaction_id": "after-boot"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "after-boot"


@pytest.mark.asyncio
async def test_boot_fences_session_baselines_when_transaction_id_is_reused(hass):
    """A reused id cannot inherit timing, token or meter state from before boot."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.triggered_boot_notification = True
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "reused"},
        id_token={"type": "ISO14443", "id_token": "OLD"},
        **evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:05:00Z",
        "MeterValuePeriodic",
        5,
        {"transaction_id": "reused"},
        **evse,
    )
    assert cp._tx_event_state["reused"]["seq"] == 5
    cp._metrics[(1, csess.meter_start)].value = 100
    cp._metrics[(1, csess.session_energy)].value = 4

    cp.on_boot_notification({}, "PowerUp")

    assert cp._metrics[(1, csess.transaction_id)].value == "reused"
    assert cp._metrics[(1, cstat.id_tag)].value == ""
    assert cp._metrics[(1, csess.meter_start)].value is None
    assert cp._metrics[(1, csess.session_energy)].value is None
    assert cp._metrics[(1, csess.session_time)].value is None
    assert 1 not in cp._tx_start_time

    cp.on_transaction_event(
        "Updated",
        "1970-01-01T00:00:10Z",
        "MeterValuePeriodic",
        0,
        {"transaction_id": "reused"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "reused"
    assert cp._metrics[(1, csess.session_time)].value is None
    assert cp._tx_event_state["reused"] == {"seq": 0, "ended": False}


@pytest.mark.asyncio
async def test_boot_preserves_the_last_completed_session_readings(hass):
    """Generation fencing must not erase a connector's retained history."""
    cp = _mk_cp(hass)
    cp.triggered_boot_notification = True
    cp._metrics[(1, csess.transaction_id)].value = ""
    cp._metrics[(1, csess.meter_start)].value = 100
    cp._metrics[(1, csess.session_energy)].value = 4
    cp._metrics[(1, csess.session_time)].value = 10

    cp.on_boot_notification({}, "PowerUp")

    assert cp._metrics[(1, csess.meter_start)].value == 100
    assert cp._metrics[(1, csess.session_energy)].value == 4
    assert cp._metrics[(1, csess.session_time)].value == 10


@pytest.mark.asyncio
async def test_missing_connector_uses_the_configured_count_when_the_report_has_none(
    hass,
):
    """A report naming no connector is not a report of zero connectors."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.settings.num_connectors = 1
    cp._inventory = InventoryReport(evse_count=1, connector_count=[0])
    cp._inventory_mapping_pending = False
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        evse={"id": 1},
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "T"
    cp.session_controller.on_transaction_start.assert_called_once_with(1, "T", (1, 1))


@pytest.mark.asyncio
async def test_replaying_held_events_publishes_one_update(hass):
    """A replay of many held events refreshes Home Assistant once."""
    cp = _mk_cp(hass)
    cp._inventory = None
    cp._evse_to_global.clear()
    cp._global_to_evse.clear()
    cp._inventory_mapping_pending = True
    evse = {"evse": {"id": 1, "connector_id": 1}}
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T", "charging_state": "EVConnected"},
        **evse,
    )
    for seq, minute in ((1, 1), (2, 2)):
        cp.on_transaction_event(
            "Updated",
            f"2026-01-01T00:0{minute}:00Z",
            "ChargingStateChanged",
            seq,
            {"transaction_id": "T", "charging_state": "Charging"},
            **evse,
        )
    assert len(cp._pending_transaction_events) == 3
    cp.update = AsyncMock()

    cp._inventory = InventoryReport(evse_count=1, connector_count=[1])
    cp._settle_inventory_boundary()
    await hass.async_block_till_done()

    assert cp.update.await_count == 1
    assert cp._metrics[(1, csess.transaction_id)].value == "T"
    assert not cp._replaying_transaction_events


@pytest.mark.asyncio
async def test_backward_transaction_timestamp_never_publishes_negative_time(hass):
    """A charger clock correction must not turn elapsed session time negative."""
    cp = _mk_cp(hass)
    transaction = {"transaction_id": "tx-clock"}
    evse = {"id": 1, "connector_id": 1}

    cp.on_transaction_event(
        "Started",
        "2026-01-01T01:00:00Z",
        "CablePluggedIn",
        0,
        transaction,
        evse=evse,
    )
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:00:00Z",
        "MeterValuePeriodic",
        1,
        transaction,
        evse=evse,
    )

    assert cp._metrics[(1, csess.session_time)].value == 0


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

    _tx_event(cp, ChargingStateEnumType.idle, seq_no=2)

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
    assert cp._metrics[(1, csess.transaction_id)].value is None
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
    _tx_event(cp, ChargingStateEnumType.idle, seq_no=2)

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


@pytest.mark.asyncio
async def test_a_failing_hook_cannot_break_transaction_events(hass, caplog):
    """A hook that raises is logged; the event still moves connector state."""
    cp = _mk_cp(hass)
    cp.session_controller = Mock()
    cp.session_controller.on_transaction_start.side_effect = RuntimeError("start")
    cp.session_controller.on_transaction_end.side_effect = RuntimeError("end")
    evse = {"evse": {"id": 1, "connector_id": 1}}

    response = cp.on_transaction_event(
        "Started",
        "2026-01-01T00:00:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "T"},
        id_token={"type": "ISO14443", "id_token": "TAG"},
        **evse,
    )
    assert response.id_token_info == {"status": "Accepted"}
    assert cp._metrics[(1, csess.transaction_id)].value == "T"
    assert cp._metrics[(1, cstat.id_tag)].value == "ISO14443:TAG"
    assert "session hook failed on transaction T start" in caplog.text

    # An online Started for another transaction retires T (end hook raises)
    # and starts U (start hook raises); the display must still switch.
    cp.on_transaction_event(
        "Started",
        "2026-01-01T00:05:00Z",
        "CablePluggedIn",
        0,
        {"transaction_id": "U"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == "U"
    assert "session hook failed on transaction T end" in caplog.text

    cp.on_transaction_event(
        "Ended",
        "2026-01-01T00:10:00Z",
        "EVDisconnected",
        1,
        {"transaction_id": "U"},
        **evse,
    )
    assert cp._metrics[(1, csess.transaction_id)].value == ""
    assert cp._tx_event_state["U"] == {"seq": 1, "ended": True}
    assert caplog.text.count("session hook failed") == 4
