"""Test additional chargepoint paths."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
import websockets
from websockets.protocol import State

from custom_components.ocpp.chargepoint import ChargePoint as BaseCP, MeasurandValue


# Reuse the client helpers & fixtures from your main v16 test module.
from .test_charge_point_v16 import wait_ready, ChargePoint


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9410, "cp_id": "CP_cov_base_defaults", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_base_defaults"])
@pytest.mark.parametrize("port", [9410])
async def test_base_default_methods_return_values(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Covers 299, 307, 315, 413, 417, 425, 429, 433, 451–453, 455–457: base defaults & no-op behaviors."""
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        client = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(client.start())
        try:
            await client.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]

            # Use the base-class implementations explicitly to cover the base lines,
            # even though v16 overrides some of these.
            assert (
                await BaseCP.get_number_of_connectors(srv) == srv.num_connectors
            )  # L299
            assert await BaseCP.get_supported_measurands(srv) == ""  # L307
            assert (
                await BaseCP.get_supported_features(srv) == 0
            )  # L315 (prof.NONE is 0)

            assert await BaseCP.set_availability(srv, True) is False  # L413
            assert await BaseCP.start_transaction(srv, 1) is False  # L417
            assert await BaseCP.stop_transaction(srv) is False  # L425
            assert await BaseCP.reset(srv) is False  # L429
            assert await BaseCP.unlock(srv, 1) is False  # L433

            assert await BaseCP.get_configuration(srv, "Foo") is None  # L451–453
            assert await BaseCP.configure(srv, "Foo", "Bar") is None  # L455–457
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9413, "cp_id": "CP_cov_handle_call", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_handle_call"])
@pytest.mark.parametrize("port", [9413])
async def test_handle_call_notimplemented_sends_call_error(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Covers 520–526: wrapper catches ocpp.exceptions.NotImplementedError and sends CallError JSON."""
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        client = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(client.start())
        try:
            await client.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]

            # Patch the exact base alias used by the subclass, and raise the *OCPP* NotImplementedError.
            import custom_components.ocpp.chargepoint as cp_mod
            from ocpp.exceptions import NotImplementedError as OcppNotImplementedError

            async def boom(self, msg):
                # Raise the OCPP exception class that the wrapper actually catches.
                raise OcppNotImplementedError(details={"cause": "nyi"})

            captured = {"payload": None}

            async def fake_send(self, payload):
                # _handle_call builds a JSON string via to_json(), then calls _send(...)
                captured["payload"] = (
                    payload.to_json() if hasattr(payload, "to_json") else payload
                )

            # Patch on the cp alias (the base class your subclass imports as `cp`).
            monkeypatch.setattr(cp_mod.cp, "_handle_call", boom, raising=True)
            monkeypatch.setattr(cp_mod.cp, "_send", fake_send, raising=True)

            class Msg:
                """Minimal message stub compatible with msg.create_call_error(e)."""

                def create_call_error(self, *_, **__):
                    from types import SimpleNamespace

                    # Return an object with to_json() so wrapper turns it into a JSON string.
                    return SimpleNamespace(to_json=lambda: '{"error":"NotImplemented"}')

            # Invoke: the wrapper should catch the OCPP NotImplementedError and call _send with JSON.
            await srv._handle_call(Msg())

            assert captured["payload"] == '{"error":"NotImplemented"}'
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9414, "cp_id": "CP_cov_run_paths", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_run_paths"])
@pytest.mark.parametrize("port", [9414])
async def test_run_handles_timeout_and_other_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Covers 537 and 540–541: run() swallows TimeoutError and logs other exceptions, then stops."""
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        client = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(client.start())
        try:
            await client.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]

            stopped = {"count": 0}

            async def fake_stop():
                stopped["count"] += 1

            monkeypatch.setattr(srv, "stop", fake_stop, raising=True)

            async def raises_timeout():
                await asyncio.sleep(0)
                raise TimeoutError("simulated")

            async def raises_other():
                await asyncio.sleep(0)
                raise ValueError("simulated")

            # TimeoutError path -> should be swallowed (L537) and then stop() called.
            await srv.run([raises_timeout()])
            assert stopped["count"] >= 1

            # Other exception path -> should be logged via L540–541 and then stop() called again.
            await srv.run([raises_other()])
            assert stopped["count"] >= 2
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9415, "cp_id": "CP_cov_update_early", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_update_early"])
@pytest.mark.parametrize("port", [9415])
async def test_update_returns_early_when_root_device_missing(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Covers 602: update() returns early if the root device cannot be found in the device registry."""
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        client = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(client.start())
        try:
            await client.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]

            # Fake registries: no device returned.
            import custom_components.ocpp.chargepoint as mod

            class FakeDR:
                """Fake DR."""

                def async_get_device(self, identifiers):
                    return None

                def async_clear_config_entry(self, config_entry_id):
                    return None

                @property
                def devices(self):
                    """Fake devices."""
                    return {}

                def async_update_device(self, *args, **kwargs):
                    return None

                def async_get_or_create(self, *args, **kwargs):
                    return SimpleNamespace(id="dummy")

            class FakeER:
                """Fake ER."""

                def async_clear_config_entry(self, config_entry_id):
                    return None

            def fake_entries_for_device(_er, _dev_id):
                # No entities to update; the loop is exercised anyway.
                return []

            # Patch HA helpers & dispatcher.
            monkeypatch.setattr(
                mod.device_registry, "async_get", lambda _: FakeDR(), raising=True
            )
            monkeypatch.setattr(
                mod.entity_registry, "async_get", lambda _: FakeER(), raising=True
            )

            monkeypatch.setattr(
                mod.entity_registry,
                "async_entries_for_device",
                fake_entries_for_device,
                raising=True,
            )
            monkeypatch.setattr(
                mod, "async_dispatcher_send", lambda *args, **kw: None, raising=True
            )

            # Should exit early without error (L602).
            await srv.update(srv.settings.cpid)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9416, "cp_id": "CP_cov_update_walk", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_update_walk"])
@pytest.mark.parametrize("port", [9416])
async def test_update_traverses_children_and_skips_visited(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Covers 612 and 623–624: skips already visited IDs and appends children discovered via via_device_id."""
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        client = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(client.start())
        try:
            await client.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]

            # Build a tiny fake device graph:
            # root -> child (twice in the values() list to create a duplicate push)
            import custom_components.ocpp.chargepoint as mod

            class Dev:
                """Fake Dev."""

                def __init__(self, id, via=None):
                    self.id = id
                    self.via_device_id = via

            root = Dev("root", via=None)
            child = Dev("child", via="root")

            class FakeDR:
                """Fake DR."""

                def async_clear_config_entry(self, config_entry_id):
                    return None

                def async_update_device(self, *args, **kwargs):
                    return None

                def async_get_or_create(self, *args, **kwargs):
                    return SimpleNamespace(id="dummy")

                def async_get_device(self, identifiers):
                    return root

                @property
                def devices(self):
                    # Duplicate the child to force the same ID to be appended twice -> will hit continue (L612)
                    class Container:
                        def values(self_inner):
                            return [root, child, child]

                    return Container()

            class FakeER:
                """Fake ER."""

                def async_clear_config_entry(self, config_entry_id):
                    return None

            def fake_entries_for_device(_er, _dev_id):
                # No entities to update; the loop is exercised anyway.
                return []

            # Patch HA helpers & dispatcher.
            monkeypatch.setattr(
                mod.device_registry, "async_get", lambda _: FakeDR(), raising=True
            )
            monkeypatch.setattr(
                mod.entity_registry, "async_get", lambda _: FakeER(), raising=True
            )
            monkeypatch.setattr(
                mod.entity_registry,
                "async_entries_for_device",
                fake_entries_for_device,
                raising=True,
            )
            monkeypatch.setattr(
                mod, "async_dispatcher_send", lambda *args, **kw: None, raising=True
            )

            # No exceptions expected; internal traversal will append 'child' twice,
            # so on second pop it will be in 'visited' and trigger L612 'continue'.
            await srv.update(srv.settings.cpid)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(5)
