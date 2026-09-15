"""Session retirement regressions using the imported integration and HA fixtures."""

import asyncio
from contextlib import suppress

import pytest
from ocpp.charge_point import ChargePoint as LibCP
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.protocol import State

from homeassistant.const import STATE_UNAVAILABLE

from custom_components.ocpp.enums import HAChargerStatuses as cstat

from .test_charge_point_core import _mk_cp


class Socket:
    """Controllable transport; no external network or charger access."""

    def __init__(self):
        """Initialize an open socket with an initially unblocked close."""
        self.state = State.OPEN
        self.ended = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.close_error = None
        self.closes = 0

    async def close(self):
        """Expose a deterministic retirement barrier."""
        self.closes += 1
        self.close_entered.set()
        await self.close_release.wait()
        self.state = State.CLOSED
        self.ended.set()
        if self.close_error:
            raise self.close_error


async def ticks():
    """Drain ready callbacks without advancing wall-clock deadlines."""
    for _ in range(30):
        await asyncio.sleep(0)


@pytest.fixture
async def lifecycle(hass, monkeypatch):
    """Construct a real ChargePoint; replace only protocol and monitor I/O."""
    cp = _mk_cp(hass)
    cp._connection = Socket()
    cp._retirement_timeout = 0.05
    sockets = [cp._connection]
    runners = []
    started = {}
    releases = []

    async def receive(self):
        """Signal runner admission and wait for its captured socket to end."""
        connection = self._connection
        started.setdefault(connection, asyncio.Event()).set()
        await connection.ended.wait()

    async def monitor():
        """Keep the monitor bound to the socket captured at startup."""
        connection = cp._connection
        await connection.ended.wait()

    monkeypatch.setattr(LibCP, "start", receive)
    monkeypatch.setattr(cp, "monitor_connection", monitor)

    def spawn(coro, name=None):
        """Track a runner so fixture teardown can cancel and join it."""
        task = asyncio.create_task(coro, name=name)
        runners.append(task)
        return task

    def socket():
        """Register a controllable socket for unconditional fixture cleanup."""
        connection = Socket()
        sockets.append(connection)
        return connection

    async def began(connection):
        """Require the replacement's receive task to reach its startup barrier."""
        await ticks()
        assert connection in started, "replacement runner never started"

    yield cp, socket, spawn, began, releases
    for release in releases:
        release.set()
    for connection in sockets:
        connection.close_release.set()
        connection.close_error = None
        connection.ended.set()
    # Release hostile work before asking runners to exit, including on RED.
    await ticks()
    for task in runners:
        task.cancel()
    await asyncio.gather(*runners, return_exceptions=True)
    await ticks()


async def test_stale_finalizer_cannot_close_replacement(lifecycle, monkeypatch):
    """A's delayed finally must never close B or cancel B's children."""
    cp, socket, spawn, began, releases = lifecycle
    old = cp._connection
    entered, release = asyncio.Event(), asyncio.Event()
    releases.append(release)
    # Fail loudly if the session-owned cleanup boundary is removed or renamed.
    original = cp._stop_session

    async def paused(*args):
        """Hold only the old runner's finalizer until replacement admission."""
        if asyncio.current_task().get_name() == "old-run":
            entered.set()
            await release.wait()
        return await original(*args)

    monkeypatch.setattr(cp, "_stop_session", paused)
    old_runner = spawn(cp.start(), "old-run")
    await began(old)
    new = socket()
    replacement = spawn(cp.reconnect(new))
    await asyncio.wait_for(entered.wait(), 1)
    await began(new)
    release.set()
    # Join the old runner, including its finalizer, before observing B. Its
    # cancellation is expected when reconnect retires A's receive/monitor tasks.
    await asyncio.wait_for(asyncio.gather(old_runner, return_exceptions=True), 1)
    assert old_runner.cancelled() or old_runner.exception() is None
    assert old.state is State.CLOSED
    assert cp._connection is new
    assert new.state is State.OPEN, "stale finalizer closed replacement"
    assert cp.status == "ok"
    assert not replacement.done()
    assert all(not task.done() for task in cp.tasks)


