"""Optional OCPP 1.6 reporting, independent of the local charging controller."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.parse import quote, urlsplit
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
# The WebSocket wire logger includes authentication headers and raw OCPP
# payloads at DEBUG. Report only our sanitized lifecycle diagnostics instead.
_TRANSPORT_LOGGER = logging.getLogger(f"{__name__}.transport")
_TRANSPORT_LOGGER.disabled = True
REPORTING_UPDATED = f"{DOMAIN}_reporting_updated"
_REPORT_ACTIONS = frozenset(
    {"Authorize", "StatusNotification", "StartTransaction", "MeterValues", "StopTransaction",
     "DiagnosticsStatusNotification", "FirmwareStatusNotification"}
)
_READ_ACTIONS = frozenset({"GetConfiguration", "TriggerMessage"})
_READ_CONFIGURATION_KEYS = frozenset({
    "ChargePointMaxProfileStackLevel", "ChargingScheduleAllowedChargingRateUnit",
    "ConnectorPhaseRotation", "HeartbeatInterval", "MeterValueSampleInterval",
    "MeterValuesSampledData", "MeterValuesAlignedData", "NumberOfConnectors",
    "StopTxnSampledData", "StopTxnAlignedData", "SupportedFeatureProfiles",
    "SupportedFeatureProfilesMaxLength", "SupportedFileTransferProtocols",
})
_REQUEST_TIMEOUT = 10.0
_RECONNECT_DELAY = 5.0
_MAX_PENDING_CALLS = 64


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _frame(raw: str | bytes) -> list | None:
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(frame, list) or len(frame) < 3 or not isinstance(frame[1], str):
        return None
    if frame[0] == 2 and len(frame) == 4 and isinstance(frame[2], str) and isinstance(frame[3], dict):
        return frame
    if frame[0] == 3 and len(frame) == 3 and isinstance(frame[2], dict):
        return frame
    if frame[0] == 4 and len(frame) == 5 and isinstance(frame[4], dict):
        return frame
    return None


@dataclass(frozen=True)
class ReportingSettings:
    """A single reporting destination; never include credentials in reprs."""

    url: str
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    queue_limit: int = 10000

    def validate(self) -> None:
        """Require an explicit WebSocket base URL with separate credentials."""
        if not isinstance(self.url, str) or self.url != self.url.strip():
            raise ValueError("invalid_reporting_url")
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError:
            raise ValueError("invalid_reporting_url") from None
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname or port == 0:
            raise ValueError("invalid_reporting_url")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid_reporting_url")
        if type(self.queue_limit) is not int or not 1 <= self.queue_limit <= 100000:
            raise ValueError("invalid_reporting_queue_limit")

    def endpoint(self, cp_id: str) -> str:
        """Append the real charger identity exactly once to the configured base."""
        return self.url.rstrip("/") + "/" + quote(cp_id, safe="")

    def destination(self, cp_id: str) -> str:
        """Bind persisted IDs to a destination, but permit password rotation."""
        return hashlib.sha256(_json([self.endpoint(cp_id), self.username or cp_id]).encode()).hexdigest()


class ReportingError(Exception):
    """A reporting-integrity error identified by a safe, non-secret code."""


class ReportingJournal:
    """Synchronous journal; every production call runs in HA's executor."""

    def __init__(self, path: Path, destination: str, limit: int):
        """Open the private outbox and fence any interrupted delivery."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.limit = limit
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                start_key TEXT PRIMARY KEY, local_tx INTEGER, remote_tx INTEGER,
                connector INTEGER NOT NULL, ended INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS local_transaction ON sessions(local_tx);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL,
                message_id TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL,
                start_key TEXT, state TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL, error TEXT
            );
        """)
        try:
            with self.db:
                self.db.execute("UPDATE events SET state='uncertain',error='interrupted_delivery' WHERE state='inflight'")
                self.db.execute("DELETE FROM events WHERE state='done' AND created < ?", (time.time() - 7 * 86400,))
                previous = self._get("destination")
                if previous and previous != destination:
                    pending = self.db.execute("SELECT 1 FROM events WHERE state!='done' LIMIT 1").fetchone()
                    active = self.db.execute("SELECT 1 FROM sessions WHERE ended=0 LIMIT 1").fetchone()
                    if pending or active:
                        raise ReportingError("destination_changed")
                    self.db.execute("DELETE FROM sessions")
                    self.db.execute("DELETE FROM events")
                    self.db.execute("DELETE FROM metadata WHERE key='boot'")
                self._put("destination", destination)
        except BaseException:
            self.db.close()
            raise

    def _get(self, key: str):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, _json(value)))

    def close(self) -> None:
        """Close the executor-owned connection."""
        self.db.close()

    def pause(self, reason: str) -> dict:
        """Latch a data-integrity failure rather than silently dropping reports."""
        with self.db:
            self._put("integrity_error", reason)
        return self.summary()

    def record(self, frame: list, result: list) -> dict:
        """Record only messages acknowledged by the real local OCPP server."""
        action, body = frame[2], frame[3]
        if result[0] != 3:
            return self.summary()
        with self.db:
            if action == "BootNotification":
                if result[2].get("status") == "Accepted":
                    self._put("boot", body)
                    self._put("boot_generation", (self._get("boot_generation") or 0) + 1)
                return self.summary()
            if action not in _REPORT_ACTIONS or self._get("integrity_error"):
                return self.summary()
            digest = hashlib.sha256(_json(frame).encode()).hexdigest()
            if self.db.execute("SELECT 1 FROM events WHERE digest=?", (digest,)).fetchone():
                return self.summary()
            if self.db.execute("SELECT count(*) FROM events WHERE state!='done'").fetchone()[0] >= self.limit:
                self._put("integrity_error", "queue_full")
                return self.summary()
            start_key = None
            if action == "StartTransaction":
                local_tx = result[2].get("transactionId")
                if result[2].get("idTagInfo", {}).get("status") != "Accepted":
                    return self.summary()
                if not isinstance(local_tx, int) or isinstance(local_tx, bool):
                    self._put("integrity_error", "invalid_local_transaction")
                    return self.summary()
                start_key = digest
                self.db.execute(
                    "INSERT INTO sessions(start_key,local_tx,connector) VALUES(?,?,?)",
                    (digest, local_tx, body["connectorId"]),
                )
            elif action in ("MeterValues", "StopTransaction") and "transactionId" in body:
                matches = self.db.execute("SELECT start_key FROM sessions WHERE local_tx=?", (body["transactionId"],)).fetchall()
                if len(matches) != 1:
                    self._put("integrity_error", "unmapped_local_transaction")
                    return self.summary()
                start_key = matches[0][0]
                if action == "StopTransaction":
                    self.db.execute("UPDATE sessions SET ended=1 WHERE start_key=?", (start_key,))
            self.db.execute(
                "INSERT INTO events(digest,message_id,action,body,start_key,created) VALUES(?,?,?,?,?,?)",
                (digest, str(uuid.uuid4()), action, _json(body), start_key, time.time()),
            )
        return self.summary()

    def next_report(self):
        """Return one ordered report, never replaying an ambiguous delivery."""
        if self._get("integrity_error"):
            return None
        row = self.db.execute("SELECT * FROM events WHERE state!='done' ORDER BY id LIMIT 1").fetchone()
        if not row or row["state"] != "pending":
            return None
        event = dict(row)
        body = json.loads(event["body"])
        if event["start_key"] and event["action"] != "StartTransaction" and "transactionId" in body:
            session = self.db.execute("SELECT remote_tx FROM sessions WHERE start_key=?", (event["start_key"],)).fetchone()
            if session is None or session[0] is None:
                raise ReportingError("unmapped_remote_transaction")
            body["transactionId"] = session[0]
        event["body"] = body
        return event

    def mark(self, event_id: int, state: str, error: str | None = None) -> dict:
        """Persist a delivery boundary before changing network state."""
        with self.db:
            self.db.execute("UPDATE events SET state=?,error=? WHERE id=?", (state, error, event_id))
        return self.summary()

    def acknowledge(self, event: dict, frame: list) -> dict:
        """Commit the provider's acknowledgement and transaction mapping."""
        if frame[0] != 3:
            raise ReportingError("provider_rejected_report")
        result = frame[2]
        with self.db:
            if event["action"] == "StartTransaction":
                remote_tx = result.get("transactionId")
                if result.get("idTagInfo", {}).get("status") != "Accepted" or not isinstance(remote_tx, int) or isinstance(remote_tx, bool):
                    raise ReportingError("provider_rejected_transaction")
                self.db.execute("UPDATE sessions SET remote_tx=? WHERE start_key=?", (remote_tx, event["start_key"]))
            self.db.execute("UPDATE events SET state='done',body='{}',error=NULL WHERE id=?", (event["id"],))
            self._put("last_acknowledged", time.time())
        return self.summary()

    def reconcile(self, event_id: int, outcome: str, remote_tx: int | None = None) -> dict:
        """Apply the operator's provider-confirmed outcome, never infer receipt."""
        if type(event_id) is not int or event_id < 1:
            raise ValueError("invalid_reporting_event_id")
        event = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["state"] not in ("uncertain", "blocked"):
            raise ValueError("event_not_reconcilable")
        if outcome == "not_received":
            return self.mark(event_id, "pending")
        if outcome != "received":
            raise ValueError("invalid_reporting_outcome")
        result = {}
        if event["action"] == "StartTransaction":
            if type(remote_tx) is not int:
                raise ValueError("provider_transaction_id_required")
            result = {"transactionId": remote_tx, "idTagInfo": {"status": "Accepted"}}
        return self.acknowledge(dict(event), [3, event["message_id"], result])

    def interrupt(self) -> dict:
        """Fence unfinished delivery after workers retire, including cancellation."""
        with self.db:
            self.db.execute("UPDATE events SET state='uncertain',error='interrupted_delivery' WHERE state='inflight'")
        return self.summary()

    def summary(self) -> dict:
        """Return only safe, bounded delivery-health metadata."""
        event = self.db.execute("SELECT id,action,state,error FROM events WHERE state!='done' ORDER BY id LIMIT 1").fetchone()
        return {
            "pending": self.db.execute("SELECT count(*) FROM events WHERE state!='done'").fetchone()[0],
            "blocked_event": dict(event) if event and event["state"] in ("uncertain", "blocked") else None,
            "integrity_error": self._get("integrity_error"),
            "last_acknowledged": self._get("last_acknowledged"),
            "boot_generation": self._get("boot_generation") or 0,
        }

    def boot(self):
        """Return the real accepted registration and its refresh generation."""
        body = self._get("boot")
        return {"payload": body, "generation": self._get("boot_generation") or 0} if body else None


