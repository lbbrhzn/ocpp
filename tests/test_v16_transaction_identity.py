"""OCPP 1.6 transaction identity, timing and containment (#2123).

The integration allocates 1.6 transaction ids itself. They used to be
int(time.time()), which was assumed unique, guessed onto connector 1 when a
StopTransaction could not be matched, and read back as the session's start
epoch. Each assumption had a failure mode:

- two connectors starting in the same second shared an id, so a stop for one
  was applied to whichever came first in the active map;
- an unknown stop reset connector 1, ending a session that may still have
  been running there;
- a charger-supplied id adopted from MeterValues after a restart is not an
  epoch, so the session timer counted from an arbitrary reference (the second
  symptom reported in #1938).

These tests drive the server-side handlers directly, the way the charger's
messages would, without a websocket.
"""

import asyncio
import logging
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from ocpp.charge_point import ChargePoint as LibChargePoint
from ocpp.v16.enums import ChargePointStatus, RemoteStartStopStatus
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from websockets.protocol import State

from custom_components.ocpp import ocppv16
from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import (
    ConfigurationKey as ckey,
    HAChargerSession as csess,
    OcppMisc as om,
    Profiles as prof,
)
from custom_components.ocpp.ocppv16 import ChargePoint, tx_store_key
from custom_components.ocpp.switch import SWITCHES, ChargePointSwitch

from .test_charge_point_core import _mk_entry_data

CP_ID = "CP_identity"
NOW = 1_800_000_000.0  # a fixed "now", far from any id the tests seed


def _store_key(entry: MockConfigEntry) -> str:
    return tx_store_key(entry.entry_id, CP_ID)


def _mk_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=_mk_entry_data())
    entry.add_to_hass(hass)
    return entry


def _mk_cp(
    hass: HomeAssistant, *, connectors: int = 2, entry: MockConfigEntry | None = None
) -> ChargePoint:
    entry = entry or _mk_entry(hass)
    # The authorization lookup in StartTransaction reads the integration's
    # config bucket; there is no CentralSystem here to have created it.
    hass.data.setdefault(DOMAIN, {})
    centr = CentralSystemSettings(**entry.data)
    chg = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    conn = SimpleNamespace(state=State.CLOSED, close=lambda: asyncio.sleep(0))
    cp = ChargePoint(CP_ID, conn, hass, entry, centr, chg)
    cp.num_connectors = connectors
    for c in range(1, connectors + 1):
        cp._init_connector_slots(c)
    return cp


@pytest.fixture
def frozen_time(monkeypatch):
    """Pin time.time() as the handlers see it; tests move it explicitly.

    Only the module under test sees the pinned clock: its `time` reference is
    replaced with a stub, so Home Assistant and the event loop keep real time.
    """
    clock = {"now": NOW}
    monkeypatch.setattr(ocppv16, "time", SimpleNamespace(time=lambda: clock["now"]))
    return clock


def _meter_values(
    tx_id: int, *, context: str = "Sample.Periodic", value: str = "1000"
) -> dict:
    return {
        "connector_id": 1,
        "meter_value": [
            {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                # Handlers see the library's snake_case conversion of the
                # wire payload, not the camelCase the charger sends.
                "sampled_value": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "context": context,
                        "unit": "Wh",
                        "value": value,
                    }
                ],
            }
        ],
        "transaction_id": tx_id,
    }


async def _settle(hass: HomeAssistant, cp: ChargePoint) -> None:
    """Let the store load and any scheduled update() tasks run."""
    cp._ensure_tx_store_loaded()
    await cp._tx_store_load
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


async def test_simultaneous_starts_get_distinct_ids(hass, frozen_time):
    """Two connectors starting in the same second must not share an id."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    first = cp.on_start_transaction(1, "tag-a", 0).transaction_id
    second = cp.on_start_transaction(2, "tag-b", 0).transaction_id
    await hass.async_block_till_done()

    assert first == int(NOW)
    assert second == first + 1
    assert cp._active_tx == {1: first, 2: second}
    assert cp._last_tx_id == second


async def test_ids_never_fall_below_the_persisted_last_id(
    hass, hass_storage, frozen_time
):
    """After a restart, allocation continues above the last id handed out.

    The persisted value is ahead of the clock here, as it would be after a
    burst of same-second starts or a clock that went backwards.
    """
    entry = _mk_entry(hass)
    key = _store_key(entry)
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"last_tx_id": int(NOW) + 1000, "connectors": {}},
    }
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)

    allocated = cp.on_start_transaction(1, "tag", 0).transaction_id
    await hass.async_block_till_done()

    assert allocated == int(NOW) + 1001


async def test_running_session_is_persisted_for_a_restart(
    hass, hass_storage, frozen_time
):
    """StartTransaction records the id and start time; the write is delayed."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    key = cp._tx_store.key

    tx = cp.on_start_transaction(1, "tag", 0).transaction_id
    assert cp._tx_store_snapshot() == {
        "last_tx_id": tx,
        "connectors": {"1": {"tx_id": tx, "started_at": NOW}},
    }
    # Nothing is written synchronously from the handler.
    assert key not in hass_storage
    async_fire_time_changed(hass, datetime.now(tz=UTC).replace(year=2100))
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["connectors"]["1"]["tx_id"] == tx


