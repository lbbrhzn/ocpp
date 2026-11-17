"""Secondary OCPP reporting regressions with persistent SQLite and loopback peers."""

import asyncio
from contextlib import asynccontextmanager
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from custom_components.ocpp.reporting import (
    ReportingJournal,
    ReportingSettings,
    ReportingTransport,
    SecondaryReporter,
)


BOOT = {"chargePointVendor": "Example", "chargePointModel": "Model 1"}
TIME = "2026-02-03T04:05:06Z"


def _call(number, action, body):
    return [2, f"local-{number}", action, body]


def _accepted(frame, transaction_id=41):
    if frame[2] == "BootNotification":
        body = {"status": "Accepted", "currentTime": TIME, "interval": 3600}
    elif frame[2] == "StartTransaction":
        body = {"transactionId": transaction_id, "idTagInfo": {"status": "Accepted"}}
    else:
        body = {}
    return [3, frame[1], body]


def _start(number, connector, *, timestamp=TIME):
    return _call(number, "StartTransaction", {
        "connectorId": connector, "idTag": "actual-tag", "meterStart": 1275,
        "timestamp": timestamp,
    })


def _meter(number, connector, transaction_id):
    return _call(number, "MeterValues", {
        "connectorId": connector, "transactionId": transaction_id,
        "meterValue": [{"timestamp": "2026-02-03T04:10:07Z", "sampledValue": [
            {"value": "1.275", "measurand": "Energy.Active.Import.Register", "unit": "kWh"},
            {"value": "736", "measurand": "Power.Active.Import", "unit": "W"},
        ]}],
    })


def _stop(number, transaction_id):
    return _call(number, "StopTransaction", {
        "transactionId": transaction_id, "meterStop": 1875,
        "timestamp": "2026-02-03T04:22:00Z", "reason": "Local",
    })


@pytest.fixture
def journal(tmp_path):
    """Exercise the on-disk journal without replacing its SQLite connection."""
    path = tmp_path / "reports.sqlite3"
    db = ReportingJournal(path, "destination-one", 10)
    try:
        yield db, path
    finally:
        db.close()


def test_ordered_reports_keep_original_measurements_and_rewrite_only_transaction(journal):
    """The provider sees original charger data and its own, not HA's, transaction ID."""
    db, path = journal
    start, meter, stop = _start(1, 2), _meter(2, 2, 41), _stop(3, 41)
    for frame in (start, meter, stop):
        assert db.record(frame, _accepted(frame))["integrity_error"] is None
    assert db.summary()["pending"] == 3
    db.close()
    db = ReportingJournal(path, "destination-one", 10)
    try:
        first = db.next_report()
        assert first["action"] == "StartTransaction"
        assert first["body"] == start[3]
        db.acknowledge(first, [3, first["message_id"], {
            "transactionId": 88001, "idTagInfo": {"status": "Accepted"},
        }])
        second = db.next_report()
        assert second["action"] == "MeterValues"
        assert second["body"] == {**meter[3], "transactionId": 88001}
        db.acknowledge(second, [3, second["message_id"], {}])
        third = db.next_report()
        assert third["action"] == "StopTransaction"
        assert third["body"] == {**stop[3], "transactionId": 88001}
        db.acknowledge(third, [3, third["message_id"], {}])
        assert db.summary()["pending"] == 0
    finally:
        db.close()


def test_interrupted_delivery_blocks_replay_until_provider_confirmed_reconciliation(journal):
    """A lost ACK cannot silently duplicate a remote start transaction."""
    db, path = journal
    start, meter = _start(1, 1), _meter(2, 1, 41)
    db.record(start, _accepted(start))
    db.record(meter, _accepted(meter))
    sent = db.next_report()
    db.mark(sent["id"], "inflight")
    db.close()
    db = ReportingJournal(path, "destination-one", 10)
    try:
        blocked = db.summary()["blocked_event"]
        assert blocked["id"] == sent["id"]
        assert blocked["state"] == "uncertain"
        assert db.next_report() is None
        with pytest.raises(ValueError, match="provider_transaction_id_required"):
            db.reconcile(sent["id"], "received")
        assert db.next_report() is None
        db.reconcile(sent["id"], "received", 55073)
        report = db.next_report()
        assert report["action"] == "MeterValues"
        assert report["body"]["transactionId"] == 55073
        assert report["body"]["meterValue"] == meter[3]["meterValue"]
    finally:
        db.close()