async def test_process_measurands_defaults_and_session_energy_v2x(hass, monkeypatch):
    """Covers 830–832, 836, 881–887: default EAIR measurand/unit and Energy.Session handling for 2.x."""
    # Minimal CP instance not bound to a real socket.
    version = SimpleNamespace(value="2.0.1")
    fake_hass = SimpleNamespace(
        async_create_task=lambda c: asyncio.create_task(c),
        helpers=SimpleNamespace(
            entity_component=SimpleNamespace(async_update_entity=lambda eid: None)
        ),
    )
    fake_entry = SimpleNamespace(entry_id="dummy")
    fake_central = SimpleNamespace(
        websocket_ping_interval=0,
        websocket_ping_timeout=0,
        websocket_ping_tries=0,
    )
    fake_settings = SimpleNamespace(cpid="cpid_dummy")
    fake_conn = SimpleNamespace(state=State.CLOSED)

    srv = BaseCP(
        "cp_dummy",
        fake_conn,
        version,
        fake_hass,
        fake_entry,
        fake_central,
        fake_settings,
    )
    srv._ocpp_version = "2.0.1"  # ensure 2.x path

    # 1) Missing measurand -> defaults to EAIR; missing unit -> defaults to Wh then normalized to kWh.
    samples1 = [[MeasurandValue(None, 12345.0, None, None, None, None)]]
    srv.process_measurands(
        samples1, is_transaction=True, connector_id=1
    )  # <-- no await

    eair = srv._metrics[(1, "Energy.Active.Import.Register")]
    assert eair.unit == "kWh"
    assert pytest.approx(eair.value, rel=1e-6) == 12.345

    esess = srv._metrics[(1, "Energy.Session")]
    assert esess.unit == "kWh"
    assert (esess.value or 0.0) == 0.0

    # 2) Next periodic EAIR sample increases by 100 Wh -> session delta = 0.1 kWh.
    samples2 = [[MeasurandValue(None, 12445.0, None, None, None, None)]]
    srv.process_measurands(
        samples2, is_transaction=True, connector_id=1
    )  # <-- no await

    eair2 = srv._metrics[(1, "Energy.Active.Import.Register")]
    esess2 = srv._metrics[(1, "Energy.Session")]
    assert pytest.approx(eair2.value, rel=1e-6) == 12.445
    assert pytest.approx(esess2.value or 0.0, rel=1e-6) == 0.1