# --------------------------------------------------------------------------
# StopTransaction resolution
# --------------------------------------------------------------------------


async def test_known_stop_ends_only_its_connector(hass, frozen_time):
    """Control: a stop whose id is recorded ends that connector alone."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first = cp.on_start_transaction(1, "tag-a", 0).transaction_id
    second = cp.on_start_transaction(2, "tag-b", 0).transaction_id

    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=second)
    await hass.async_block_till_done()

    assert cp._active_tx == {1: first, 2: 0}
    assert cp._metrics[(1, csess.transaction_id)].value == first
    assert cp._metrics[(2, csess.transaction_id)].value == 0
    assert cp._tx_indeterminate == set()


async def test_unknown_stop_resolves_to_the_only_live_connector(hass, frozen_time):
    """One transaction running: an unrecorded stop id can only be for it."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp._active_tx[2] = 4242  # adopted from the charger, not our allocation
    cp._metrics[(2, csess.transaction_id)].value = 4242

    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=9999)
    await hass.async_block_till_done()

    assert cp._active_tx[2] == 0
    assert cp._metrics[(2, csess.transaction_id)].value == 0
    # Connector 1, the old fallback target, is untouched.
    assert cp._metrics[(1, csess.transaction_id)].value is None
    assert cp._tx_indeterminate == set()


async def test_unknown_stop_with_two_live_connectors_holds_both(hass, frozen_time):
    """Ambiguity is contained, not guessed: neither connector is reset."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first = cp.on_start_transaction(1, "tag-a", 0).transaction_id
    second = cp.on_start_transaction(2, "tag-b", 0).transaction_id

    resp = cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=9999)
    await hass.async_block_till_done()

    assert resp.id_tag_info["status"] == "Accepted"
    assert cp._active_tx == {1: first, 2: second}
    assert cp._tx_indeterminate == {1, 2}

    # The charger then reports connector 2 idle and connector 1 charging.
    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    cp.on_status_notification(1, "NoError", ChargePointStatus.charging.value)
    await hass.async_block_till_done()

    assert cp._active_tx == {1: first, 2: 0}
    assert cp._metrics[(2, csess.transaction_id)].value == 0
    assert cp._tx_indeterminate == set()


async def test_unknown_stop_with_nothing_running_changes_nothing(hass, frozen_time):
    """No transaction anywhere: the stop is accepted and nothing is reset."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    resp = cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=9999)
    await hass.async_block_till_done()

    assert resp.id_tag_info["status"] == "Accepted"
    assert cp._active_tx == {}
    assert cp._tx_indeterminate == set()
    assert cp._metrics[(1, csess.transaction_id)].value is None


async def test_duplicate_charger_ids_on_two_connectors_are_held(hass, frozen_time):
    """Charger-supplied ids can collide; a stop for one cannot be attributed."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    for c in (1, 2):
        cp._active_tx[c] = 700
        cp._metrics[(c, csess.transaction_id)].value = 700

    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=700)
    await hass.async_block_till_done()

    assert cp._active_tx == {1: 700, 2: 700}
    assert cp._tx_indeterminate == {1, 2}


async def test_held_connector_is_settled_by_its_own_meter_values(hass, frozen_time):
    """A sample for the held transaction proves it is still running."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag-a", 0).transaction_id
    cp.on_start_transaction(2, "tag-b", 0)
    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=9999)
    assert cp._tx_indeterminate == {1, 2}

    cp.on_meter_values(**_meter_values(tx))
    await hass.async_block_till_done()

    assert cp._tx_indeterminate == {2}
    assert cp._active_tx[1] == tx


# --------------------------------------------------------------------------
# Session time
# --------------------------------------------------------------------------


async def test_session_time_comes_from_the_recorded_start(hass, frozen_time):
    """Ten minutes after StartTransaction the timer reads ten minutes."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag", 0).transaction_id

    frozen_time["now"] = NOW + 600
    cp.on_meter_values(**_meter_values(tx))
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_time)].value == 10
    assert cp._metrics[(1, csess.session_time)].extra_attr == {}


async def test_adopted_transaction_counts_from_first_sighting_and_says_so(
    hass, frozen_time
):
    """A charger id is not an epoch: the timer must not read (now - id)."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    cp.on_meter_values(**_meter_values(4242))
    frozen_time["now"] = NOW + 300
    cp.on_meter_values(**_meter_values(4242))
    await hass.async_block_till_done()

    assert cp._active_tx[1] == 4242
    assert cp._metrics[(1, csess.session_time)].value == 5
    assert cp._metrics[(1, csess.session_time)].extra_attr == {
        "start_time_estimated": True
    }


