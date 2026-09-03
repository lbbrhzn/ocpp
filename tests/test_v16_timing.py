"""Per-charge-point OCPP 1.6 connection timing configuration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.ocpp.chargepoint import ChargePoint as BaseChargePoint
from custom_components.ocpp.config_flow import STEP_USER_CP_DATA_SCHEMA
from custom_components.ocpp.const import (
    CONF_CHARGER_WEBSOCKET_PING_INTERVAL,
    CONF_HEARTBEAT_INTERVAL,
    DEFAULT_HEARTBEAT_INTERVAL,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import ConfigurationKey
from custom_components.ocpp.ocppv16 import ChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import ConfigurationStatus


def _legacy_settings(**overrides):
    values = {
        "cpid": "test",
        "max_current": 32,
        "idle_interval": 900,
        "meter_interval": 60,
        "monitored_variables": "Energy.Active.Import.Register",
        "monitored_variables_autoconfig": True,
        "skip_schema_validation": False,
        "force_smart_charging": False,
    }
    values.update(overrides)
    return ChargerSystemSettings(**values)


def _server(settings, connection=None):
    server = ChargePoint.__new__(ChargePoint)
    server.id = "CP_1"
    server.settings = settings
    server._connection = connection if connection is not None else object()
    server._timing_connection_lock = asyncio.Lock()
    server._requires_reboot = False
    server.notify_ha = AsyncMock()
    return server


def _configuration(key, value, *, readonly=False):
    return call_result.GetConfiguration(
        configuration_key=[{"key": key, "readonly": readonly, "value": value}],
        unknown_key=[],
    )


def test_legacy_charge_point_settings_preserve_timing_defaults():
    """Old entries load without migration or writes and retain interval 3600."""
    settings = _legacy_settings()

    assert settings.heartbeat_interval is None
    assert DEFAULT_HEARTBEAT_INTERVAL == 3600
    assert settings.charger_websocket_ping_interval is None


def test_charge_point_form_exposes_distinct_timing_controls():
    """Charger timing must not be confused with central-system websocket ping."""
    fields = {str(key): key for key in STEP_USER_CP_DATA_SCHEMA.schema}

    assert CONF_HEARTBEAT_INTERVAL in fields
    assert fields[CONF_HEARTBEAT_INTERVAL].default() == 3600
    assert CONF_CHARGER_WEBSOCKET_PING_INTERVAL in fields
    assert fields[CONF_CHARGER_WEBSOCKET_PING_INTERVAL].default() is None


def test_boot_notification_uses_backward_compatible_default():
    """Keep the historical BootNotification interval for legacy entries."""
    server = _server(_legacy_settings())
    server.hass = SimpleNamespace(async_create_task=lambda coroutine: coroutine.close())
    server._register_boot_notification = lambda: None
    server.received_boot_notification = False

    response = server.on_boot_notification()

    assert response.interval == 3600


def test_boot_notification_uses_selected_charge_point_interval():
    """Advertise the selected heartbeat interval in BootNotification.conf."""
    server = _server(_legacy_settings(heartbeat_interval=60))
    server.hass = SimpleNamespace(async_create_task=lambda coroutine: coroutine.close())
    server._register_boot_notification = lambda: None
    server.received_boot_notification = False

    response = server.on_boot_notification()

    assert response.interval == 60


async def test_selected_timing_is_changed_and_read_back():
    """60 / 60 / 10 changes only mismatches and verifies all three values."""
    settings = _legacy_settings(
        heartbeat_interval=60,
        charger_websocket_ping_interval=60,
        meter_interval=10,
    )
    connection = object()
    server = _server(settings, connection)
    state = {
        ConfigurationKey.heartbeat_interval: "3600",
        ConfigurationKey.web_socket_ping_interval: "30",
        ConfigurationKey.meter_value_sample_interval: "60",
    }
    gets = []
    changes = []

    async def fake_call(request):
        assert server._connection is connection
        if isinstance(request, call.GetConfiguration):
            key = request.key[0]
            gets.append(key)
            return _configuration(key, state[key])
        if isinstance(request, call.ChangeConfiguration):
            changes.append((request.key, request.value))
            state[request.key] = request.value
            return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
        raise AssertionError(type(request))

    server.call = fake_call

    await server.configure_connection_timing(connection)

    assert changes == [
        (ConfigurationKey.heartbeat_interval, "60"),
        (ConfigurationKey.web_socket_ping_interval, "60"),
        (ConfigurationKey.meter_value_sample_interval, "10"),
    ]
    assert gets.count(ConfigurationKey.heartbeat_interval) == 2
    assert gets.count(ConfigurationKey.web_socket_ping_interval) == 2
    assert gets.count(ConfigurationKey.meter_value_sample_interval) == 2


async def test_defaults_skip_charger_ping_and_matching_writes():
    """Default None never probes charger ping; matching values are read-only checks."""
    settings = _legacy_settings(heartbeat_interval=3600, meter_interval=60)
    connection = object()
    server = _server(settings, connection)
    gets = []

    async def fake_call(request):
        if isinstance(request, call.GetConfiguration):
            key = request.key[0]
            gets.append(key)
            values = {
                ConfigurationKey.heartbeat_interval: "3600",
                ConfigurationKey.meter_value_sample_interval: "60",
            }
            return _configuration(key, values[key])
        raise AssertionError("matching values must not be written")

    server.call = fake_call

    await server.configure_connection_timing(connection)

    assert gets == [
        ConfigurationKey.heartbeat_interval,
        ConfigurationKey.meter_value_sample_interval,
    ]


async def test_legacy_entry_does_not_persistently_write_heartbeat_setting():
    """A missing legacy option affects BootNotification only, not charger storage."""
    settings = _legacy_settings()
    connection = object()
    server = _server(settings, connection)
    gets = []

    async def fake_call(request):
        if isinstance(request, call.GetConfiguration):
            key = request.key[0]
            gets.append(key)
            assert key == ConfigurationKey.meter_value_sample_interval
            return _configuration(key, "60")
        raise AssertionError("legacy timing must not be written")

    server.call = fake_call

    await server.configure_connection_timing(connection)

    assert gets == [ConfigurationKey.meter_value_sample_interval]


async def test_timing_failures_are_non_fatal_and_continue(caplog):
    """Unknown, read-only and rejected keys remain observable without aborting setup."""
    settings = _legacy_settings(
        heartbeat_interval=60,
        charger_websocket_ping_interval=60,
        meter_interval=10,
    )
    connection = object()
    server = _server(settings, connection)
    queried = []
    changed = []

    async def fake_call(request):
        if isinstance(request, call.GetConfiguration):
            key = request.key[0]
            queried.append(key)
            if key == ConfigurationKey.heartbeat_interval:
                return call_result.GetConfiguration(
                    configuration_key=[], unknown_key=[key]
                )
            if key == ConfigurationKey.web_socket_ping_interval:
                return _configuration(key, "30", readonly=True)
            return _configuration(key, "60")
        if isinstance(request, call.ChangeConfiguration):
            changed.append(request.key)
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        raise AssertionError(type(request))

    server.call = fake_call

    await server.configure_connection_timing(connection)

    assert queried == [
        ConfigurationKey.heartbeat_interval,
        ConfigurationKey.web_socket_ping_interval,
        ConfigurationKey.meter_value_sample_interval,
    ]
    assert changed == [ConfigurationKey.meter_value_sample_interval]
    assert server.notify_ha.await_count == 0
    assert "unknown" in caplog.text.lower()
    assert "read-only" in caplog.text.lower()
    assert "rejected" in caplog.text.lower()


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError("dropped")])
async def test_timeout_or_disconnect_does_not_abort_remaining_timing_keys(failure):
    """A failed key costs only that key and never tears down timing setup."""
    settings = _legacy_settings(
        heartbeat_interval=60,
        charger_websocket_ping_interval=60,
        meter_interval=10,
    )
    connection = object()
    server = _server(settings, connection)
    queried = []

    async def fake_call(request):
        if isinstance(request, call.GetConfiguration):
            key = request.key[0]
            queried.append(key)
            if key == ConfigurationKey.heartbeat_interval:
                raise failure
            return _configuration(
                key,
                str(
                    getattr(
                        settings,
                        {
                            ConfigurationKey.web_socket_ping_interval: "charger_websocket_ping_interval",
                            ConfigurationKey.meter_value_sample_interval: "meter_interval",
                        }[key],
                    )
                ),
            )
        raise AssertionError(type(request))

    server.call = fake_call

    await server.configure_connection_timing(connection)

    assert queried == [
        ConfigurationKey.heartbeat_interval,
        ConfigurationKey.web_socket_ping_interval,
        ConfigurationKey.meter_value_sample_interval,
    ]


async def test_reboot_required_is_recorded_and_read_back(caplog):
    """RebootRequired is non-fatal, observable, and still verifies readback."""
    settings = _legacy_settings(heartbeat_interval=60)
    connection = object()
    server = _server(settings, connection)
    reads = iter(["300", "60"])
    requests = []

    async def fake_call(request):
        requests.append(request)
        if isinstance(request, call.GetConfiguration):
            return _configuration(request.key[0], next(reads))
        if isinstance(request, call.ChangeConfiguration):
            return call_result.ChangeConfiguration(
                status=ConfigurationStatus.reboot_required
            )
        raise AssertionError(request)

    server.call = fake_call

    with caplog.at_level("WARNING"):
        await server._configure_timing_key(
            ConfigurationKey.heartbeat_interval, 60, connection
        )

    assert server._requires_reboot is True
    assert sum(isinstance(item, call.GetConfiguration) for item in requests) == 2
    assert "requires a charger reboot" in caplog.text


async def test_stale_timing_setup_stops_before_writing_to_reconnected_session():
    """A read returning after reconnect cannot write or continue on the new socket."""
    settings = _legacy_settings(
        heartbeat_interval=60,
        charger_websocket_ping_interval=60,
        meter_interval=10,
    )
    old_connection = object()
    server = _server(settings, old_connection)
    calls = []

    async def fake_call(request):
        calls.append(request)
        server._connection = object()
        return _configuration(ConfigurationKey.heartbeat_interval, "3600")

    server.call = fake_call

    await server.configure_connection_timing(old_connection)

    assert len(calls) == 1
    assert isinstance(calls[0], call.GetConfiguration)


async def test_stale_timing_setup_does_not_apply_idle_setting_to_new_session():
    """The rest of standard configuration stops when timing detects replacement."""
    server = _server(_legacy_settings())
    server.configure_connection_timing = AsyncMock(return_value=False)
    server.configure = AsyncMock()

    await server.set_standard_configuration()

    server.configure.assert_not_awaited()


async def test_idle_interval_send_cannot_cross_into_replacement_session():
    """ClockAlignedDataInterval remains fenced after timing setup succeeds."""
    old_connection = object()
    new_connection = object()
    server = _server(_legacy_settings(), old_connection)
    server.configure_connection_timing = AsyncMock(return_value=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    sent_on = []

    async def paused_configure(*args):
        entered.set()
        await release.wait()
        sent_on.append(server._connection)

    async def replace_connection():
        async with server._timing_connection_lock:
            server._connection = new_connection

    server.configure = paused_configure
    setup = asyncio.create_task(server.set_standard_configuration())
    await entered.wait()
    replacement = asyncio.create_task(replace_connection())
    await asyncio.sleep(0)

    try:
        assert server._connection is old_connection
    finally:
        release.set()
        await asyncio.gather(setup, replacement, return_exceptions=True)

    assert sent_on == [old_connection]
    assert server._connection is new_connection


async def test_post_connect_scheduling_is_coalesced_per_connection():
    """Repeated boot notifications create one setup task for a session."""
    server = BaseChargePoint.__new__(BaseChargePoint)
    server._post_connect_task = None
    server._post_connect_connection = None
    server.post_connect_success = False
    server._connection = object()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    server.hass = SimpleNamespace(async_create_task=asyncio.create_task)

    async def fake_post_connect(connection=None):
        calls.append(connection)
        entered.set()
        await release.wait()

    server.post_connect = fake_post_connect

    first = server._schedule_post_connect()
    second = server._schedule_post_connect()
    await entered.wait()

    assert first is second
    assert calls == [server._connection]

    release.set()
    await first


async def test_replacement_connections_coalesce_behind_one_setup_runner():
    """Repeated reconnects retain one runner and process only the latest session."""
    server = BaseChargePoint.__new__(BaseChargePoint)
    server._post_connect_task = None
    server._post_connect_connection = None
    server.post_connect_success = False
    first_connection = object()
    server._connection = first_connection
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    server.hass = SimpleNamespace(async_create_task=asyncio.create_task)

    async def cancellation_resistant_post_connect(connection=None):
        calls.append(connection)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    server.post_connect = cancellation_resistant_post_connect

    tasks = [server._schedule_post_connect()]
    await entered.wait()
    latest_connection = first_connection
    for _ in range(4):
        latest_connection = object()
        server._connection = latest_connection
        tasks.append(server._schedule_post_connect())
    await asyncio.sleep(0)

    try:
        assert len(set(tasks)) == 1
        assert calls == [first_connection]
    finally:
        release.set()
        await asyncio.gather(*set(tasks), return_exceptions=True)

    assert calls == [first_connection, latest_connection]


async def test_reconnect_cannot_replace_connection_during_timing_call():
    """The session remains immutable from timing pre-check through send/response."""
    from custom_components.ocpp.enums import HAChargerStatuses

    old_connection = SimpleNamespace(state=None)
    new_connection = SimpleNamespace(state=None)
    server = _server(_legacy_settings(heartbeat_interval=60), old_connection)
    server._ocpp_version = "1.6"
    server.status = "ok"
    server.post_connect_success = True
    server.received_boot_notification = True
    server.triggered_boot_notification = True
    server._timing_connection_lock = asyncio.Lock()
    server._metrics = {
        (0, HAChargerStatuses.reconnects): SimpleNamespace(value=0),
    }
    entered_call = asyncio.Event()
    release_call = asyncio.Event()
    sent_on = []
    server.stop = AsyncMock()

    async def close_coroutines(tasks):
        for coroutine in tasks:
            coroutine.close()

    async def paused_call(request):
        entered_call.set()
        await release_call.wait()
        sent_on.append(server._connection)
        return SimpleNamespace()

    server.run = close_coroutines
    server.call = paused_call

    timing = asyncio.create_task(
        server._timing_call(
            call.GetConfiguration(key=[ConfigurationKey.heartbeat_interval]),
            old_connection,
        )
    )
    await entered_call.wait()
    reconnect = asyncio.create_task(server.reconnect(new_connection))
    await asyncio.sleep(0)

    try:
        assert server._connection is old_connection
    finally:
        release_call.set()
        await asyncio.gather(timing, reconnect, return_exceptions=True)

    assert sent_on == [old_connection]
    assert server._connection is new_connection


async def test_post_connect_mutation_helper_fences_replacement_session():
    """Optional post-connect mutations cannot run across a reconnect."""
    old_connection = object()
    new_connection = object()
    server = BaseChargePoint.__new__(BaseChargePoint)
    server._connection = old_connection
    server._timing_connection_lock = asyncio.Lock()
    entered = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def mutation():
        entered.set()
        await release.wait()
        observed.append(server._connection)

    async def replace_connection():
        async with server._timing_connection_lock:
            server._connection = new_connection

    mutation_task = asyncio.create_task(
        server._run_on_connection(old_connection, mutation)
    )
    await entered.wait()
    replacement = asyncio.create_task(replace_connection())
    await asyncio.sleep(0)

    try:
        assert server._connection is old_connection
    finally:
        release.set()
        await asyncio.gather(mutation_task, replacement, return_exceptions=True)

    assert observed == [old_connection]
    assert server._connection is new_connection


async def test_initial_post_connect_requests_are_fenced_to_original_session():
    """Feature, connector, and heartbeat discovery cannot cross sessions."""
    old_connection = object()
    new_connection = object()
    server = BaseChargePoint.__new__(BaseChargePoint)
    server._connection = old_connection
    server._timing_connection_lock = asyncio.Lock()
    entered = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def fetch_features():
        entered.set()
        await release.wait()
        observed.append(server._connection)

    async def replace_connection():
        async with server._timing_connection_lock:
            server._connection = new_connection

    server.fetch_supported_features = fetch_features
    server.get_number_of_connectors = AsyncMock(return_value=1)
    server.get_heartbeat_interval = AsyncMock()
    discovery = asyncio.create_task(
        server._fetch_post_connect_inventory(old_connection)
    )
    await entered.wait()
    replacement = asyncio.create_task(replace_connection())
    await asyncio.sleep(0)

    try:
        assert server._connection is old_connection
    finally:
        release.set()
        await asyncio.gather(discovery, replacement, return_exceptions=True)

    assert observed == [old_connection]
    assert server._connection is new_connection
    server.get_number_of_connectors.assert_not_awaited()
    server.get_heartbeat_interval.assert_not_awaited()


async def test_measurand_change_cannot_cross_into_replacement_session():
    """MeterValuesSampledData writes remain on the post-connect session."""
    old_connection = object()
    new_connection = object()
    desired = "Energy.Active.Import.Register"
    settings = _legacy_settings(
        monitored_variables=desired,
        monitored_variables_autoconfig=True,
    )
    server = _server(settings, old_connection)
    server.get_configuration = AsyncMock(return_value=desired)
    server.configure = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()
    sent_on = []

    async def paused_call(request):
        assert isinstance(request, call.ChangeConfiguration)
        entered.set()
        await release.wait()
        sent_on.append(server._connection)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    async def replace_connection():
        async with server._timing_connection_lock:
            server._connection = new_connection

    server.call = paused_call
    setup = asyncio.create_task(server.get_supported_measurands())
    await entered.wait()
    replacement = asyncio.create_task(replace_connection())
    await asyncio.sleep(0)

    try:
        assert server._connection is old_connection
    finally:
        release.set()
        await asyncio.gather(setup, replacement, return_exceptions=True)

    assert sent_on == [old_connection]
    assert server._connection is new_connection


@pytest.mark.parametrize(
    ("ocpp_version", "expected_success"), [("1.6", False), ("2.0.1", True)]
)
async def test_reconnect_rearms_timing_only_for_ocpp_16(ocpp_version, expected_success):
    """OCPP 1.6 re-verifies timing without changing 2.0.1 reconnect behavior."""
    from custom_components.ocpp.enums import HAChargerStatuses

    server = BaseChargePoint.__new__(BaseChargePoint)
    server.id = "CP_1"
    server._ocpp_version = ocpp_version
    server._connection = SimpleNamespace(state=None)
    server.status = "ok"
    server.post_connect_success = True
    server.received_boot_notification = True
    server.triggered_boot_notification = True
    server._timing_connection_lock = asyncio.Lock()
    server._metrics = {
        (0, HAChargerStatuses.reconnects): SimpleNamespace(value=0),
    }
    server.stop = AsyncMock()

    async def close_coroutines(tasks):
        for coroutine in tasks:
            coroutine.close()

    server.run = close_coroutines

    await server.reconnect(SimpleNamespace(state=None))

    assert server.post_connect_success is expected_success