def test_different_chargers_and_connectors_keep_provider_transactions_isolated(tmp_path):
    """Distinct local sessions survive restart; two chargers may use the same local ID."""
    a_path, b_path = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"
    first = ReportingJournal(a_path, "provider-A", 10)
    other = ReportingJournal(b_path, "provider-B", 10)
    try:
        old = _start(1, 1)
        first.record(old, _accepted(old, 17))
        first_event = first.next_report()
        first.acknowledge(first_event, [3, first_event["message_id"], {
            "transactionId": 8001, "idTagInfo": {"status": "Accepted"},
        }])
        old_stop = _stop(2, 17)
        first.record(old_stop, _accepted(old_stop))
        stop_event = first.next_report()
        assert stop_event["body"]["transactionId"] == 8001
        first.acknowledge(stop_event, [3, stop_event["message_id"], {}])
        first.close()
        first = ReportingJournal(a_path, "provider-A", 10)
        renewed = _start(3, 2, timestamp="2026-02-04T04:05:06Z")
        first.record(renewed, _accepted(renewed, 18))
        other.record(_start(4, 1), _accepted(_start(4, 1), 17))
        renewed_event = first.next_report()
        other_event = other.next_report()
        first.acknowledge(renewed_event, [3, renewed_event["message_id"], {
            "transactionId": 8002, "idTagInfo": {"status": "Accepted"},
        }])
        other.acknowledge(other_event, [3, other_event["message_id"], {
            "transactionId": 9001, "idTagInfo": {"status": "Accepted"},
        }])
        first.record(_meter(5, 2, 18), _accepted(_meter(5, 2, 18)))
        other.record(_meter(6, 1, 17), _accepted(_meter(6, 1, 17)))
        assert first.summary()["integrity_error"] is None
        assert first.next_report()["body"]["transactionId"] == 8002
        assert other.next_report()["body"]["transactionId"] == 9001
    finally:
        first.close()
        other.close()

def test_ambiguous_local_transaction_id_blocks_instead_of_crossing_connectors(journal):
    """A broken local authority reusing a live ID must never guess which session to bill."""
    db, _ = journal
    first, second = _start(1, 1), _start(2, 2)
    db.record(first, _accepted(first, 17))
    db.record(second, _accepted(second, 17))
    assert db.record(_meter(3, 2, 17), _accepted(_meter(3, 2, 17)))["integrity_error"] == "unmapped_local_transaction"
    assert db.summary()["pending"] == 2
    assert db.next_report() is None


def test_rejected_or_duplicate_local_messages_never_consume_queue_capacity(journal):
    """Only locally accepted distinct reports can fill a bounded durable queue."""
    db, path = journal
    db.limit = 1
    start = _start(1, 1)
    rejected = [4, start[1], "FormationViolation", "Invalid charger request", {}]
    assert db.record(start, rejected)["pending"] == 0
    assert db.record(start, _accepted(start))["pending"] == 1
    assert db.record(start, _accepted(start))["pending"] == 1
    assert db.record(_meter(2, 1, 41), _accepted(_meter(2, 1, 41)))["integrity_error"] == "queue_full"
    assert db.summary()["pending"] == 1
    assert db.next_report() is None
    db.close()
    reopened = ReportingJournal(path, "destination-one", 1)
    try:
        assert reopened.summary()["integrity_error"] == "queue_full"
        assert reopened.summary()["pending"] == 1
        assert reopened.next_report() is None
    finally:
        reopened.close()


@asynccontextmanager
async def _provider():
    connections, messages = asyncio.Queue(), asyncio.Queue()

    async def handler(ws):
        await connections.put(ws)
        async for raw in ws:
            frame = json.loads(raw)
            await messages.put(frame)
            if frame[0] == 2 and frame[2] == "BootNotification":
                await ws.send(json.dumps(_accepted(frame)))

    async with serve(handler, "127.0.0.1", 0, subprotocols=["ocpp1.6"], ping_interval=None) as server:
        yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/reports", connections, messages