async def test_adopted_transaction_matching_the_persisted_one_keeps_its_start(
    hass, hass_storage, frozen_time
):
    """The session running before a restart resumes with its real duration."""
    entry = _mk_entry(hass)
    key = _store_key(entry)
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "last_tx_id": 4242,
            "connectors": {"1": {"tx_id": 4242, "started_at": NOW - 1800}},
        },
    }
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)

    cp.on_meter_values(**_meter_values(4242))
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_time)].value == 30
    assert cp._metrics[(1, csess.session_time)].extra_attr == {}


async def test_session_time_is_never_negative(hass, frozen_time):
    """A clock that went backwards yields zero, not a negative duration."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag", 0).transaction_id

    frozen_time["now"] = NOW - 3600
    cp.on_meter_values(**_meter_values(tx))
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_time)].value == 0


async def test_closing_values_leave_the_final_session_time(hass, frozen_time):
    """The Transaction.End sample after a stop must not restart the clock."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag", 0).transaction_id
    frozen_time["now"] = NOW + 600
    cp.on_meter_values(**_meter_values(tx))
    assert cp._metrics[(1, csess.session_time)].value == 10

    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=tx)
    frozen_time["now"] = NOW + 660
    cp.on_meter_values(**_meter_values(tx, context="Transaction.End"))
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_time)].value == 10


async def test_held_connector_timer_resumes_on_its_own_sample(hass, frozen_time):
    """A held connector's timer stands still until a sample settles it."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag-a", 0).transaction_id
    cp.on_start_transaction(2, "tag-b", 0)
    frozen_time["now"] = NOW + 600
    cp.on_meter_values(**_meter_values(tx))
    assert cp._metrics[(1, csess.session_time)].value == 10

    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=9999)
    # Nothing updates the timer while the connector is held. The first sample
    # for its transaction settles it as running and the timer resumes.
    frozen_time["now"] = NOW + 1200
    cp.on_meter_values(**_meter_values(tx))
    assert cp._metrics[(1, csess.session_time)].value == 20
    await hass.async_block_till_done()


async def test_broken_store_is_tolerated(hass, frozen_time, caplog):
    """A store that cannot be read must not stop transactions from starting."""
    cp = _mk_cp(hass)

    async def boom():
        raise OSError("disk unhappy")

    cp._tx_store.async_load = boom
    await _settle(hass, cp)

    assert cp.on_start_transaction(1, "tag", 0).transaction_id == int(NOW)
    assert "could not load transaction state" in caplog.text


# --------------------------------------------------------------------------
# Review findings on the first cut
# --------------------------------------------------------------------------


async def test_start_waits_for_the_store_before_the_message_loop(
    hass, hass_storage, frozen_time, monkeypatch
):
    """The first message must see the persisted ceiling, not race the load."""
    entry = _mk_entry(hass)
    key = _store_key(entry)
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"last_tx_id": int(NOW) + 1000, "connectors": {}},
    }
    cp = _mk_cp(hass, entry=entry)
    seen = {}

    async def first_message_arrives(self):
        # Stands in for the library's receive loop: the charger's first
        # message is a StartTransaction.
        seen["loaded"] = cp._tx_store_load is not None and cp._tx_store_load.done()
        seen["id"] = cp.on_start_transaction(1, "tag", 0).transaction_id

    async def no_monitor():
        return None

    monkeypatch.setattr(LibChargePoint, "start", first_message_arrives)
    monkeypatch.setattr(cp, "monitor_connection", no_monitor)

    await cp.start()
    await hass.async_block_till_done()

    assert seen == {"loaded": True, "id": int(NOW) + 1001}


async def test_a_store_that_loads_late_still_corrects_an_estimated_start(
    hass, hass_storage, frozen_time
):
    """A path that bypasses start() gets its estimate replaced once loaded."""
    entry = _mk_entry(hass)
    key = _store_key(entry)
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "last_tx_id": 4242,
            "connectors": {"1": {"tx_id": 4242, "started_at": NOW - 1800}},
        },
    }
    cp = _mk_cp(hass, entry=entry)
    # Home Assistant starts tasks eagerly and the storage mock never suspends,
    # so make the load yield once, as real disk I/O does.
    real_load = cp._tx_store.async_load

    async def slow_load():
        await asyncio.sleep(0)
        return await real_load()

    cp._tx_store.async_load = slow_load

    # Adopted before the load has run: the only start known is "now".
    cp.on_meter_values(**_meter_values(4242))
    assert cp._metrics[(1, csess.session_time)].extra_attr == {
        "start_time_estimated": True
    }

    await cp._tx_store_load
    frozen_time["now"] = NOW + 60
    cp.on_meter_values(**_meter_values(4242))
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_time)].value == 31
    assert cp._metrics[(1, csess.session_time)].extra_attr == {}


async def test_an_adopted_id_is_not_handed_out_again(hass, frozen_time):
    """A charger id adopted from MeterValues is kept clear of allocation."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    cp.on_meter_values(**_meter_values(int(NOW)))
    assert cp._active_tx[1] == int(NOW)

    second = cp.on_start_transaction(2, "tag", 0).transaction_id
    await hass.async_block_till_done()

    assert second == int(NOW) + 1