async def test_retirement_tracking_exists_before_start(hass):
    """A fresh owner exposes an empty survivor registry before any teardown."""
    cp = _mk_cp(hass)
    assert cp._retirement_tasks == set()


async def test_clean_reconnect_and_stop(lifecycle):
    """Ordinary replacement remains available until explicit stop."""
    cp, socket, spawn, began, _ = lifecycle
    spawn(cp.start())
    await began(cp._connection)
    new = socket()
    spawn(cp.reconnect(new))
    await began(new)
    assert cp.status == "ok"
    assert new.state is State.OPEN
    assert cp._metrics[(0, cstat.reconnects)].value == 1
    await cp.stop()
    assert new.state is State.CLOSED
    assert cp.status == STATE_UNAVAILABLE
    assert all(task.done() for task in cp.tasks)


@pytest.mark.parametrize("explicit_stop", [False, True])
async def test_overlapping_admission(lifecycle, explicit_stop):
    """Newest reconnect wins; an explicit stop invalidates pending requests."""
    cp, socket, spawn, began, _ = lifecycle
    old = cp._connection
    spawn(cp.start())
    await began(old)
    old.close_release.clear()
    first, latest = socket(), socket()
    spawn(cp.reconnect(first))
    await old.close_entered.wait()
    if explicit_stop:
        spawn(cp.stop())
    else:
        spawn(cp.reconnect(latest))
    await ticks()
    old.close_release.set()
    await ticks()
    assert first.state is State.CLOSED
    assert old.closes == 1
    if explicit_stop:
        assert cp._connection is old
        assert cp.status == STATE_UNAVAILABLE
    else:
        await began(latest)
        assert cp._connection is latest
        assert latest.state is State.OPEN


async def test_concurrent_stop_is_shared_and_cancellation_atomic(lifecycle):
    """Repeated cancellation cannot interrupt shared close/child settlement."""
    cp, _, spawn, began, _ = lifecycle
    old = cp._connection
    spawn(cp.start())
    await began(old)
    old.close_release.clear()
    first = spawn(cp.stop())
    await old.close_entered.wait()
    second = spawn(cp.stop())
    for _ in range(2):
        first.cancel()
        await ticks()
    old.close_release.set()
    await asyncio.gather(first, second, return_exceptions=True)
    assert first.cancelled()
    assert not second.cancelled()
    assert second.exception() is None
    assert old.closes == 1
    assert all(task.done() for task in cp.tasks)


async def test_close_error_rejects_candidate_and_allows_retry(lifecycle):
    """A failed close is surfaced without poisoning all future reconnects."""
    cp, socket, spawn, began, _ = lifecycle
    old = cp._connection
    spawn(cp.start())
    await began(old)
    old.close_error = OSError("injected close failure")
    rejected = socket()
    with pytest.raises(OSError, match="injected close failure"):
        await cp.reconnect(rejected)
    assert rejected.state is State.CLOSED
    assert all(task.done() for task in cp.tasks)
    old.close_error = None
    new = socket()
    spawn(cp.reconnect(new))
    await began(new)
    assert new.state is State.OPEN