class ReportingTransport:
    """Transparent single-reader boundary; never await reporting storage/network."""

    def __init__(self, connection, reporter: SecondaryReporter):
        """Wrap one local socket without creating another receive loop."""
        self.connection = connection
        self.reporter = reporter
        self.pending: dict[str, list] = {}
        self.queries: dict[str, asyncio.Future] = {}
        self.local_calls: set[str] = set()
        self.attached = False

    def __getattr__(self, name):
        """Preserve the connection API expected by the local controller."""
        return getattr(self.connection, name)

    def _attach(self) -> None:
        # A replacement socket is not authoritative until its local protocol
        # loop starts. Construction alone must not replace a live transport.
        if not self.attached:
            self.attached = True
            self.reporter.attach(self)

    async def recv(self):
        """Observe real source calls and isolate provider-query replies."""
        self._attach()
        try:
            while True:
                raw = await self.connection.recv()
                frame = _frame(raw)
                if frame and frame[0] in (3, 4) and frame[1].startswith("reporting-") and frame[1] not in self.local_calls:
                    future = self.queries.get(frame[1])
                    if future and not future.done():
                        future.set_result(frame)
                    # Include late replies to timed-out queries: they must
                    # never enter the local controller's response queue.
                    continue
                if frame and frame[0] == 2 and frame[2] in _REPORT_ACTIONS | {"BootNotification"}:
                    if len(self.pending) >= _MAX_PENDING_CALLS:
                        self.reporter.integrity_failure("unacknowledged_messages_overflow")
                    else:
                        self.pending[frame[1]] = frame
                if frame and frame[0] in (3, 4):
                    self.local_calls.discard(frame[1])
                return raw
        except ConnectionClosed:
            self._detach()
            raise

    async def send(self, raw):
        """Publish only locally acknowledged calls after the local send."""
        self._attach()
        frame = _frame(raw)
        if frame and frame[0] == 2 and frame[1].startswith("reporting-"):
            self.local_calls.add(frame[1])
        await self.connection.send(raw)
        if frame and frame[0] in (3, 4):
            source = self.pending.pop(frame[1], None)
            if source and frame[0] == 3:
                self.reporter.observe(source, frame)

    async def query(self, frame: list) -> list:
        """Correlate an allowlisted provider query without a second recv loop."""
        if len(self.queries) >= _MAX_PENDING_CALLS:
            raise ReportingError("too_many_queries")
        message_id = "reporting-" + uuid.uuid4().hex[:26]
        while message_id in self.local_calls or message_id in self.queries:
            message_id = "reporting-" + uuid.uuid4().hex[:26]
        future = asyncio.get_running_loop().create_future()
        self.queries[message_id] = future
        try:
            await self.connection.send(_json([2, message_id, frame[2], frame[3]]))
            response = await asyncio.wait_for(future, _REQUEST_TIMEOUT)
            return [response[0], frame[1], *response[2:]]
        finally:
            self.queries.pop(message_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def _detach(self) -> None:
        if self.reporter.transport is self:
            self.reporter.attach(None)
        for future in self.queries.values():
            if not future.done():
                future.set_exception(ConnectionError("charger_disconnected"))

    async def close(self, *args, **kwargs):
        """Close this socket without detaching an admitted replacement."""
        try:
            return await self.connection.close(*args, **kwargs)
        finally:
            self._detach()


class SecondaryReporter:
    """Own reporting tasks at entry level, outside ChargePoint.run's task group."""

    def __init__(self, hass: HomeAssistant, entry_id: str, cp_id: str, cpid: str, settings: ReportingSettings):
        """Prepare a per-charger sidecar without opening network or storage."""
        self.hass, self.cp_id, self.cpid = hass, cp_id, cpid
        self.settings = settings
        self.entry_id = entry_id
        validation_error = None
        try:
            self.settings.validate()
        except ValueError as exc:
            validation_error = str(exc)
        digest = hashlib.sha256(cp_id.encode()).hexdigest()[:16]
        self.path = Path(hass.config.path(".storage", f"ocpp.reporting.{entry_id}.{digest}.sqlite3"))
        self.issue_id = f"reporting_{entry_id}_{digest}"
        self.transport: ReportingTransport | None = None
        self.status = "starting"
        self.state = {"pending": 0, "blocked_event": None, "integrity_error": None, "last_acknowledged": None}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1 if validation_error else settings.queue_limit)
        self._wake = asyncio.Event()
        self._loaded = asyncio.Event()
        self._db_lock = asyncio.Lock()
        self._journal: ReportingJournal | None = None
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._fatal: str | None = validation_error
        self._upstream = None
        self._pending: dict[str, asyncio.Future] = {}
        self._query_tasks: set[asyncio.Task] = set()
        self._call_lock = asyncio.Lock()
        self._protocol = "1.6"

    def start(self) -> None:
        """Start independent workers without holding up charger admission."""
        if self._fatal:
            self.status = "blocked"
            self._loaded.set()
            self._publish()
            return
        self._tasks = [
            self.hass.async_create_background_task(self._ingest(), f"OCPP reporting storage {self.cpid}"),
            self.hass.async_create_background_task(self._run(), f"OCPP secondary reporting {self.cpid}"),
        ]
        if urlsplit(self.settings.url).scheme == "ws":
            _LOGGER.warning("Secondary OCPP reporting for %s uses unencrypted ws://", self.cpid)

    async def _db(self, method: str, *args):
        async with self._db_lock:
            future = self.hass.async_add_executor_job(getattr(self._journal, method), *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # Do not release the lock or close SQLite while an executor
                # operation is still running on its connection.
                await future
                raise

    def _publish(self) -> None:
        blocked = self.state.get("blocked_event")
        error = (
            self._fatal or self.state.get("integrity_error")
            or (blocked["state"] if blocked else None)
            or ("unsupported_protocol" if self.status == "unsupported_protocol" else None)
        )
        if error:
            ir.async_create_issue(
                self.hass, DOMAIN, self.issue_id, is_fixable=False,
                severity=ir.IssueSeverity.WARNING, translation_key="secondary_reporting",
                translation_placeholders={"charger": self.cpid, "reason": str(error)},
            )
        elif self.status == "connected":
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        async_dispatcher_send(self.hass, REPORTING_UPDATED, self.cpid)

    def diagnostics(self) -> dict:
        """Expose health, never raw events, identities or credentials."""
        blocked = self._fatal or self.state.get("integrity_error") or self.state.get("blocked_event")
        return {
            "status": "blocked" if blocked else self.status,
            **self.state, "buffered": self._queue.qsize(),
            "source_connected": self.transport is not None,
        }

    def attach(self, transport: ReportingTransport | None, protocol: str = "1.6") -> None:
        """Track only the locally admitted transport and negotiated protocol."""
        self.transport = transport
        self._protocol = protocol
        if protocol != "1.6":
            self.status = "unsupported_protocol"
        self._wake.set()
        self._publish()

    def integrity_failure(self, reason: str) -> None:
        """Stop reporting, never the local OCPP controller, after a data gap."""
        self._fatal = reason
        self.status = "blocked"
        self._wake.set()
        self._publish()

    def observe(self, frame: list, response: list) -> None:
        """Buffer an acknowledged source message without blocking local control."""
        if self._closing or self._fatal:
            return
        try:
            self._queue.put_nowait((frame, response))
        except asyncio.QueueFull:
            self.integrity_failure("buffer_full")

    async def _ingest(self) -> None:
        try:
            self._journal = await self.hass.async_add_executor_job(
                ReportingJournal, self.path, self.settings.destination(self.cp_id), self.settings.queue_limit
            )
            self.state = await self._db("summary")
            self._loaded.set()
            self._publish()
            while True:
                frame, response = await self._queue.get()
                try:
                    self.state = await self._db("record", frame, response)
                    if self._fatal:
                        self.state = await self._db("pause", self._fatal)
                    self._wake.set()
                    self._publish()
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fatal = str(exc) if isinstance(exc, ReportingError) else "storage_error"
            self.status = "blocked"
            self._loaded.set()
            self._wake.set()
            self._publish()
            _LOGGER.error("Secondary OCPP storage unavailable for %s (%s)", self.cpid, type(exc).__name__)

    async def _call(self, action: str, body: dict, message_id: str | None = None) -> list:
        async with self._call_lock:
            message_id = message_id or str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            self._pending[message_id] = future
            try:
                await self._upstream.send(_json([2, message_id, action, body]))
                return await asyncio.wait_for(future, _REQUEST_TIMEOUT)
            finally:
                self._pending.pop(message_id, None)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    async def _query(self, ws, frame: list) -> None:
        try:
            transport = self.transport
            allowed = frame[2] in _READ_ACTIONS
            if frame[2] == "GetConfiguration":
                keys = frame[3].get("key")
                allowed = (
                    isinstance(keys, list) and bool(keys)
                    and all(isinstance(key, str) and key in _READ_CONFIGURATION_KEYS for key in keys)
                )
            if frame[2] == "TriggerMessage":
                allowed = frame[3].get("requestedMessage") in {"BootNotification", "StatusNotification", "MeterValues", "Heartbeat", "FirmwareStatusNotification", "DiagnosticsStatusNotification"}
            if not allowed or transport is None:
                response = [4, frame[1], "NotSupported", "Home Assistant owns charging control; query unavailable", {}]
            else:
                try:
                    response = await transport.query(frame)
                except (TimeoutError, ConnectionError, ConnectionClosed, ReportingError):
                    response = [4, frame[1], "InternalError", "Real charger did not answer the query", {}]
            await ws.send(_json(response))
        except ConnectionClosed:
            return

    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                frame = _frame(raw)
                if frame is None:
                    raise ReportingError("invalid_provider_frame")
                if frame[0] in (3, 4):
                    future = self._pending.get(frame[1])
                    if future and not future.done():
                        future.set_result(frame)
                elif len(self._query_tasks) < _MAX_PENDING_CALLS:
                    task = self.hass.async_create_background_task(self._query(ws, frame), f"OCPP provider query {self.cpid}")
                    self._query_tasks.add(task)
                    task.add_done_callback(self._query_done)
                else:
                    await ws.send(_json([4, frame[1], "OccurrenceConstraintViolation", "Too many outstanding queries", {}]))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("provider_disconnected"))

    def _query_done(self, task: asyncio.Task) -> None:
        self._query_tasks.discard(task)
        if not task.cancelled() and task.exception():
            _LOGGER.warning("Secondary OCPP query failed for %s", self.cpid)

    async def _register(self, payload: dict) -> float:
        """Register using only an actual accepted charger BootNotification."""
        result = await self._call("BootNotification", payload)
        if result[0] != 3 or result[2].get("status") != "Accepted":
            raise ReportingError("registration_rejected")
        interval = result[2].get("interval", 300)
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise ReportingError("invalid_heartbeat_interval")
        return interval

    async def _deliver(self, boot: dict) -> None:
        interval = await self._register(boot["payload"])
        generation = boot["generation"]
        next_heartbeat = time.monotonic() + interval
        self.status = "connected"
        self._publish()
        while True:
            self._wake.clear()
            if self._protocol != "1.6":
                return
            if self._fatal:
                if self._journal:
                    self.state = await self._db("pause", self._fatal)
                self._publish()
                await self._wake.wait()
                continue
            if self.state.get("boot_generation", 0) != generation:
                boot = await self._db("boot")
                interval = await self._register(boot["payload"])
                generation = boot["generation"]
                next_heartbeat = time.monotonic() + interval
            try:
                event = await self._db("next_report")
            except ReportingError as exc:
                self.state = await self._db("pause", str(exc))
                self._publish()
                continue
            if event is not None:
                self.state = await self._db("mark", event["id"], "inflight")
                try:
                    response = await self._call(event["action"], event["body"], event["message_id"])
                except BaseException:
                    self.state = await self._db("mark", event["id"], "uncertain", "acknowledgement_unknown")
                    self._publish()
                    raise
                try:
                    self.state = await self._db("acknowledge", event, response)
                except ReportingError as exc:
                    self.state = await self._db("mark", event["id"], "blocked", str(exc))
                except Exception:
                    # The provider may have accepted it, but its receipt could
                    # not be committed locally. Never resend it automatically.
                    self.state = await self._db("mark", event["id"], "uncertain", "ack_persistence_failed")
                self._publish()
                continue
            if self.transport is not None and time.monotonic() >= next_heartbeat:
                await self._call("Heartbeat", {})
                next_heartbeat = time.monotonic() + interval
            try:
                timeout = max(0.1, next_heartbeat - time.monotonic()) if self.transport is not None else interval
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                next_heartbeat = max(next_heartbeat, time.monotonic())

    async def _run(self) -> None:
        await self._loaded.wait()
        while not self._closing:
            self._wake.clear()
            if self._fatal or self.transport is None or self._protocol != "1.6":
                if not self._fatal and self._protocol == "1.6":
                    self.status = "waiting_for_charger"
                    self._publish()
                await self._wake.wait()
                continue
            try:
                boot = await self._db("boot")
            except Exception:
                self.integrity_failure("storage_error")
                continue
            if not boot:
                await self._wake.wait()
                continue
            headers = None
            if self.settings.username or self.settings.password:
                credentials = base64.b64encode(f"{self.settings.username or self.cp_id}:{self.settings.password}".encode()).decode()
                headers = {"Authorization": "Basic " + credentials}
            try:
                async with connect(self.settings.endpoint(self.cp_id), subprotocols=["ocpp1.6"], additional_headers=headers, compression=None, open_timeout=10, max_size=1048576, logger=_TRANSPORT_LOGGER) as ws:
                    self._upstream = ws
                    reader = self.hass.async_create_background_task(self._read(ws), f"OCPP reporting receive {self.cpid}")
                    delivery = self.hass.async_create_background_task(self._deliver(boot), f"OCPP reporting delivery {self.cpid}")
                    try:
                        done, _ = await asyncio.wait((reader, delivery), return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        children = [reader, delivery, *self._query_tasks]
                        for task in children:
                            task.cancel()
                        await asyncio.gather(*children, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = "disconnected"
                _LOGGER.warning("Secondary OCPP reporting disconnected for %s (%s); local control is independent", self.cpid, type(exc).__name__)
                ir.async_create_issue(self.hass, DOMAIN, self.issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="secondary_reporting", translation_placeholders={"charger": self.cpid, "reason": "connection_failed"})
            finally:
                self._upstream = None
                if self._protocol == "1.6":
                    self.status = "disconnected"
                if self._journal is not None:
                    try:
                        self.state = await self._db("interrupt")
                    except Exception:
                        self.integrity_failure("storage_error")
                self._publish()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), _RECONNECT_DELAY)

    async def reconcile(self, event_id: int, outcome: str, remote_tx: int | None = None) -> None:
        """Resume only after an operator confirms the provider's receipt state."""
        await self._loaded.wait()
        if self._journal is None:
            raise ValueError("reporting_storage_unavailable")
        self.state = await self._db("reconcile", event_id, outcome, remote_tx)
        self._wake.set()
        self._publish()

    async def close(self) -> None:
        """Retire workers and finish already queued writes before closing SQLite."""
        self._closing = True
        if self._tasks and not self._tasks[0].done():
            await self._loaded.wait()
            try:
                await asyncio.wait_for(self._queue.join(), 10)
            except TimeoutError:
                self._fatal = "unflushed_buffer"
                if self._journal:
                    self.state = await self._db("pause", self._fatal)
                self._publish()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._journal:
            await self._db("close")
            self._journal = None