async def test_a_restored_id_is_not_handed_out_again(hass, frozen_time, monkeypatch):
    """An id restored from Home Assistant state is kept clear of allocation."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    def restored(metric, connector_id=None):
        return int(NOW) if metric == csess.transaction_id else None

    monkeypatch.setattr(cp, "get_ha_metric", restored)
    cp.on_meter_values(**_meter_values(0))
    assert cp._active_tx[1] == int(NOW)

    second = cp.on_start_transaction(2, "tag", 0).transaction_id
    await hass.async_block_till_done()

    assert second == int(NOW) + 1


async def _two_held(hass, cp):
    """Start two transactions and hold both with an unattributable stop."""
    first = cp.on_start_transaction(1, "tag-a", 0).transaction_id
    second = cp.on_start_transaction(2, "tag-b", 0).transaction_id
    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9999, reason="EVDisconnected"
    )
    await hass.async_block_till_done()
    assert cp._tx_indeterminate == {1, 2}
    return first, second


@pytest.mark.parametrize(
    "status", [ChargePointStatus.faulted.value, ChargePointStatus.preparing.value]
)
async def test_ambiguous_statuses_keep_a_held_connector(hass, frozen_time, status):
    """Faulted and Preparing prove nothing, so the hold stays."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first, second = await _two_held(hass, cp)

    cp.on_status_notification(1, "NoError", status)
    await hass.async_block_till_done()

    assert cp._tx_indeterminate == {1, 2}
    assert cp._active_tx == {1: first, 2: second}


@pytest.mark.parametrize(
    "status",
    [
        ChargePointStatus.available.value,
        ChargePointStatus.finishing.value,
        ChargePointStatus.unavailable.value,
        ChargePointStatus.reserved.value,
    ],
)
async def test_idle_statuses_end_a_held_connector(hass, frozen_time, status):
    """A status that proves the connector has no transaction ends its hold."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first, _ = await _two_held(hass, cp)

    cp.on_status_notification(2, "NoError", status)
    await hass.async_block_till_done()

    assert cp._tx_indeterminate == {1}
    assert cp._active_tx == {1: first, 2: 0}


async def test_global_remote_stop_skips_a_held_connector(
    hass, frozen_time, monkeypatch
):
    """A global stop never targets an id a held connector may not own."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first, second = await _two_held(hass, cp)
    # Connector 1 proves it is running; connector 2 stays held. The legacy
    # global field points at connector 2's id, the last one started.
    cp.on_status_notification(1, "NoError", ChargePointStatus.charging.value)
    assert cp._tx_indeterminate == {2}
    assert cp.active_transaction_id == second
    sent = []

    async def accept(req):
        sent.append(req)
        return SimpleNamespace(status=RemoteStartStopStatus.accepted)

    monkeypatch.setattr(cp, "call", accept)
    assert await cp.stop_transaction() is True
    await hass.async_block_till_done()

    assert [req.transaction_id for req in sent] == [first]


async def test_no_tx_profile_is_bound_to_a_held_connector(
    hass, frozen_time, monkeypatch
):
    """The 1.6 fallback must not bind a profile to an uncertain transaction."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    await _two_held(hass, cp)
    cp._attr_supported_features = prof.SMART
    purposes = []

    async def configuration(key):
        if key == ckey.charging_schedule_allowed_charging_rate_unit:
            return "Current"
        return "5"

    async def reject_station_profile(req):
        purpose = req.cs_charging_profiles[om.charging_profile_purpose]
        purposes.append(purpose)
        accepted = purpose != "ChargePointMaxProfile"
        return SimpleNamespace(status="Accepted" if accepted else "Rejected")

    monkeypatch.setattr(cp, "get_configuration", configuration)
    monkeypatch.setattr(cp, "call", reject_station_profile)

    await cp.set_charge_rate(limit_amps=10, conn_id=1)
    await hass.async_block_till_done()

    assert "TxProfile" not in purposes
    assert "TxDefaultProfile" in purposes


async def test_pending_stop_is_applied_to_the_connector_that_ended(hass, frozen_time):
    """The unattributed stop's meter reading and reason reach the right session."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first, _ = await _two_held(hass, cp)
    assert cp._pending_stops[0]["meter_stop"] == 5000

    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    await hass.async_block_till_done()

    assert cp._metrics[(2, csess.session_energy)].value == 5.0
    assert cp._metrics[(2, "Stop.Reason")].value == "EVDisconnected"
    assert cp._metrics[(1, csess.session_energy)].value == 0.0
    assert cp._active_tx == {1: first, 2: 0}
    assert cp._pending_stops == []


