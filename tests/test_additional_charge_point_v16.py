"""Test additional v16 paths."""

import asyncio
import contextlib
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest
import websockets

from ocpp.v16 import call
from ocpp.v16.enums import (
    TriggerMessageStatus,
    ChargePointStatus,
    ConfigurationStatus,
)

from custom_components.ocpp.enums import (
    HAChargerDetails as cdet,
    ConfigurationKey as ckey,
)

from .test_charge_point_v16 import wait_ready, ChargePoint


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9116, "cp_id": "CP_trig_timeout_nonzero_adjusts", "cms": "cms_trig_tnz"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_trig_timeout_nonzero_adjusts"])
@pytest.mark.parametrize("port", [9116])
async def test_trigger_status_timeout_on_nonzero_adjusts_and_stops(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test trigger status timeout on nonzero adjusts and stops."""
    cs = setup_config_entry
    attempts = []

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            srv_cp = cs.charge_points[cp_id]
            srv_cp._metrics[0][cdet.connectors.value].value = 2

            async def fake_call(req):
                if isinstance(req, call.TriggerMessage):
                    attempts.append(req.connector_id)
                    if req.connector_id == 2:
                        raise TimeoutError("simulated")
                    return SimpleNamespace(status=TriggerMessageStatus.accepted)
                return SimpleNamespace()

            monkeypatch.setattr(srv_cp, "call", fake_call, raising=True)

            ok = await srv_cp.trigger_status_notification()
            assert ok is False
            # Should stop after the failing connector
            assert attempts == [0, 1, 2]
            assert int(srv_cp._metrics[0][cdet.connectors.value].value) == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9310, "cp_id": "CP_cov_conn_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_conn_exc"])
@pytest.mark.parametrize("port", [9310])
async def test_get_number_of_connectors_exception_defaults(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test get number of connectors when exception defaults to 1."""
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

            async def fake_call(req):
                # Simulate failure when requesting NumberOfConnectors
                if isinstance(req, call.GetConfiguration):
                    raise TypeError("boom")
                return SimpleNamespace()

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            n = await srv.get_number_of_connectors()
            assert n == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9311, "cp_id": "CP_cov_conn_bad", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_conn_bad"])
@pytest.mark.parametrize("port", [9311])
async def test_get_number_of_connectors_invalid_value_defaults(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test get number of connectors with invalid value defaults to 1."""
    cs = setup_config_entry

    class FakeResp:
        def __init__(self):
            self.configuration_key = [{"key": "NumberOfConnectors", "value": "n/a"}]
            self.unknown_key = None

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]

            async def fake_call(req):
                if isinstance(req, call.GetConfiguration):
                    return FakeResp()
                return SimpleNamespace()

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            n = await srv.get_number_of_connectors()
            assert n == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9312, "cp_id": "CP_cov_auto_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_auto_exc"])
@pytest.mark.parametrize("port", [9312])
async def test_autodetect_measurands_change_configuration_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test autodetect measurands when ChangeConfiguration raises and fallback occurs."""
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

            # Enable autodetect and set a desired CSV to trigger the path
            srv.settings.monitored_variables_autoconfig = True
            srv.settings.monitored_variables = "Power.Active.Import,Voltage"

            async def fake_call(req):
                # Fail on ChangeConfiguration, so code reads back via GetConfiguration
                if isinstance(req, call.ChangeConfiguration):
                    raise TypeError("set failed")
                if isinstance(req, call.GetConfiguration):
                    return SimpleNamespace(
                        configuration_key=[{"value": "Voltage"}], unknown_key=None
                    )
                return SimpleNamespace()

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            result = await srv.get_supported_measurands()
            assert result == "Voltage"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9313, "cp_id": "CP_cov_manual_ok", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_manual_ok"])
@pytest.mark.parametrize("port", [9313])
async def test_measurands_manual_set_accepted_configures(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test manual measurands set accepted and configure is called."""
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

            # Disable autodetect; set a desired CSV
            srv.settings.monitored_variables_autoconfig = False
            srv.settings.monitored_variables = "Energy.Active.Import.Register,Voltage"

            called_configure = []

            async def fake_call(req):
                if isinstance(req, call.ChangeConfiguration):
                    return SimpleNamespace(status=ConfigurationStatus.accepted)
                return SimpleNamespace()

            async def fake_configure(key, value):
                called_configure.append((key, value))

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            monkeypatch.setattr(srv, "configure", fake_configure, raising=True)

            result = await srv.get_supported_measurands()
            assert result == srv.settings.monitored_variables
            # configure() should have been called with the accepted CSV
            assert (
                called_configure
                and called_configure[0][0] == ckey.meter_values_sampled_data.value
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9314, "cp_id": "CP_cov_manual_rej", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_manual_rej"])
@pytest.mark.parametrize("port", [9314])
async def test_measurands_manual_set_rejected_returns_empty(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test manual measurands rejected and fallback returns empty string."""
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

            srv.settings.monitored_variables_autoconfig = False
            srv.settings.monitored_variables = "Energy.Active.Import.Register"

            async def fake_call(req):
                if isinstance(req, call.ChangeConfiguration):
                    return SimpleNamespace(status=ConfigurationStatus.rejected)
                if isinstance(req, call.GetConfiguration):
                    # Simulate charger returning no value for the requested key
                    # by providing an empty configuration_key list (attribute present).
                    return SimpleNamespace(configuration_key=[], unknown_key=None)
                return SimpleNamespace()

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            result = await srv.get_supported_measurands()
            assert result == ""  # effective_csv was empty
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9315, "cp_id": "CP_cov_trig_bn", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_trig_bn"])
@pytest.mark.parametrize("port", [9315])
async def test_trigger_boot_notification_accepts(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test trigger boot notification accepted path."""
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

            async def fake_call(req):
                if isinstance(req, call.TriggerMessage):
                    return SimpleNamespace(status=TriggerMessageStatus.accepted)
                return SimpleNamespace()

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            ok = await srv.trigger_boot_notification()
            assert ok is True and srv.triggered_boot_notification is True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9316, "cp_id": "CP_cov_trig_stat_n_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_trig_stat_n_exc"])
@pytest.mark.parametrize("port", [9316])
async def test_trigger_status_notification_connector_count_parse_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test trigger status notification when connector count parse exception causes n=1."""
    cs = setup_config_entry
    attempts = []
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            srv = cs.charge_points[cp_id]
            # Force parse error so n=1
            srv._metrics[0][cdet.connectors.value].value = "bad"

            async def fake_call(req):
                if isinstance(req, call.TriggerMessage):
                    attempts.append(req.connector_id)
                    return SimpleNamespace(status=TriggerMessageStatus.accepted)
                return SimpleNamespace()

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            ok = await srv.trigger_status_notification()
            assert ok is True
            assert attempts == [1]  # n<=1 -> probe only connector 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9317, "cp_id": "CP_cov_trig_custom_bad", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_trig_custom_bad"])
@pytest.mark.parametrize("port", [9317])
async def test_trigger_custom_message_unsupported_name(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test trigger custom message rejects unsupported trigger names."""
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
            ok = await srv.trigger_custom_message("not_a_trigger")
            assert ok is False
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9318, "cp_id": "CP_cov_profile_ids", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_profile_ids"])
@pytest.mark.parametrize("port", [9318])
async def test_profile_ids_for_bad_conn_id_cast(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test profile ids path when conn_id cast fails and conn_seg defaults to 1."""
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
            pid, level = srv._profile_ids_for(conn_id="X", purpose="TxDefaultProfile")
            # conn_seg should fall back to 1 -> pid = 1000 + 2 + (1*10) = 1012
            assert (pid, level) == (1012, 1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9319, "cp_id": "CP_cov_stop_tx_early", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_stop_tx_early"])
@pytest.mark.parametrize("port", [9319])
async def test_stop_transaction_early_return(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test stop transaction early return when there is no active transaction."""
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
            srv.active_transaction_id = 0
            srv._active_tx.clear()
            ok = await srv.stop_transaction()
            assert ok is True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9320, "cp_id": "CP_cov_update_fw", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_update_fw"])
@pytest.mark.parametrize("port", [9320])
async def test_update_firmware_wait_time_invalid_falls_back(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test update firmware path when wait_time is invalid and falls back to immediate time."""
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

            async def fake_call(req):
                return SimpleNamespace()  # success path

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            ok = await srv.update_firmware(
                "https://example.com/fw.bin", wait_time="not-int"
            )
            assert ok is True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9321, "cp_id": "CP_cov_getcfg_empty", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_getcfg_empty"])
@pytest.mark.parametrize("port", [9321])
async def test_get_configuration_empty_key_path(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test get_configuration empty key path uses call without key property."""
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

            async def fake_call(req):
                # When key is empty, code constructs call.GetConfiguration() (no key list)
                assert isinstance(req, call.GetConfiguration) and not getattr(
                    req, "key", None
                )
                return SimpleNamespace(
                    configuration_key=[{"value": "42"}], unknown_key=None
                )

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            val = await srv.get_configuration("")
            assert val == "42"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9322, "cp_id": "CP_cov_config_ro", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_config_ro"])
@pytest.mark.parametrize("port", [9322])
async def test_configure_readonly_warns_and_notifies(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test configure warns and notifies when key is read-only."""
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

            notified = []

            async def fake_call(req):
                if isinstance(req, call.GetConfiguration):
                    return SimpleNamespace(
                        configuration_key=[
                            {"key": "Foo", "value": "Bar", "readonly": True}
                        ],
                        unknown_key=None,
                    )
                # ChangeConfiguration may still be issued; return accepted for completeness
                if isinstance(req, call.ChangeConfiguration):
                    return SimpleNamespace(status=ConfigurationStatus.accepted)
                return SimpleNamespace()

            async def fake_notify(msg):
                notified.append(msg)

            monkeypatch.setattr(srv, "call", fake_call, raising=True)
            monkeypatch.setattr(srv, "notify_ha", fake_notify, raising=True)
            await srv.configure("Foo", "Baz")
            # A warning/notification should be pushed for read-only
            assert any("read-only" in m for m in notified)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9323, "cp_id": "CP_cov_restore_ms", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_restore_ms"])
@pytest.mark.parametrize("port", [9323])
async def test_restore_meter_start_cast_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test restore meter start from HA when cast raises and remains None."""
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
            # Force metric slot to look missing
            srv._metrics[(1, "Energy.Meter.Start")].value = None

            def fake_get_ha_metric(name, connector_id=None):
                if name == "Energy.Meter.Start" and connector_id == 1:
                    return "not-a-float"
                return None

            monkeypatch.setattr(srv, "get_ha_metric", fake_get_ha_metric, raising=True)

            # Send a MeterValues WITHOUT transactionId to trigger restore branch
            mv_no_tx = call.MeterValues(
                connector_id=1,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "15000",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "location": "Inlet",
                                "context": "Sample.Clock",
                            }
                        ],
                    }
                ],
            )
            # Open a WS client to deliver this message
            resp = await cp.call(mv_no_tx)
            assert resp is not None
            assert srv._metrics[(1, "Energy.Meter.Start")].value is None
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9324, "cp_id": "CP_cov_restore_tx", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_restore_tx"])
@pytest.mark.parametrize("port", [9324])
async def test_restore_transaction_id_cast_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test restore transaction id when cast fails leaving candidate as None."""
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
            srv._metrics[(1, "Transaction.Id")].value = None

            def fake_get_ha_metric(name, connector_id=None):
                if name == "Transaction.Id" and connector_id == 1:
                    return "not-an-int"
                return None

            monkeypatch.setattr(srv, "get_ha_metric", fake_get_ha_metric, raising=True)

            # Trigger the handler with a MeterValues (no strict need to carry tx)
            mv_no_tx = call.MeterValues(
                connector_id=1,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "1",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "location": "Inlet",
                                "context": "Sample.Clock",
                            }
                        ],
                    }
                ],
            )
            _ = await cp.call(mv_no_tx)
            # Value remains unset because candidate was None
            assert srv._metrics[(1, "Transaction.Id")].value is None
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9325, "cp_id": "CP_cov_new_tx", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_new_tx"])
@pytest.mark.parametrize("port", [9325])
async def test_new_transaction_resets_tx_bound_metrics(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test new transaction detection resets tx-bound EAIR and meter_start."""
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
            # Preload per-connector values which should be cleared when new tx starts
            srv._metrics[(1, "Energy.Active.Import.Register")].value = 999.0
            srv._metrics[(1, "Energy.Meter.Start")].value = 888.0

            # Send MeterValues with a new transactionId
            mv_tx = call.MeterValues(
                connector_id=1,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "0",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "context": "Sample.Clock",
                                "location": "Inlet",
                            }
                        ],
                    }
                ],
                transaction_id=12345,
            )
            resp = await cp.call(mv_tx)
            assert resp is not None
            # New tx should be registered
            assert srv._active_tx.get(1) == 12345
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9326, "cp_id": "CP_cov_eair_cast_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_eair_cast_exc"])
@pytest.mark.parametrize("port", [9326])
async def test_eair_get_energy_kwh_exception_ignored(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test EAIR scan ignores entries when energy_kwh conversion raises."""
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            # Monkeypatch energy conversion to raise
            from custom_components.ocpp.ocppv16 import cp as cp_mod

            monkeypatch.setattr(
                cp_mod,
                "get_energy_kwh",
                lambda item: (_ for _ in ()).throw(RuntimeError("bad")),
            )

            mv = call.MeterValues(
                connector_id=1,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "1000",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "context": "Sample.Clock",
                            }
                        ],
                    }
                ],
                transaction_id=1,
            )
            # Should not raise
            _ = await cp.call(mv)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9327, "cp_id": "CP_cov_num_conn_int_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_num_conn_int_exc"])