@asynccontextmanager
async def _local_link(hass, provider_url, *, journal_path, entry_id="secondary-test", settings=None):
    """One real charger socket and one real provider-independent HA transport."""
    reporter = SecondaryReporter(hass, entry_id, "actual-charger", "charger", settings or ReportingSettings(provider_url))
    if journal_path is not None:
        reporter.path = journal_path
    received = asyncio.Queue()

    async def handler(ws):
        transport = ReportingTransport(ws, reporter)
        async for raw in _transport_messages(transport):
            frame = json.loads(raw)
            await received.put(frame)
            if frame[0] == 2:
                await transport.send(json.dumps(_accepted(frame)))

    reporter.start()
    try:
        async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/actual-charger", subprotocols=["ocpp1.6"]) as charger:
                async with asyncio.timeout(3):
                    while not reporter.diagnostics()["source_connected"]:
                        await asyncio.sleep(0)
                yield reporter, charger, received, port
    finally:
        await reporter.close()


async def _transport_messages(transport):
    while True:
        try:
            yield await transport.recv()
        except ConnectionClosed:
            return


async def _charger_call(charger, number, action, body):
    frame = _call(number, action, body)
    await charger.send(json.dumps(frame))
    response = json.loads(await asyncio.wait_for(charger.recv(), 3))
    assert response[0] == 3 and response[1] == frame[1]
    return response


async def _next_action(messages, action):
    async with asyncio.timeout(3):
        while True:
            frame = await messages.get()
            if frame[0] == 2 and frame[2] == action:
                return frame