def test_store_keys_differ_for_ids_that_slugify_alike():
    """Chargers of one entry whose ids slugify the same keep separate state."""
    keys = {tx_store_key("entry", cp_id) for cp_id in ("CP-A", "CP_A", "CP A")}

    assert len(keys) == 3
    assert all(k.startswith(f"{DOMAIN}.v16_transactions.entry.cp_a_") for k in keys)


async def test_an_id_adopted_after_a_stop_is_not_handed_out_again(hass, frozen_time):
    """The self-heal path (metric zeroed by a stop) also raises the floor.

    After a stop the connector's metric is 0, so a charger id seen in
    MeterValues is adopted by the self-heal branch rather than the restore
    branch. If that id is exactly the next one allocation would produce,
    the next StartTransaction must still not repeat it.
    """
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first = cp.on_start_transaction(1, "tag-a", 0).transaction_id
    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=first)
    assert cp._metrics[(1, csess.transaction_id)].value == 0

    cp.on_meter_values(**_meter_values(first + 1))
    assert cp._active_tx[1] == first + 1

    second = cp.on_start_transaction(2, "tag-b", 0).transaction_id
    await hass.async_block_till_done()

    assert second == first + 2


# --------------------------------------------------------------------------
# Second review: remote stops, concurrent unattributed stops, bad timestamps
# --------------------------------------------------------------------------


async def test_per_connector_remote_stop_refuses_a_held_connector(
    hass, frozen_time, monkeypatch
):
    """The Charge Control path names its connector; a held one is refused."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    await _two_held(hass, cp)
    sent, notices = [], []

    async def accept(req):
        sent.append(req)
        return SimpleNamespace(status=RemoteStartStopStatus.accepted)

    async def note(msg, title="Ocpp integration"):
        notices.append(msg)
        return True

    monkeypatch.setattr(cp, "call", accept)
    monkeypatch.setattr(cp, "notify_ha", note)

    assert await cp.stop_transaction(connector_id=1) is False
    await hass.async_block_till_done()

    assert sent == []
    assert notices and "unresolved" in notices[0]


async def test_global_remote_stop_skips_an_id_shared_with_a_held_connector(
    hass, frozen_time, monkeypatch
):
    """An unheld connector cannot lend an id a held connector may still own."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    for c in (1, 2):
        cp._active_tx[c] = 700
        cp._metrics[(c, csess.transaction_id)].value = 700
    cp.active_transaction_id = 700
    cp._tx_indeterminate = {1}
    sent = []

    async def accept(req):
        sent.append(req)
        return SimpleNamespace(status=RemoteStartStopStatus.accepted)

    monkeypatch.setattr(cp, "call", accept)

    assert await cp.stop_transaction() is True  # nothing safe to stop: no-op
    await hass.async_block_till_done()

    assert sent == []


async def test_central_system_reports_a_held_connector(hass):
    """The switch's availability gate reads the charge point's held set."""
    from .test_api_paths import _available_central_system

    cs, dummy = _available_central_system(hass)
    dummy._tx_indeterminate = {1}

    assert cs.is_transaction_indeterminate("ok", 1) is True
    assert cs.is_transaction_indeterminate("ok", 2) is False
    assert cs.is_transaction_indeterminate("nope", 1) is False


async def test_two_unattributed_stops_are_not_cross_applied(hass, frozen_time):
    """Neither connector may knowingly receive the other session's final data."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp.on_start_transaction(1, "tag-a", 1000)
    cp.on_start_transaction(2, "tag-b", 2000)
    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9001, reason="Local"
    )
    cp.on_stop_transaction(
        meter_stop=9000, timestamp=None, transaction_id=9002, reason="Remote"
    )
    assert cp._tx_indeterminate == {1, 2}
    assert len(cp._pending_stops) == 2

    cp.on_status_notification(1, "NoError", ChargePointStatus.available.value)
    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    await hass.async_block_till_done()

    for conn in (1, 2):
        assert cp._active_tx[conn] == 0
        assert cp._metrics[(conn, csess.session_energy)].value == 0.0
        assert cp._metrics[(conn, "Stop.Reason")].value is None
    assert cp._pending_stops == []


async def test_sequential_unattributed_stops_each_reach_their_connector(
    hass, frozen_time
):
    """One outstanding stop at a time is attributable and applied."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp.on_start_transaction(1, "tag-a", 1000)
    cp.on_start_transaction(2, "tag-b", 2000)

    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9001, reason="Local"
    )
    cp.on_status_notification(1, "NoError", ChargePointStatus.available.value)
    cp.on_stop_transaction(
        meter_stop=9000, timestamp=None, transaction_id=9002, reason="Remote"
    )
    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_energy)].value == 4.0
    assert cp._metrics[(1, "Stop.Reason")].value == "Local"
    assert cp._metrics[(2, csess.session_energy)].value == 7.0
    assert cp._metrics[(2, "Stop.Reason")].value == "Remote"
    assert cp._pending_stops == []