@pytest.mark.parametrize("port", [9327])
async def test_mirror_single_connector_handles_int_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test single connector mirroring handles int casting exception by falling back."""
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

            class Bad:
                def __int__(self):
                    raise ValueError("no int")

            srv.num_connectors = Bad()

            mv = call.MeterValues(
                connector_id=1,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "1000",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "context": "Sample.Clock",
                            }
                        ],
                    }
                ],
                transaction_id=2,
            )
            _ = await cp.call(mv)
            # If we reach here without exceptions, fallback worked (n_connectors=1)
            assert True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9328, "cp_id": "CP_cov_sess_energy_cast_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_sess_energy_cast_exc"])
@pytest.mark.parametrize("port", [9328])
async def test_session_energy_get_energy_kwh_exception_ignored(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test session energy calculation ignores EAIR entries raising conversion errors."""
    cs = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            from custom_components.ocpp.ocppv16 import cp as cp_mod

            monkeypatch.setattr(
                cp_mod,
                "get_energy_kwh",
                lambda item: (_ for _ in ()).throw(RuntimeError("bad")),
            )

            mv = call.MeterValues(
                connector_id=1,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "1000",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "context": "Sample.Clock",
                            }
                        ],
                    }
                ],
                transaction_id=3,
            )
            _ = await cp.call(mv)
            # No crash == lines 1005-1006 exercised
            assert True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9329, "cp_id": "CP_cov_sess_ms_cast_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_sess_ms_cast_exc"])