async def test_local_acceptance_and_control_continue_while_provider_is_offline(hass, socket_enabled, tmp_path):
    """An unavailable provider must not become an authority over local responses."""
    async with serve(lambda ws: ws.wait_closed(), "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
    async with _local_link(hass, f"ws://127.0.0.1:{port}/offline", journal_path=tmp_path / "outbox.sqlite3") as (reporter, charger, _, _):
        await _charger_call(charger, 1, "BootNotification", BOOT)
        response = await _charger_call(charger, 2, "StartTransaction", _start(2, 1)[3])
        assert response[2]["transactionId"] == 41
        await _charger_call(charger, 3, "MeterValues", _meter(3, 1, 41)[3])
        await _charger_call(charger, 4, "StopTransaction", _stop(4, 41)[3])
        await asyncio.wait_for(reporter._queue.join(), 3)
        assert reporter.diagnostics()["pending"] == 3
        assert reporter.diagnostics()["integrity_error"] is None


async def test_provider_commands_are_rejected_but_safe_queries_reach_actual_charger(hass, socket_enabled, tmp_path):
    """The reporting socket cannot issue charging commands or secret-bearing reads."""
    async with (
        _provider() as (url, connections, messages),
        _local_link(hass, url, journal_path=tmp_path / "outbox.sqlite3") as (_, charger, local, _),
    ):
        await _charger_call(charger, 1, "BootNotification", BOOT)
        upstream = await asyncio.wait_for(connections.get(), 3)
        await _next_action(messages, "BootNotification")
        assert (await asyncio.wait_for(local.get(), 3))[2] == "BootNotification"
        for number, action, body in (
            (1, "RemoteStartTransaction", {"idTag": "provider-tag"}),
            (2, "ChangeConfiguration", {"key": "HeartbeatInterval", "value": "1"}),
            (3, "Reset", {"type": "Hard"}),
            (4, "GetConfiguration", {"key": ["AuthorizationKey"]}),
            (5, "TriggerMessage", {"requestedMessage": "SignCertificate"}),
            (8, "GetConfiguration", {}),
            (9, "GetConfiguration", {"key": []}),
        ):
            await upstream.send(json.dumps([2, f"provider-{number}", action, body]))
            rejected = await asyncio.wait_for(messages.get(), 3)
            assert rejected[:3] == [4, f"provider-{number}", "NotSupported"]
        assert local.empty(), "a rejected provider call reached the real charger"
        for number, action, body, answer in (
            (6, "GetConfiguration", {"key": ["HeartbeatInterval"]},
             {"configurationKey": [{"key": "HeartbeatInterval", "readonly": True, "value": "300"}]}),
            (7, "TriggerMessage", {"requestedMessage": "Heartbeat"}, {"status": "Accepted"}),
        ):
            await upstream.send(json.dumps([2, f"provider-{number}", action, body]))
            forwarded = json.loads(await asyncio.wait_for(charger.recv(), 3))
            assert forwarded[0] == 2 and forwarded[2:] == [action, body]
            assert forwarded[1].startswith("reporting-")
            await charger.send(json.dumps([3, forwarded[1], answer]))
            result = await asyncio.wait_for(messages.get(), 3)
            assert result == [3, f"provider-{number}", answer]


async def test_late_query_answer_stays_out_of_local_controller_response_stream(hass, monkeypatch, socket_enabled, tmp_path):
    """A timed-out query response must not be misdelivered to HA after timeout."""
    async with (
        _provider() as (url, connections, messages),
        _local_link(hass, url, journal_path=tmp_path / "outbox.sqlite3") as (_, charger, _, _),
    ):
        await _charger_call(charger, 1, "BootNotification", BOOT)
        upstream = await asyncio.wait_for(connections.get(), 3)
        await _next_action(messages, "BootNotification")
        monkeypatch.setattr("custom_components.ocpp.reporting._REQUEST_TIMEOUT", 0.05)
        await upstream.send(json.dumps([2, "provider-query", "TriggerMessage", {"requestedMessage": "Heartbeat"}]))
        forwarded = json.loads(await asyncio.wait_for(charger.recv(), 3))
        error = await asyncio.wait_for(messages.get(), 3)
        assert error[:3] == [4, "provider-query", "InternalError"]
        await charger.send(json.dumps([3, forwarded[1], {"status": "Accepted"}]))
        await _charger_call(charger, 2, "Heartbeat", {})


async def test_new_local_boot_refreshes_provider_registration(hass, socket_enabled, tmp_path):
    """The next registration reflects a real newly accepted charger boot."""
    async with (
        _provider() as (url, _, messages),
        _local_link(hass, url, journal_path=tmp_path / "outbox.sqlite3") as (reporter, charger, _, _),
    ):
        await _charger_call(charger, 1, "BootNotification", BOOT)
        first = await _next_action(messages, "BootNotification")
        assert first[3] == BOOT
        updated = {**BOOT, "firmwareVersion": "2026.11"}
        await _charger_call(charger, 2, "BootNotification", updated)
        refreshed = await _next_action(messages, "BootNotification")
        assert refreshed[3] == updated
        assert reporter.diagnostics()["integrity_error"] is None


async def test_replaced_socket_owns_queries_after_old_socket_closes(hass, socket_enabled, tmp_path):
    """A retired connection must not detach the replacement charger socket."""
    async with (
        _provider() as (url, connections, messages),
        _local_link(hass, url, journal_path=tmp_path / "outbox.sqlite3") as (reporter, old, _, port),
    ):
        await _charger_call(old, 1, "BootNotification", BOOT)
        upstream = await asyncio.wait_for(connections.get(), 3)
        await _next_action(messages, "BootNotification")
        async with connect(f"ws://127.0.0.1:{port}/actual-charger", subprotocols=["ocpp1.6"]) as replacement:
            await _charger_call(replacement, 2, "Heartbeat", {})
            await old.close()
            await upstream.send(json.dumps([2, "current-owner", "TriggerMessage", {
                "requestedMessage": "Heartbeat",
            }]))
            forwarded = json.loads(await asyncio.wait_for(replacement.recv(), 3))
            assert forwarded[2:] == ["TriggerMessage", {"requestedMessage": "Heartbeat"}]
            await replacement.send(json.dumps([3, forwarded[1], {"status": "Accepted"}]))
            assert await asyncio.wait_for(messages.get(), 3) == [
                3, "current-owner", {"status": "Accepted"},
            ]
            assert reporter.diagnostics()["source_connected"]


async def test_lost_provider_start_ack_requires_reconciliation_after_reporter_restart(hass, socket_enabled, tmp_path):
    """No duplicate start is sent; a confirmed provider ID unlocks the waiting meter."""
    reports = asyncio.Queue()
    starts = 0

    async def handler(ws):
        nonlocal starts
        async for raw in ws:
            frame = json.loads(raw)
            await reports.put(frame)
            if frame[0] != 2:
                continue
            if frame[2] == "BootNotification":
                await ws.send(json.dumps(_accepted(frame)))
            elif frame[2] == "StartTransaction":
                starts += 1
                await ws.close()
            else:
                await ws.send(json.dumps([3, frame[1], {}]))

    async with serve(handler, "127.0.0.1", 0, subprotocols=["ocpp1.6"], ping_interval=None) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/reports"
        async with _local_link(hass, url, entry_id="lost-ack", journal_path=tmp_path / "outbox.sqlite3") as (first, charger, _, _):
            await _charger_call(charger, 1, "BootNotification", BOOT)
            await _next_action(reports, "BootNotification")
            await _charger_call(charger, 2, "StartTransaction", _start(2, 1)[3])
            await _charger_call(charger, 3, "MeterValues", _meter(3, 1, 41)[3])
            assert (await _next_action(reports, "StartTransaction"))[3] == _start(2, 1)[3]
            async with asyncio.timeout(3):
                while first.diagnostics()["blocked_event"] is None:
                    await asyncio.sleep(0)
            assert first.diagnostics()["blocked_event"]["state"] == "uncertain"
            assert first.diagnostics()["pending"] == 2
        async with _local_link(hass, url, entry_id="lost-ack", journal_path=tmp_path / "outbox.sqlite3") as (second, charger, _, _):
            await _charger_call(charger, 4, "BootNotification", BOOT)
            await _next_action(reports, "BootNotification")
            blocked = second.diagnostics()["blocked_event"]
            assert blocked["action"] == "StartTransaction"
            assert blocked["state"] == "uncertain"
            with pytest.raises(ValueError, match="provider_transaction_id_required"):
                await second.reconcile(blocked["id"], "received")
            await second.reconcile(blocked["id"], "received", 84721)
            delivered = await _next_action(reports, "MeterValues")
            assert delivered[3] == {**_meter(3, 1, 41)[3], "transactionId": 84721}
            async with asyncio.timeout(3):
                while second.diagnostics()["pending"]:
                    await asyncio.sleep(0)
            assert starts == 1


@pytest.mark.parametrize("username", ["", "provider-user"])
async def test_provider_basic_auth_is_real_and_not_logged(hass, socket_enabled, caplog, username, tmp_path):
    """Send separate credentials without leaking them through wire debug logs."""
    import base64
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.ocpp.reporting")
    password = "reporting-password-never-log"
    async with (
        _provider() as (url, connections, messages),
        _local_link(hass, url, journal_path=tmp_path / "outbox.sqlite3", settings=ReportingSettings(url, username, password)) as (_, charger, _, _),
    ):
        await _charger_call(charger, 1, "BootNotification", BOOT)
        upstream = await asyncio.wait_for(connections.get(), 3)
        await _next_action(messages, "BootNotification")
        credential = base64.b64encode(f"{username or 'actual-charger'}:{password}".encode()).decode()
        assert upstream.request.headers["Authorization"] == f"Basic {credential}"
        assert upstream.request.path == "/reports/actual-charger"
        logs = "\n".join(
            record.getMessage() for record in caplog.records
            if record.name.startswith("custom_components.ocpp.reporting") or record.name == "websockets.client"
        )
        assert password not in logs
        assert credential not in logs


async def test_storage_fault_never_stops_local_charging(hass, socket_enabled, tmp_path):
    """A real filesystem obstruction blocks reporting, not local OCPP responses."""
    obstruction = tmp_path / "not-a-directory"
    obstruction.write_text("file")
    async with _local_link(
        hass, "ws://127.0.0.1:1/unreachable",
        journal_path=obstruction / "outbox.sqlite3",
    ) as (reporter, charger, _, _):
        await _charger_call(charger, 1, "BootNotification", BOOT)
        started = await _charger_call(charger, 2, "StartTransaction", _start(2, 1)[3])
        assert started[2]["transactionId"] == 41
        await _charger_call(charger, 3, "StopTransaction", _stop(3, 41)[3])
        async with asyncio.timeout(3):
            while reporter.diagnostics()["status"] != "blocked":
                await asyncio.sleep(0)