async def test_a_connector_settled_as_running_gives_up_its_claim(hass, frozen_time):
    """Once one candidate proves it is running, the stop must be the other's."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp.on_start_transaction(1, "tag-a", 1000)
    cp.on_start_transaction(2, "tag-b", 2000)
    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9001, reason="Local"
    )

    cp.on_status_notification(1, "NoError", ChargePointStatus.charging.value)
    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    await hass.async_block_till_done()

    assert cp._metrics[(2, csess.session_energy)].value == 3.0
    assert cp._metrics[(2, "Stop.Reason")].value == "Local"
    assert cp._pending_stops == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -5.0])
async def test_non_finite_persisted_start_falls_back_to_an_estimate(
    hass, hass_storage, frozen_time, bad
):
    """A corrupt timestamp must not make every later MeterValues raise."""
    entry = _mk_entry(hass)
    key = _store_key(entry)
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "last_tx_id": 4242,
            "connectors": {"1": {"tx_id": 4242, "started_at": bad}},
        },
    }
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)
    assert cp._persisted_tx == {}

    cp.on_meter_values(**_meter_values(4242))
    frozen_time["now"] = NOW + 120
    cp.on_meter_values(**_meter_values(4242))
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_time)].value == 2
    assert cp._metrics[(1, csess.session_time)].extra_attr == {
        "start_time_estimated": True
    }


async def test_charge_control_switch_is_unavailable_while_its_connector_is_held(
    hass,
):
    """The entity that remote-stops a connector goes unavailable while held."""
    from .test_api_paths import _available_central_system

    cs, dummy = _available_central_system(hass)
    charge_control = next(d for d in SWITCHES if d.key == "charge_control")
    assert charge_control.transaction_bound is True
    entity = ChargePointSwitch(cs, "ok", charge_control, connector_id=1)

    assert entity.available is True
    dummy._tx_indeterminate = {1}
    assert entity.available is False
    dummy._tx_indeterminate = {2}
    assert entity.available is True


async def test_a_stop_no_held_connector_can_own_is_discarded(hass, frozen_time):
    """Once every candidate proves it is running, the stale stop is dropped.

    Left in place it could later be mistaken for a fresh unattributed stop
    and make a genuine one ambiguous.
    """
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    await _two_held(hass, cp)
    assert len(cp._pending_stops) == 1

    cp.on_status_notification(1, "NoError", ChargePointStatus.charging.value)
    cp.on_status_notification(2, "NoError", ChargePointStatus.charging.value)
    await hass.async_block_till_done()

    assert cp._tx_indeterminate == set()
    assert cp._pending_stops == []


# --------------------------------------------------------------------------
# Third review: other evidence of how a candidate ended, and duplicate-id gating
# --------------------------------------------------------------------------


async def test_a_known_stop_on_a_candidate_invalidates_the_pending_stop(
    hass, frozen_time
):
    """A duplicate for the connector that then stopped properly is possible.

    So the other candidate must not receive the unattributed stop.
    """
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first = cp.on_start_transaction(1, "tag-a", 1000).transaction_id
    cp.on_start_transaction(2, "tag-b", 2000)
    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9999, reason="EVDisconnected"
    )
    assert cp._tx_indeterminate == {1, 2}

    cp.on_stop_transaction(
        meter_stop=3000, timestamp=None, transaction_id=first, reason="Local"
    )
    assert cp._pending_stops == []
    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    await hass.async_block_till_done()

    assert cp._metrics[(1, csess.session_energy)].value == 2.0
    assert cp._metrics[(1, "Stop.Reason")].value == "Local"
    assert cp._metrics[(2, csess.session_energy)].value == 0.0
    assert cp._metrics[(2, "Stop.Reason")].value is None
    assert cp._active_tx[2] == 0


async def test_a_new_start_on_a_candidate_invalidates_the_pending_stop(
    hass, frozen_time
):
    """A new transaction on a candidate supersedes whatever the stop ended."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp.on_start_transaction(1, "tag-a", 1000)
    cp.on_start_transaction(2, "tag-b", 2000)
    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9999, reason="EVDisconnected"
    )

    cp.on_start_transaction(1, "tag-c", 1500)
    assert cp._tx_indeterminate == {2}
    assert cp._pending_stops == []
    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    await hass.async_block_till_done()

    assert cp._metrics[(2, csess.session_energy)].value == 0.0
    assert cp._metrics[(2, "Stop.Reason")].value is None


