"""Initial-start admission through the real central-system and v1.6 store load."""

import asyncio
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.exceptions import ConnectionClosedOK
from websockets.protocol import State

from custom_components.ocpp.api import CentralSystem
from custom_components.ocpp.const import DOMAIN

from .const import MOCK_CONFIG_FLOW
from .test_reconnect_lifecycle import Socket, ticks


class ReceiveSocket(Socket):
    """Transport boundary for the unmodified OCPP receive loop."""

    subprotocol = "ocpp1.6"
    request = SimpleNamespace(path="/test_cp_id")

    async def recv(self):
        """Wait for transport closure, just as a real WebSocket receive does."""
        await self.ended.wait()
        raise ConnectionClosedOK(None, None)


@pytest.mark.parametrize("action", ["stop", "stop_reconnect", "reconnect", "rebuild"])
async def test_stop_fences_store_blocked_initial_start(hass, monkeypatch, action):
    """A v1.6 store load cannot admit an old start after stop or replacement."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_FLOW.copy())
    central = CentralSystem(hass, entry)
    old, new = ReceiveSocket(), ReceiveSocket()
    entered, release = asyncio.Event(), asyncio.Event()
    monitored = []
    runners = []
    build = central._build_charge_point

    async def load():
        """Hold the genuine asynchronous storage boundary, not run publication."""
        entered.set()
        await release.wait()
        return None

    def build_with_blocked_store(*args):
        """Keep the real constructor and lifecycle; control only storage I/O."""
        charge_point = build(*args)
        if charge_point._ocpp_version == "1.6":
            monkeypatch.setattr(charge_point._tx_store, "async_load", load)
        monitor = charge_point.monitor_connection

        async def observe_monitor():
            """Record actual monitor entry without introducing a new await."""
            monitored.append(charge_point._connection)
            await monitor()

        monkeypatch.setattr(charge_point, "monitor_connection", observe_monitor)
        return charge_point

    monkeypatch.setattr(central, "_build_charge_point", build_with_blocked_store)
    initial = asyncio.create_task(central.on_connect(old))
    runners.append(initial)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        charge_point = central.charge_points["test_cp_id"]
        assert charge_point.tasks is None
        if action in ("stop", "stop_reconnect"):
            await charge_point.stop()
            assert charge_point._session["tasks"] == ()
            assert old.state is State.CLOSED
        replacement = action != "stop"
        if replacement:
            if action == "rebuild":
                new.subprotocol = "ocpp2.0.1"
            runners.append(asyncio.create_task(central.on_connect(new)))
            await ticks()
            active = central.charge_points["test_cp_id"]
            assert active._connection is new
            assert monitored == [new]
            assert old.state is State.CLOSED
        expected_session = charge_point._session
        expected_tasks = charge_point.tasks
        release.set()
        await charge_point._tx_store_load
        await ticks()
        assert charge_point._session is expected_session, (
            "late start republished session"
        )
        assert charge_point.tasks is expected_tasks, "late start replaced owned tasks"
        assert monitored == ([new] if replacement else [])
        assert initial.done(), "stopped initial handler resumed protocol execution"
        if replacement:
            assert new.state is State.OPEN
            assert all(not task.done() for task in active.tasks)
        else:
            assert charge_point.tasks is None
        await central.charge_points["test_cp_id"].stop()
    finally:
        release.set()
        for connection in (old, new):
            connection.ended.set()
            connection.close_release.set()
        await ticks()
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        await ticks()
