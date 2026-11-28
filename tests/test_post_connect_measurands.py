"""Measurands setup finds are saved, and the stored list only grows (#2258).

post_connect copied only the top level of the entry, changed the charger's
record in place and passed the result to async_update_entry. That changed
entry.data as well, so Home Assistant saw no difference and nothing was
saved: what setup found was lost at the next restart, and a measurand the
charger started sampling never got a sensor that lasted.
"""

import asyncio
import contextlib
import copy
import logging
from types import SimpleNamespace

import pytest
import websockets
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from ocpp.routing import on
from ocpp.v16 import call_result
from ocpp.v16.enums import Action
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp import (
    async_reload_entry,
    async_remove_config_entry_device,
    async_remove_entry,
)
from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    DEFAULT_MEASURAND,
    DEFAULT_MONITORED_VARIABLES,
    DOMAIN,
    sensor_unique_id,
)
from custom_components.ocpp.chargepoint import MeasurandValue
from custom_components.ocpp.enums import ConfigurationKey as ckey

from .charge_point_test import create_configuration, remove_configuration
from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA
from .test_charge_point_v16 import ChargePoint, wait_ready

ENERGY_AND_VOLTAGE = f"{DEFAULT_MEASURAND},Voltage"


def _record(entry, cp_id):
    return next(r[cp_id] for r in entry.data[CONF_CPIDS] if cp_id in r)


def _store(hass, entry, cp_id, **values):
    """Change the charger's record the way the options would."""
    data = copy.deepcopy(dict(entry.data))
    _record(SimpleNamespace(data=data), cp_id).update(values)
    hass.config_entries.async_update_entry(entry, data=data)