async def test_closing_values_on_a_candidate_do_not_claim_the_pending_stop(
    hass, frozen_time
):
    """Closing values are evidence of how the connector's transaction ended.

    Unlike a blank idle report, they attribute nothing to it or to the other.
    """
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first = cp.on_start_transaction(1, "tag-a", 1000).transaction_id
    cp.on_start_transaction(2, "tag-b", 2000)
    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9999, reason="EVDisconnected"
    )

    cp.on_meter_values(**_meter_values(first, context="Transaction.End"))
    assert cp._tx_indeterminate == {2}
    assert cp._pending_stops == []
    assert cp._metrics[(1, "Stop.Reason")].value is None
    cp.on_status_notification(2, "NoError", ChargePointStatus.available.value)
    await hass.async_block_till_done()

    assert cp._metrics[(2, csess.session_energy)].value == 0.0
    assert cp._metrics[(2, "Stop.Reason")].value is None


async def test_foreign_closing_values_do_not_settle_a_held_connector(
    hass, frozen_time, caplog
):
    """Closing values for some other transaction prove nothing about this one.

    A Transaction.End context speaks only for the id it carries. A stray or
    delayed sample for another transaction leaves the held connector held,
    with its transaction, energy and flow intact and the pending stop in place.
    """
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    first = cp.on_start_transaction(1, "tag-a", 1000).transaction_id
    cp.on_start_transaction(2, "tag-b", 2000)
    cp.on_meter_values(**_meter_values(first, value="3000"))
    assert cp._metrics[(1, csess.session_energy)].value == 2.0
    cp._metrics[(1, "Power.Active.Import")].value = 7.0
    cp.on_stop_transaction(
        meter_stop=5000, timestamp=None, transaction_id=9999, reason="EVDisconnected"
    )
    pending = list(cp._pending_stops)

    cp.on_meter_values(
        **_meter_values(first + 500, context="Transaction.End", value="9000")
    )
    await hass.async_block_till_done()

    assert cp._tx_indeterminate == {1, 2}
    assert cp._pending_stops == pending
    assert cp._active_tx[1] == first
    assert cp._metrics[(1, csess.transaction_id)].value == first
    assert cp._metrics[(1, csess.session_energy)].value == 2.0
    assert cp._metrics[(1, "Power.Active.Import")].value == 7.0
    assert "Unknown transaction detected on conn 1" in caplog.text


async def test_closing_values_for_an_unknown_id_are_not_adopted(hass, frozen_time):
    """After a stop, another transaction's closing values start nothing here."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag", 1000).transaction_id
    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=tx)

    cp.on_meter_values(**_meter_values(tx + 500, context="Transaction.End"))
    await hass.async_block_till_done()

    assert cp._active_tx[1] == 0
    assert cp._metrics[(1, csess.transaction_id)].value == 0


async def test_first_closing_values_after_restart_are_not_adopted(hass, frozen_time):
    """A first sighting marked Transaction.End is not a live transaction."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    closing_tx = int(NOW)

    cp.on_meter_values(**_meter_values(closing_tx, context="Transaction.End"))
    await hass.async_block_till_done()

    assert cp._active_tx[1] == 0
    assert cp._metrics[(1, csess.transaction_id)].value is None
    assert 1 not in cp._tx_started_at
    # It is ended, not forgotten: do not allocate the same id immediately.
    allocated = cp.on_start_transaction(1, "tag", 1000).transaction_id
    await hass.async_block_till_done()
    assert allocated == closing_tx + 1