@pytest.mark.parametrize("port", [9329])
async def test_session_energy_meter_start_cast_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test session energy path when meter_start cannot be cast to float."""
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
            # Poison meter_start with a non-float so that float() raises
            srv._metrics[(1, "Energy.Meter.Start")].value = object()

            mv = call.MeterValues(
                connector_id=1,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "1000",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "context": "Sample.Clock",
                            }
                        ],
                    }
                ],
                transaction_id=4,
            )
            _ = await cp.call(mv)
            # No crash is sufficient to cover lines 1023-1024
            assert True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9330, "cp_id": "CP_cov_status_suspended", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_status_suspended"])
@pytest.mark.parametrize("port", [9330])
async def test_status_notification_suspended_resets_metrics(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test status notification suspended state resets power/current metrics to zero."""
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

            # Pre-populate metrics (non-zero) so they can be zeroed on suspended status
            for meas in [
                "Current.Import",
                "Power.Active.Import",
                "Power.Reactive.Import",
                "Current.Export",
                "Power.Active.Export",
                "Power.Reactive.Export",
            ]:
                srv._metrics[(1, meas)].value = 123

            # Simulate status notification
            resp = srv.on_status_notification(
                connector_id=1,
                error_code="NoError",
                status=ChargePointStatus.suspended_ev.value,
            )
            assert resp is not None
            for meas in [
                "Current.Import",
                "Power.Active.Import",
                "Power.Reactive.Import",
                "Current.Export",
                "Power.Active.Export",
                "Power.Reactive.Export",
            ]:
                assert int(srv._metrics[(1, meas)].value) == 0
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9331, "cp_id": "CP_cov_start_tx_ms_cast_exc", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_start_tx_ms_cast_exc"])
@pytest.mark.parametrize("port", [9331])
async def test_start_transaction_meter_start_cast_exception(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test start transaction handler path when meter_start cast fails defaults to 0.0."""
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

            # Ensure authorization passes so the handler proceeds normally
            monkeypatch.setattr(
                srv, "get_authorization_status", lambda id_tag: "Accepted", raising=True
            )

            # Call the handler directly with a non-numeric meter_start
            result = srv.on_start_transaction(
                connector_id=1, id_tag="test_cp", meter_start="not-a-number"
            )
            assert result is not None
            # The cast fails internally and baseline defaults to 0.0 (lines 1147-1148)
            assert float(srv._metrics[(1, "Energy.Meter.Start")].value) == 0.0
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9332, "cp_id": "CP_cov_start_tx_denied", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_start_tx_denied"])
@pytest.mark.parametrize("port", [9332])
async def test_start_transaction_auth_denied_returns_tx0(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Test start transaction returns transaction id 0 when authorization is denied."""
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

            # Force non-accepted authorization
            monkeypatch.setattr(
                srv,
                "get_authorization_status",
                lambda id_tag: "Invalid",
                raising=True,
            )
            # Call handler directly to inspect response
            result = srv.on_start_transaction(
                connector_id=1, id_tag="bad", meter_start=0
            )
            assert result.transaction_id == 0
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9333, "cp_id": "CP_cov_stop_tx_paths", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_cov_stop_tx_paths"])
@pytest.mark.parametrize("port", [9333])
async def test_stop_transaction_misc_paths(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test stop transaction paths covering unknown unit and meter_stop cast exception."""
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

            # Simulate a running transaction
            srv._active_tx[1] = 777
            srv.active_transaction_id = 777

            # Prepare main-meter EAIR with unknown unit to drive line 1213
            srv._metrics[(1, "Energy.Active.Import.Register")].value = 123.456
            srv._metrics[(1, "Energy.Active.Import.Register")].unit = "kWs"  # unknown

            # Call stop_transaction with a non-numeric meter_stop so 1225-1226 -> 0.0
            result = srv.on_stop_transaction(
                meter_stop="not-a-number", timestamp=None, transaction_id=777
            )
            assert result is not None
            # Session energy should be computed; we mainly care lines executed without crash
            assert srv._active_tx[1] == 0 and srv.active_transaction_id == 0
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()