async def _settle():
    """Let a scheduled update listener run."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _wait_for(condition, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


class ChargePointSamplingVoltage(ChargePoint):
    """A simulated charger set to sample voltage as well as energy."""

    sampled = ENERGY_AND_VOLTAGE

    @on(Action.get_configuration)
    def on_get_configuration(self, key, **kwargs):
        """Report the sampled list, and the rest as usual."""
        if key[0] == ckey.meter_values_sampled_data:
            return call_result.GetConfiguration(
                configuration_key=[
                    {"key": key[0], "readonly": False, "value": self.sampled}
                ]
            )
        return super().on_get_configuration(key, **kwargs)


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9338, "cp_id": "CP_measurands_saved", "cms": "cms_measurands"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_measurands_saved"])
@pytest.mark.parametrize("port", [9338])
async def test_new_measurands_are_saved_without_a_reload(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch, caplog
):
    """Test only new measurands are saved, with a warning and no reload.

    The same ones in another order, fewer, none, unknown names, or any with
    automatic detection off in the stored record change nothing. A connector
    count found on connect is never put into the entry, before or after it
    unloads, so no save carries it, for this charger or another. A change
    made anywhere else still reloads.
    """
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]
            entry = srv.entry
            # Connecting found nothing that isn't stored
            assert (
                _record(entry, cp_id)[CONF_MONITORED_VARIABLES]
                == DEFAULT_MONITORED_VARIABLES
            )

            reloads = []
            saves = []
            notes = []
            real_update = hass.config_entries.async_update_entry

            async def fake_reload(entry_id):
                reloads.append(entry_id)
                return True

            def counting_update(*args, **kwargs):
                # Whether Home Assistant saw a change, and so saved it
                changed = real_update(*args, **kwargs)
                saves.append(changed)
                return changed

            async def fake_notify(msg, title="Ocpp integration"):
                notes.append(msg)
                return True

            monkeypatch.setattr(hass.config_entries, "async_reload", fake_reload)
            monkeypatch.setattr(srv, "notify_ha", fake_notify)
            found = {"measurands": "", "connectors": 1}

            async def fake_measurands():
                return found["measurands"]

            async def fake_connectors():
                return found["connectors"]

            monkeypatch.setattr(srv, "get_supported_measurands", fake_measurands)
            monkeypatch.setattr(srv, "get_number_of_connectors", fake_connectors)

            # A change made elsewhere reloads, as before
            _store(hass, entry, cp_id, **{CONF_MONITORED_VARIABLES: DEFAULT_MEASURAND})
            await _settle()
            assert reloads == [entry.entry_id]
            monkeypatch.setattr(
                hass.config_entries, "async_update_entry", counting_update
            )

            async def connect(measurands, connectors=1):
                """Run setup, and return what was stored, saved, reloaded and said."""
                found["measurands"] = measurands
                found["connectors"] = connectors
                reloads.clear()
                saves.clear()
                notes.clear()
                start = len(caplog.records)
                with caplog.at_level(logging.WARNING):
                    await srv.post_connect()
                    await _settle()
                warnings = [
                    r
                    for r in caplog.records[start:]
                    if r.levelno == logging.WARNING
                    and "had no sensors" in r.getMessage()
                ]
                return (
                    _record(entry, cp_id)[CONF_MONITORED_VARIABLES],
                    saves.count(True),
                    list(reloads),
                    len(warnings),
                    len([n for n in notes if "had no sensors" in n]),
                )

            e_v = ENERGY_AND_VOLTAGE
            # A new measurand is saved, with a warning and a notification, and
            # no reload
            assert await connect(e_v) == (e_v, 1, [], 1, 1)
            note = next(n for n in notes if "had no sensors" in n)
            assert "Voltage" in note and cp_id in note
            # The same ones in another order aren't new
            assert await connect(f"Voltage,{DEFAULT_MEASURAND}") == (e_v, 0, [], 0, 0)
            # Fewer aren't either: a stored measurand is never removed
            assert await connect(DEFAULT_MEASURAND) == (e_v, 0, [], 0, 0)
            # Nothing resolved changes nothing
            assert await connect("") == (e_v, 0, [], 0, 0)
            # A new one is added to what's stored, even when the charger no
            # longer reports something stored
            e_v_c = f"{e_v},Current.Import"
            assert await connect(f"{DEFAULT_MEASURAND},Current.Import") == (
                e_v_c,
                1,
                [],
                1,
                1,
            )

            # A name the integration doesn't know isn't stored
            assert await connect(f"{e_v_c},Energy.Active.Net") == (e_v_c, 0, [], 0, 0)

            # A connector count found on connect is kept outside the entry
            # straight away, so it's kept even if the charger is replaced
            # before the entry unloads
            assert await connect(e_v_c, 2) == (e_v_c, 0, [], 0, 0)
            assert _record(entry, cp_id)[CONF_NUM_CONNECTORS] == 1
            assert cs.found_connectors == {cp_id: 2}
            assert hass.data[DOMAIN]["found_connectors"][entry.entry_id] == {cp_id: 2}

            # So a measurand save carries only stored counts, for this charger
            # and for another one with a different count found
            data = copy.deepcopy(dict(entry.data))
            other = copy.deepcopy(_record(entry, cp_id))
            other[CONF_CPID] = "other_cpid"
            data[CONF_CPIDS].append({"CP_other": other})
            real_update(entry, data=data)
            await _settle()
            monkeypatch.setitem(cs.found_connectors, "CP_other", 3)
            e_v_c_f = f"{e_v_c},Frequency"
            assert await connect(e_v_c_f, 2) == (e_v_c_f, 1, [], 1, 1)
            saved = entry.as_dict()["data"]
            assert _record(SimpleNamespace(data=saved), cp_id)[CONF_NUM_CONNECTORS] == 1
            assert (
                _record(SimpleNamespace(data=saved), "CP_other")[CONF_NUM_CONNECTORS]
                == 1
            )

            # The counts found stay outside the entry for the setup after a
            # reload, and the entry still holds what's stored
            assert cs.found_connectors == {cp_id: 2, "CP_other": 3}
            assert _record(entry, cp_id)[CONF_NUM_CONNECTORS] == 1
            assert _record(entry, "CP_other")[CONF_NUM_CONNECTORS] == 1

            async def count_after_late_read(replace, meanwhile=4):
                """Return the count kept when setup's charger changes mid-read."""
                kept = []
                with monkeypatch.context() as m:

                    async def probe():
                        replace(m)
                        # A count kept meanwhile, by a replacement or an
                        # earlier setup
                        if meanwhile is not None:
                            cs.found_connectors[cp_id] = meanwhile
                        return 1

                    async def heartbeat():
                        # Past the count: note what's kept, then put the live
                        # charger back for the rest of setup
                        kept.append(cs.found_connectors.get(cp_id))
                        m.undo()

                    m.setattr(srv, "get_number_of_connectors", probe)
                    m.setattr(srv, "get_heartbeat_interval", heartbeat)
                    await srv.post_connect()
                    await _settle()
                cs.found_connectors[cp_id] = 2
                return kept[0]

            # A late result from a setup whose charger was rebuilt, say for
            # another OCPP version, or whose connection was replaced or
            # closed, isn't kept: the count already kept stands
            assert (
                await count_after_late_read(
                    lambda m: m.setitem(cs.charge_points, cp_id, SimpleNamespace())
                )
                == 4
            )
            assert (
                await count_after_late_read(
                    lambda m: m.setattr(srv, "_connection", object())
                )
                == 4
            )
            assert (
                await count_after_late_read(
                    lambda m: m.setattr(srv._connection.protocol, "state", State.CLOSED)
                )
                == 4
            )

            def remove_charger(m):
                """Do what removing the charger's device does to its record."""
                m.setattr(
                    srv, "entry", SimpleNamespace(entry_id=entry.entry_id, data={})
                )
                m.delitem(cs.found_connectors, cp_id)

            # Nor from a setup whose charger was removed meanwhile, so its
            # count can't come back for another charger added with its OCPP id
            assert await count_after_late_read(remove_charger, None) is None

            # Without an update listener yet, nothing reloads anyway, so no
            # marker is left behind to skip a later change
            cs.saved_by_setup = None
            with monkeypatch.context() as m:
                m.setattr(entry, "update_listeners", [])
                assert await connect(f"{e_v_c_f},SoC") == (
                    f"{e_v_c_f},SoC",
                    1,
                    [],
                    1,
                    1,
                )
                assert cs.saved_by_setup is None
            e_v_c_f = f"{e_v_c_f},SoC"
            # A save after that still stores only the stored counts
            saved = entry.as_dict()["data"]
            assert _record(SimpleNamespace(data=saved), cp_id)[CONF_NUM_CONNECTORS] == 1
            assert (
                _record(SimpleNamespace(data=saved), "CP_other")[CONF_NUM_CONNECTORS]
                == 1
            )

            # With automatic detection off in the stored record, the selection
            # is left alone, even though this setup started with it on
            monkeypatch.setattr(hass.config_entries, "async_update_entry", real_update)
            _store(hass, entry, cp_id, **{CONF_MONITORED_VARIABLES_AUTOCONFIG: False})
            await _settle()
            monkeypatch.setattr(
                hass.config_entries, "async_update_entry", counting_update
            )
            assert srv.settings.monitored_variables_autoconfig
            assert await connect(f"{e_v_c_f},Temperature") == (e_v_c_f, 0, [], 0, 0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9340, "cp_id": "CP_measurands_sent", "cms": "cms_measurands_sent"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_measurands_sent"])
@pytest.mark.parametrize("port", [9340])
async def test_measurands_sent_without_a_sensor_are_not_dropped(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch, caplog
):
    """Test a measurand in meter values with no sensor isn't silently dropped.

    With automatic detection on, it's saved and warned about once, like one
    setup found. With it off, a warning says it isn't selected. One the
    integration has no sensor type for is warned about once, and not stored.
    """
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]
            entry = srv.entry
            reloads = []
            notes = []

            async def fake_reload(entry_id):
                reloads.append(entry_id)
                return True

            async def fake_notify(msg, title="Ocpp integration"):
                notes.append(msg)
                return True

            monkeypatch.setattr(hass.config_entries, "async_reload", fake_reload)
            monkeypatch.setattr(srv, "notify_ha", fake_notify)
            _store(hass, entry, cp_id, **{CONF_MONITORED_VARIABLES: DEFAULT_MEASURAND})
            await _settle()

            def values(*names):
                return [
                    [
                        MeasurandValue(name, 1.0, None, None, None, None)
                        for name in names
                    ]
                ]

            async def receive(*names):
                """Process meter values, and return what was stored and said."""
                reloads.clear()
                notes.clear()
                start = len(caplog.records)
                with caplog.at_level(logging.WARNING):
                    srv.process_measurands(values(*names), False, 1)
                    await _settle()
                said = [
                    r.getMessage()
                    for r in caplog.records[start:]
                    if r.levelno == logging.WARNING
                    and (
                        "had no sensors" in r.getMessage()
                        or "isn't among the chosen" in r.getMessage()
                        or "has no sensors for" in r.getMessage()
                    )
                ]
                return (
                    _record(entry, cp_id)[CONF_MONITORED_VARIABLES],
                    list(reloads),
                    said,
                    [n for n in notes if "had no sensors" in n],
                )

            # A value without a measurand is the energy register, already stored
            assert await receive(None) == (DEFAULT_MEASURAND, [], [], [])
            # Voltage has no sensor, so it's saved and warned about once
            stored, reloaded, said, noted = await receive(None, "Voltage")
            assert (stored, reloaded) == (ENERGY_AND_VOLTAGE, [])
            assert len(said) == 1 and "Voltage" in said[0] and cp_id in said[0]
            assert len(noted) == 1 and "Voltage" in noted[0]
            assert await receive("Voltage") == (ENERGY_AND_VOLTAGE, [], [], [])
            # One the integration has no sensor type for, like this OCPP 2.0.1
            # measurand, is said once, and not stored
            stored, reloaded, said, noted = await receive("Energy.Active.Net")
            assert (stored, reloaded, noted) == (ENERGY_AND_VOLTAGE, [], [])
            assert len(said) == 1 and "has no sensors for" in said[0]
            assert "Energy.Active.Net" in said[0] and cp_id in said[0]
            assert await receive("Energy.Active.Net") == (
                ENERGY_AND_VOLTAGE,
                [],
                [],
                [],
            )

            # A malformed name, which skipping schema validation lets through,
            # doesn't stop the readings with it being processed
            srv.process_measurands(
                [
                    [
                        MeasurandValue(5, 1.0, None, None, None, None),
                        MeasurandValue("Voltage", 230.0, None, "V", None, None),
                    ]
                ],
                False,
                1,
            )
            assert srv._metrics[(1, "Voltage")].value == 230.0

            # A save that fails doesn't stop the readings with it being
            # processed, and the measurand is checked again with the next values
            failed = []

            def failing_update(entry, data=None):
                failed.append(data)
                raise RuntimeError("update failed")

            start = len(caplog.records)
            with monkeypatch.context() as m, caplog.at_level(logging.WARNING):
                m.setattr(hass.config_entries, "async_update_entry", failing_update)
                srv.process_measurands(
                    [[MeasurandValue("Current.Import", 16.0, None, "A", None, None)]],
                    False,
                    1,
                )
                await _settle()
            assert srv._metrics[(1, "Current.Import")].value == 16.0
            assert len(failed) == 1
            assert _record(SimpleNamespace(data=failed[0]), cp_id)[
                CONF_MONITORED_VARIABLES
            ] == (f"{ENERGY_AND_VOLTAGE},Current.Import")
            assert [
                r.getMessage()
                for r in caplog.records[start:]
                if "Couldn't save" in r.getMessage()
            ] == [
                f"Couldn't save the measurands charger '{cp_id}' sends: update failed"
            ]
            assert _record(entry, cp_id)[CONF_MONITORED_VARIABLES] == (
                ENERGY_AND_VOLTAGE
            )
            e_v_c = f"{ENERGY_AND_VOLTAGE},Current.Import"
            stored, reloaded, said, noted = await receive("Current.Import")
            assert (stored, reloaded) == (e_v_c, [])
            assert len(said) == 1 and "Current.Import" in said[0]
            assert len(noted) == 1 and "Current.Import" in noted[0]

            # With automatic detection off, it's said once, and not saved
            _store(hass, entry, cp_id, **{CONF_MONITORED_VARIABLES_AUTOCONFIG: False})
            await _settle()
            stored, reloaded, said, noted = await receive("Frequency")
            assert stored == e_v_c
            assert len(said) == 1 and "isn't among the chosen" in said[0]
            assert "Frequency" in said[0] and noted == []
            assert await receive("Frequency") == (e_v_c, [], [], [])

            # One that can't be selected but is stored has a sensor, so
            # nothing is said
            apparent = f"{e_v_c},Energy.Apparent.Import.Register"
            _store(hass, entry, cp_id, **{CONF_MONITORED_VARIABLES: apparent})
            await _settle()
            assert await receive("Energy.Apparent.Import.Register") == (
                apparent,
                [],
                [],
                [],
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


async def test_update_listener_skips_only_setups_own_save(hass, monkeypatch):
    """Test the listener reloads unless the data is exactly what setup saved."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CPIDS: []})
    entry.add_to_hass(hass)
    reloads = []

    async def fake_reload(entry_id):
        reloads.append(entry_id)
        return True

    monkeypatch.setattr(hass.config_entries, "async_reload", fake_reload)
    central = SimpleNamespace(saved_by_setup=None)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = central
    try:
        # Nothing saved by setup: a reload, as before
        await async_reload_entry(hass, entry)
        assert reloads == [entry.entry_id]

        # Exactly what setup saved: no reload, and the marker is used up
        reloads.clear()
        central.saved_by_setup = dict(entry.data)
        await async_reload_entry(hass, entry)
        assert reloads == []
        assert central.saved_by_setup is None

        # Changed since setup saved: a reload
        central.saved_by_setup = {CONF_CPIDS: [{"other": {}}]}
        await async_reload_entry(hass, entry)
        assert reloads == [entry.entry_id]
        assert central.saved_by_setup is None

        # With the listener on the entry, a setup save doesn't reload, and a
        # preference change after it, which leaves the data alone, does
        entry.add_update_listener(async_reload_entry)
        reloads.clear()
        setup_saved = {CONF_CPIDS: [{"cp0": {}}]}
        central.saved_by_setup = setup_saved
        hass.config_entries.async_update_entry(entry, data=setup_saved)
        await hass.async_block_till_done()
        assert reloads == []
        hass.config_entries.async_update_entry(entry, pref_disable_new_entities=True)
        await hass.async_block_till_done()
        assert reloads == [entry.entry_id]

        # Home Assistant starts listeners inside the update today. If it ran
        # them later, a change made elsewhere, then a setup save that carries
        # it, would still reload.
        reloads.clear()
        real_create_task = hass.async_create_task
        with monkeypatch.context() as m:
            m.setattr(
                hass,
                "async_create_task",
                lambda target, name=None, eager_start=True: real_create_task(
                    target, name, eager_start=False
                ),
            )
            changed = {**setup_saved, CONF_PORT: 9999}
            hass.config_entries.async_update_entry(entry, data=changed)
            setup_saved = {**changed, CONF_CPIDS: [{"cp0": {}}, {"cp1": {}}]}
            central.saved_by_setup = setup_saved
            hass.config_entries.async_update_entry(entry, data=setup_saved)
            await hass.async_block_till_done()
        assert entry.entry_id in reloads
    finally:
        hass.data[DOMAIN].pop(entry.entry_id, None)


async def test_removed_entry_forgets_its_connector_counts(hass):
    """Test removing an entry drops only its kept connector counts."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_CPIDS: []})
    hass.data.setdefault(DOMAIN, {})["found_connectors"] = {
        entry.entry_id: {"CP_a": 2},
        "other_entry": {"CP_a": 3},
    }
    await async_remove_entry(hass, entry)
    assert hass.data[DOMAIN]["found_connectors"] == {"other_entry": {"CP_a": 3}}


@pytest.mark.timeout(60)
async def test_detected_measurand_gets_a_sensor_at_the_next_load(
    hass, socket_enabled, monkeypatch
):
    """Test a measurand found on connect is saved, and its sensor follows.

    Connecting saves it without reloading, so the charger stays connected and
    the sensor isn't there yet. The next reload creates it, using the
    connector count the charger reported. That count is never put into the
    entry, so a save before or after the reload stores only what was stored,
    and it's forgotten when the charger is removed.
    """
    cp_id = "CP_measurand_sensor"
    port = 9339
    config_data = copy.deepcopy(MOCK_CONFIG_DATA)
    record = copy.deepcopy(MOCK_CONFIG_CP_APPEND)
    record[CONF_MONITORED_VARIABLES] = DEFAULT_MEASURAND
    config_data[CONF_CPIDS].append({cp_id: record})
    config_data[CONF_PORT] = port
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_data,
        entry_id="cms_measurand_sensor",
        title="cms_measurand_sensor",
        version=2,
        minor_version=0,
    )
    cs = await create_configuration(hass, entry)
    ent_reg = er.async_get(hass)
    voltage = sensor_unique_id(record["cpid"], "Voltage")
    reloads = []
    real_reload = hass.config_entries.async_reload

    async def counting_reload(entry_id):
        reloads.append(entry_id)
        return await real_reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", counting_reload)

    def stored_count():
        """Return the charger's count in the entry, and as it would be saved."""
        saved = entry.as_dict()["data"]
        return (
            _record(entry, cp_id)[CONF_NUM_CONNECTORS],
            _record(SimpleNamespace(data=saved), cp_id)[CONF_NUM_CONNECTORS],
        )

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
        ) as ws:
            cp = ChargePointSamplingVoltage(f"{cp_id}_client", ws, no_connectors=2)
            task = asyncio.create_task(cp.start())
            try:
                await cp.send_boot_notification()
                await wait_ready(cs.charge_points[cp_id])
                await _settle()
                assert _record(entry, cp_id)[CONF_MONITORED_VARIABLES] == (
                    ENERGY_AND_VOLTAGE
                )
                assert reloads == []
                assert hass.data[DOMAIN][entry.entry_id] is cs
                assert ent_reg.async_get_entity_id("sensor", DOMAIN, voltage) is None
                # The save stored the count that was stored
                assert cs.charge_points[cp_id].num_connectors == 2
                assert stored_count() == (1, 1)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await ws.close()

        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        new_cs = hass.data[DOMAIN][entry.entry_id]
        assert new_cs is not cs
        # The setup after the reload used the count the charger reported, so
        # voltage has a sensor on each connector, but the entry still holds
        # the stored count, which is what a save queued before the reload
        # writes too
        assert new_cs.found_connectors == {cp_id: 2}
        for connector in (1, 2):
            uid = sensor_unique_id(record["cpid"], "Voltage", connector)
            assert ent_reg.async_get_entity_id("sensor", DOMAIN, uid) is not None
        # Every platform used it
        second_connector = {
            e.domain
            for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
            if "conn2" in e.unique_id.split(".")
        }
        assert {"sensor", "switch", "button", "number"} <= second_connector
        assert stored_count() == (1, 1)

        # A measurand saved after the reload stores the stored count too, and
        # the charger is set up with the count it reported
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
        ) as ws:
            cp = ChargePointSamplingVoltage(f"{cp_id}_client", ws, no_connectors=2)
            cp.sampled = f"{ENERGY_AND_VOLTAGE},Current.Import"
            task = asyncio.create_task(cp.start())
            try:
                await cp.send_boot_notification()
                await wait_ready(new_cs.charge_points[cp_id])
                await _settle()
                assert _record(entry, cp_id)[CONF_MONITORED_VARIABLES] == (
                    f"{ENERGY_AND_VOLTAGE},Current.Import"
                )
                assert reloads == [entry.entry_id]
                assert new_cs.charge_points[cp_id].settings.num_connectors == 2
                assert stored_count() == (1, 1)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await ws.close()

        # Removing the charger forgets its count, so a charger added later with
        # its OCPP id doesn't inherit it, and a replacement with the same name
        # and another OCPP id is set up with its own count
        device = dr.async_get(hass).async_get(
            dr.async_get_device_id_by_identifier(
                hass, (DOMAIN, cp_id), config_entry_id=entry.entry_id
            )
        )
        assert await async_remove_config_entry_device(hass, entry, device)
        await hass.async_block_till_done()
        assert hass.data[DOMAIN]["found_connectors"][entry.entry_id] == {}
        replacement = copy.deepcopy(record)
        replacement[CONF_NUM_CONNECTORS] = 1
        replacement[CONF_MONITORED_VARIABLES] = ENERGY_AND_VOLTAGE
        data = copy.deepcopy(dict(entry.data))
        data[CONF_CPIDS].append({"CP_replacement": replacement})
        hass.config_entries.async_update_entry(entry, data=data)
        await hass.async_block_till_done()
        assert ent_reg.async_get_entity_id("sensor", DOMAIN, voltage) is not None
    finally:
        await remove_configuration(hass, entry)