async def test_old_ended_id_does_not_settle_a_new_adopted_held_session(
    hass, frozen_time
):
    """The post-stop fallback cannot speak for a newer restored transaction."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    old = cp.on_start_transaction(1, "tag-a", 1000).transaction_id
    cp.on_stop_transaction(meter_stop=2000, timestamp=None, transaction_id=old)

    adopted = old + 500
    cp.on_meter_values(**_meter_values(adopted))
    assert cp._active_tx[1] == adopted
    cp.on_start_transaction(2, "tag-b", 1000)
    cp.on_stop_transaction(
        meter_stop=3000, timestamp=None, transaction_id=9999, reason="Remote"
    )
    assert cp._tx_indeterminate == {1, 2}
    pending = list(cp._pending_stops)

    cp.on_meter_values(**_meter_values(old, context="Transaction.End"))
    await hass.async_block_till_done()

    assert cp._active_tx[1] == adopted
    assert cp._metrics[(1, csess.transaction_id)].value == adopted
    assert cp._tx_indeterminate == {1, 2}
    assert cp._pending_stops == pending


async def test_switch_gating_uses_the_same_unsafe_id_rule_as_the_stop(hass):
    """A connector sharing its id with a held one cannot be stopped.

    Its Charge Control switch must be unavailable, not merely failing.
    """
    from .test_api_paths import _available_central_system

    cs, _ = _available_central_system(hass)
    cp = _mk_cp(hass)
    cs.charge_points["CP_OK"] = cp
    for c in (1, 2):
        cp._active_tx[c] = 700
        cp._metrics[(c, csess.transaction_id)].value = 700
    cp._tx_indeterminate = {1}
    charge_control = next(d for d in SWITCHES if d.key == "charge_control")

    assert cs.is_transaction_indeterminate("ok", 1) is True
    assert cs.is_transaction_indeterminate("ok", 2) is True
    assert (
        ChargePointSwitch(cs, "ok", charge_control, connector_id=2).available is False
    )

    cp._active_tx[2] = 701  # its own id: stoppable again
    cp._metrics[(2, csess.transaction_id)].value = 701
    assert cs.is_transaction_indeterminate("ok", 2) is False


async def test_malformed_store_contents_are_ignored(hass, hass_storage, frozen_time):
    """A store whose fields are the wrong shape leaves everything at defaults."""
    entry = _mk_entry(hass)
    key = _store_key(entry)
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"last_tx_id": "not-a-number", "connectors": ["not", "a", "dict"]},
    }
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)

    assert cp._last_tx_id == 0
    assert cp._persisted_tx == {}
    assert cp.on_start_transaction(1, "tag", 0).transaction_id == int(NOW)


async def test_malformed_connector_records_are_skipped(hass, hass_storage, frozen_time):
    """One bad connector record does not cost the good ones."""
    entry = _mk_entry(hass)
    key = _store_key(entry)
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "last_tx_id": 5,
            "connectors": {
                "x": {"tx_id": 1, "started_at": 1.0},
                "2": {"tx_id": "abc", "started_at": 1.0},
                "3": {"tx_id": 9},
                "1": {"tx_id": 700, "started_at": NOW - 60},
            },
        },
    }
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)

    assert cp._persisted_tx == {1: (700, NOW - 60)}
    assert cp._last_tx_id == 700


async def test_a_failing_store_write_does_not_stop_a_start(
    hass, frozen_time, caplog, monkeypatch
):
    """Persistence is best-effort: a broken store never blocks charging."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    def broken(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(cp._tx_store, "async_delay_save", broken)
    caplog.set_level(logging.DEBUG, logger="custom_components.ocpp")
    tx = cp.on_start_transaction(1, "tag", 0).transaction_id

    assert cp._active_tx[1] == tx
    assert "transaction state not persisted" in caplog.text


async def test_non_numeric_ids_are_inert(hass, frozen_time):
    """A non-numeric id is neither a live transaction nor a floor."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag", 0).transaction_id
    cp._metrics[(2, csess.transaction_id)].value = "garbage"

    assert cp._live_connectors() == [1]
    cp._note_transaction_id("garbage")
    assert cp._last_tx_id == tx


async def test_a_stop_is_resolved_by_the_recorded_metric_alone(hass, frozen_time):
    """After a restart the metric may be all that names the transaction."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp._active_tx.clear()
    cp._metrics[(2, csess.transaction_id)].value = 555

    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=555)
    await hass.async_block_till_done()

    assert cp._metrics[(2, csess.transaction_id)].value == 0
    assert cp._tx_indeterminate == set()
    assert cp._pending_stops == []


async def test_an_id_recorded_on_two_connectors_holds_both(hass, frozen_time):
    """Duplicate ids left behind by an older release cannot be told apart."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp._active_tx.clear()
    for c in (1, 2):
        cp._metrics[(c, csess.transaction_id)].value = 555

    cp.on_stop_transaction(meter_stop=5000, timestamp=None, transaction_id=555)
    await hass.async_block_till_done()

    assert cp._tx_indeterminate == {1, 2}
    assert [p["candidates"] for p in cp._pending_stops] == [{1, 2}]


async def test_an_unreadable_meter_stop_records_no_session_energy(hass, frozen_time):
    """A stop whose reading cannot be parsed still ends the session."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag", 1000).transaction_id

    cp.on_stop_transaction(meter_stop="broken", timestamp=None, transaction_id=tx)
    await hass.async_block_till_done()

    assert cp._active_tx[1] == 0
    assert cp._metrics[(1, csess.session_energy)].value == 0.0


async def test_a_station_level_control_is_never_gated_by_a_hold(hass):
    """Only a connector's own switch answers to its hold."""
    cp = _mk_cp(hass)
    cp._active_tx[1] = 700
    cp._tx_indeterminate = {1}

    assert cp.transaction_is_unsafe(1) is True
    assert cp.transaction_is_unsafe(None) is False