async def test_hostile_child_is_retained_observed_and_fences_retry(lifecycle):
    """Timeout bounds callers, not cancellation acknowledgement from children."""
    cp, socket, spawn, began, releases = lifecycle
    old = cp._connection
    entered, release, retiring = asyncio.Event(), asyncio.Event(), asyncio.Event()
    releases.append(release)

    async def hostile():
        """Resist retirement cancellation, then fail after explicit release."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            retiring.set()
            while not release.is_set():
                with suppress(asyncio.CancelledError):
                    await release.wait()
            assert cp._connection is old
            raise RuntimeError("injected late survivor failure")

    runner = spawn(cp.run([hostile()]))
    await entered.wait()
    child = cp.tasks[0]
    stop = spawn(cp.stop())
    await retiring.wait()
    for _ in range(2):
        stop.cancel()
        await ticks()
    _, pending = await asyncio.wait({stop}, timeout=0.3)
    assert not pending, "stop exceeded aggregate retirement deadline"
    assert not stop.cancelled(), "retirement failure must outrank cancellation"
    assert isinstance(stop.exception(), TimeoutError)
    assert child in cp._retirement_tasks
    assert cp.status == STATE_UNAVAILABLE
    for _ in range(2):
        rejected = socket()
        with pytest.raises(TimeoutError):
            await cp.reconnect(rejected)
        assert rejected.state is State.CLOSED
        assert cp._connection is old
    release.set()
    await asyncio.gather(runner, return_exceptions=True)
    await ticks()
    assert child not in cp._retirement_tasks
    assert child.done()
    error = child.exception()
    assert isinstance(error, RuntimeError)
    assert str(error) == "injected late survivor failure"
    new = socket()
    spawn(cp.reconnect(new))
    await began(new)
    assert new.state is State.OPEN


async def test_retirement_callback_retrieves_late_failure(lifecycle):
    """Prove owner observation without a gather or test retrieving the exception."""
    cp, _, _, _, releases = lifecycle
    entered, release = asyncio.Event(), asyncio.Event()
    releases.append(release)
    observations = []

    class ObservedTask(asyncio.Task):
        """Record calls to the public exception API without CPython internals."""

        def exception(self):
            """Record outcome retrieval by the owner before returning the error."""
            error = super().exception()
            observations.append(error)
            return error

    async def hostile():
        """Fail only after retirement has timed out and abandoned its join."""
        entered.set()
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()
        raise RuntimeError("late callback failure")

    child = ObservedTask(hostile())
    session = {"connection": cp._connection, "tasks": (child,), "cleanup": None}
    try:
        await entered.wait()
        with pytest.raises(TimeoutError):
            await cp._close_session(session, asyncio.current_task())
        assert child in cp._retirement_tasks
        assert not observations
        release.set()
        await ticks()
        assert child.done()
        assert child not in cp._retirement_tasks
        assert len(observations) == 1
        assert isinstance(observations[0], RuntimeError)
        assert str(observations[0]) == "late callback failure"
    finally:
        release.set()
        await asyncio.gather(child, return_exceptions=True)


@pytest.mark.parametrize(
    "production_deadline", [False, pytest.param(True, marks=pytest.mark.slow)]
)
async def test_hostile_close_has_aggregate_deadline(lifecycle, production_deadline):
    """Even a close that suppresses cancellation is retained and bounded."""
    cp, socket, spawn, _, releases = lifecycle
    old = cp._connection
    release = asyncio.Event()
    releases.append(release)
    if production_deadline:
        del cp._retirement_timeout
    budget = 10.0 if production_deadline else 0.05

    async def close():
        """Keep the transport close pending despite repeated cancellation."""
        old.close_entered.set()
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()
        old.state = State.CLOSED

    old.close = close
    start = asyncio.get_running_loop().time()
    stop = spawn(cp.stop())
    await old.close_entered.wait()
    for _ in range(2):
        stop.cancel()
        await ticks()
    _, pending = await asyncio.wait({stop}, timeout=budget + 0.5)
    assert not pending, "hostile close escaped aggregate bound"
    assert not stop.cancelled(), "cleanup timeout must outrank caller cancellation"
    assert isinstance(stop.exception(), TimeoutError)
    elapsed = asyncio.get_running_loop().time() - start
    assert budget <= elapsed < budget + 0.5
    assert cp._retirement_tasks
    with pytest.raises(TimeoutError):
        await cp.reconnect(socket())
    assert cp._connection is old
    release.set()
    await ticks()
    assert not cp._retirement_tasks


@pytest.mark.parametrize("child_initiates", [False, True])
async def test_child_stop_cannot_self_join_or_publish_early(lifecycle, child_initiates):
    """Exclude a stopper from its own join, but not from admission fencing."""
    cp, socket, spawn, _, releases = lifecycle
    entered, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    releases.append(release)

    async def child():
        """Exercise child-initiated stop or joining an external teardown."""
        entered.set()
        if child_initiates:
            await cp.stop()
            stopped.set()
            await release.wait()
        else:
            try:
                await asyncio.Event().wait()
            finally:
                await cp.stop()
                stopped.set()

    spawn(cp.run([child()]))
    await entered.wait()
    if not child_initiates:
        await cp.stop()
    await asyncio.wait_for(stopped.wait(), 0.3)
    if child_initiates:
        rejected = socket()
        with pytest.raises(TimeoutError):
            await cp.reconnect(rejected)
        assert rejected.state is State.CLOSED


async def test_reconnect_waiting_for_child_stop_rejects_surviving_initiator(lifecycle):
    """Recheck child settlement after joining an in-progress successful cleanup."""
    cp, socket, spawn, _, releases = lifecycle
    old = cp._connection
    old.close_release.clear()
    stopped, release = asyncio.Event(), asyncio.Event()
    releases.append(release)

    async def child():
        """Initiate teardown, then remain alive beyond its successful completion."""
        await cp.stop()
        stopped.set()
        await release.wait()

    runner = spawn(cp.run([child()]))
    await asyncio.wait_for(old.close_entered.wait(), 1)
    session = cp._session
    cleanup = session["cleanup"]
    assert not cleanup.done()
    rejected = socket()
    reconnect = spawn(cp.reconnect(rejected))
    await ticks()
    assert not reconnect.done()
    assert session["cleanup"] is cleanup
    old.close_release.set()
    await asyncio.wait_for(stopped.wait(), 1)
    with pytest.raises(TimeoutError, match="retirement survivors still active"):
        await asyncio.wait_for(reconnect, 1)
    assert cleanup.done() and cleanup.exception() is None
    assert not cp.tasks[0].done()
    assert cp.tasks[0] in cp._retirement_tasks
    assert cp._connection is old
    assert cp.status == STATE_UNAVAILABLE
    assert cp._metrics[(0, cstat.reconnects)].value == 0
    assert rejected.state is State.CLOSED
    assert rejected.closes == 1
    release.set()
    await asyncio.wait_for(runner, 1)
    await ticks()
    assert not cp._retirement_tasks


async def test_localhost_idle_reconnect_keeps_replacement_open(hass, socket_enabled):
    """Exercise real recv, client Ping/Pong and Close without OCPP calls."""
    cp = _mk_cp(hass)
    cp.post_connect_success = True

    accepted = asyncio.Queue()

    async def handler(connection):
        """Expose each accepted loopback socket until its peer closes."""
        await accepted.put(connection)
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}", ping_interval=None) as first:
            cp._connection = await accepted.get()
            old = cp._connection
            runner = asyncio.create_task(cp.start())
            replacement = None
            try:
                await ticks()
                async with connect(
                    f"ws://127.0.0.1:{port}", ping_interval=None
                ) as second:
                    new = await accepted.get()
                    replacement = asyncio.create_task(cp.reconnect(new))
                    await asyncio.wait_for(first.wait_closed(), 1)
                    await ticks()
                    assert old.state is State.CLOSED
                    assert cp._connection is new
                    assert new.state is State.OPEN
                    assert cp.status == "ok"
                    pong = await second.ping()
                    await asyncio.wait_for(pong, 1)
                    assert not replacement.done()
                    await cp.stop()
                    await asyncio.wait_for(second.wait_closed(), 1)
            finally:
                await cp.stop()
                await asyncio.gather(
                    runner,
                    *([replacement] if replacement else []),
                    return_exceptions=True,
                )
