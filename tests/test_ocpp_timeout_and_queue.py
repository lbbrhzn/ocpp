"""Tests for OCPP timeout handling and command queue."""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock
import pytest
from websockets.protocol import State
from homeassistant.const import STATE_OK
from custom_components.ocpp.command_queue import (
    CommandQueue,
    QueuedCommand,
    profile_purpose,
)
from custom_components.ocpp.chargepoint import ChargePoint
from custom_components.ocpp.ocppv201 import ChargePoint as ChargePointV201
from ocpp.v201.enums import RequestStartStopStatusEnumType

from .test_reconnect_lifecycle import lifecycle as lifecycle_fixture, ticks

# Reuse the session-lifecycle fixture rather than rebuild a second, divergent
# fake of the same machinery. Imported under another name and rebound, because
# importing it as "lifecycle" shadows the argument of every test that takes it.
lifecycle = lifecycle_fixture

pytestmark = pytest.mark.asyncio


class TestCommandQueue:
    """Tests for CommandQueue class."""

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue_all(self):
        """Test basic enqueue and dequeue_all functionality."""
        queue = CommandQueue()

        cmd1 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=AsyncMock(),
            connector_id=1,
        )
        cmd2 = QueuedCommand(
            call_type="RemoteStartTransaction",
            call_fn=AsyncMock(),
            connector_id=1,
        )

        await queue.enqueue(cmd1)
        await queue.enqueue(cmd2)

        commands = await queue.dequeue_all()
        assert len(commands) == 2
        assert commands[0] is cmd1
        assert commands[1] is cmd2

        # Queue should be empty after dequeue_all
        commands_again = await queue.dequeue_all()
        assert len(commands_again) == 0

    @pytest.mark.asyncio
    async def test_command_coalescing_same_type_and_connector(self):
        """Test that newer commands replace older ones of the same type/connector."""
        queue = CommandQueue()

        call_fn1 = AsyncMock()
        call_fn2 = AsyncMock()
        call_fn3 = AsyncMock()

        cmd1 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=call_fn1,
            connector_id=1,
        )
        cmd2 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=call_fn2,
            connector_id=1,
        )
        cmd3 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=call_fn3,
            connector_id=1,
        )

        await queue.enqueue(cmd1)
        await queue.enqueue(cmd2)
        await queue.enqueue(cmd3)

        commands = await queue.dequeue_all()
        assert len(commands) == 1
        assert commands[0] is cmd3

    @pytest.mark.asyncio
    async def test_no_coalescing_different_connectors(self):
        """Test that commands for different connectors are not coalesced."""
        queue = CommandQueue()

        cmd1 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=AsyncMock(),
            connector_id=1,
        )
        cmd2 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=AsyncMock(),
            connector_id=2,
        )

        await queue.enqueue(cmd1)
        await queue.enqueue(cmd2)

        commands = await queue.dequeue_all()
        assert len(commands) == 2

    @pytest.mark.asyncio
    async def test_no_coalescing_different_types(self):
        """Test that commands of different types are not coalesced."""
        queue = CommandQueue()

        cmd1 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=AsyncMock(),
            connector_id=1,
        )
        cmd2 = QueuedCommand(
            call_type="RemoteStartTransaction",
            call_fn=AsyncMock(),
            connector_id=1,
        )

        await queue.enqueue(cmd1)
        await queue.enqueue(cmd2)

        commands = await queue.dequeue_all()
        assert len(commands) == 2

    @pytest.mark.asyncio
    async def test_clear(self):
        """Test clearing the queue."""
        queue = CommandQueue()

        await queue.enqueue(
            QueuedCommand(
                call_type="SetChargingProfile",
                call_fn=AsyncMock(),
                connector_id=1,
            )
        )

        await queue.clear()
        assert queue.is_empty()


class TestTimeoutHandling:
    """Tests for ChargePoint timeout handling and queue replay."""

    @pytest.mark.asyncio
    async def test_timeout_triggers_reconnect(self, monkeypatch):
        """Test that timeout in _call_with_timeout_handling queues for replay."""
        chargepoint = MagicMock(spec=ChargePoint)
        chargepoint._command_queue = CommandQueue()
        chargepoint.id = "test_charger"

        # Monkeypatch call to raise TimeoutError
        async def mock_call(*args, **kwargs):
            raise TimeoutError("Waited 10s for response")

        chargepoint.call = mock_call

        # Bind the real _call_with_timeout_handling method to the mock
        chargepoint._call_with_timeout_handling = (
            ChargePoint._call_with_timeout_handling.__get__(chargepoint, ChargePoint)
        )

        req = MagicMock()

        with pytest.raises(TimeoutError):
            await chargepoint._call_with_timeout_handling(
                req,
                call_type="SetChargingProfile",
                connector_id=1,
            )

        # Verify command was queued for replay on reconnect
        assert not chargepoint._command_queue.is_empty()

    @pytest.mark.asyncio
    async def test_command_coalescing_in_queue(self):
        """Test command coalescing when multiple SetChargingProfile queued."""
        queue = CommandQueue()

        mock_fn1 = AsyncMock()
        mock_fn2 = AsyncMock()
        mock_fn3 = AsyncMock()

        cmd1 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=mock_fn1,
            connector_id=1,
            kwargs={"current": 6},
        )
        cmd2 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=mock_fn2,
            connector_id=1,
            kwargs={"current": 10},
        )
        cmd3 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=mock_fn3,
            connector_id=1,
            kwargs={"current": 16},
        )

        await queue.enqueue(cmd1)
        await queue.enqueue(cmd2)
        await queue.enqueue(cmd3)

        commands = await queue.dequeue_all()
        assert len(commands) == 1
        assert commands[0].kwargs["current"] == 16

    @pytest.mark.asyncio
    async def test_mixed_queue_types_no_coalescing(self):
        """Test that different command types are not coalesced."""
        queue = CommandQueue()

        cmd1 = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=AsyncMock(),
            connector_id=1,
        )
        cmd2 = QueuedCommand(
            call_type="RemoteStartTransaction",
            call_fn=AsyncMock(),
            connector_id=1,
        )

        await queue.enqueue(cmd1)
        await queue.enqueue(cmd2)

        commands = await queue.dequeue_all()
        assert len(commands) == 2
        assert commands[0].call_type == "SetChargingProfile"
        assert commands[1].call_type == "RemoteStartTransaction"

    @pytest.mark.asyncio
    async def test_timeout_with_dict_profile_purpose(self):
        """Test profile purpose extraction from dict."""
        chargepoint = MagicMock(spec=ChargePoint)
        chargepoint._command_queue = CommandQueue()
        chargepoint.id = "test_charger"

        async def mock_call(*args, **kwargs):
            raise TimeoutError("Timeout")

        chargepoint.call = mock_call
        chargepoint._call_with_timeout_handling = (
            ChargePoint._call_with_timeout_handling.__get__(chargepoint, ChargePoint)
        )

        # Request with profile purpose in dict
        req = MagicMock()
        req.cs_charging_profiles = {"charging_profile_purpose": "tx_profile"}

        with pytest.raises(TimeoutError):
            await chargepoint._call_with_timeout_handling(
                req, call_type="SetChargingProfile", connector_id=1
            )

        # Verify purpose was extracted and queued
        commands = await chargepoint._command_queue.dequeue_all()
        assert len(commands) == 1
        assert commands[0].profile_purpose == "tx_profile"

    @pytest.mark.asyncio
    async def test_timeout_with_attribute_profile_purpose(self):
        """Test profile purpose extraction from attribute."""
        chargepoint = MagicMock(spec=ChargePoint)
        chargepoint._command_queue = CommandQueue()
        chargepoint.id = "test_charger"

        async def mock_call(*args, **kwargs):
            raise TimeoutError("Timeout")

        chargepoint.call = mock_call
        chargepoint._call_with_timeout_handling = (
            ChargePoint._call_with_timeout_handling.__get__(chargepoint, ChargePoint)
        )

        # Request with profile purpose as attribute
        req = MagicMock()
        profiles = MagicMock()
        profiles.charging_profile_purpose = "tx_default_profile"
        req.cs_charging_profiles = profiles

        with pytest.raises(TimeoutError):
            await chargepoint._call_with_timeout_handling(
                req, call_type="SetChargingProfile", connector_id=1
            )

        # Verify purpose was extracted
        commands = await chargepoint._command_queue.dequeue_all()
        assert len(commands) == 1
        assert commands[0].profile_purpose == "tx_default_profile"

    @pytest.mark.asyncio
    async def test_timeout_without_profile_purpose(self):
        """Test timeout when profile purpose is not available."""
        chargepoint = MagicMock(spec=ChargePoint)
        chargepoint._command_queue = CommandQueue()
        chargepoint.id = "test_charger"

        async def mock_call(*args, **kwargs):
            raise TimeoutError("Timeout")

        chargepoint.call = mock_call
        chargepoint._call_with_timeout_handling = (
            ChargePoint._call_with_timeout_handling.__get__(chargepoint, ChargePoint)
        )

        # Request without cs_charging_profiles
        req = MagicMock(spec=[])

        with pytest.raises(TimeoutError):
            await chargepoint._call_with_timeout_handling(
                req, call_type="RemoteStartTransaction", connector_id=1
            )

        # Verify command was queued with None purpose
        commands = await chargepoint._command_queue.dequeue_all()
        assert len(commands) == 1
        assert commands[0].profile_purpose is None

    @pytest.mark.asyncio
    async def test_queued_command_execution_failure(self):
        """Test handling when queued command execution fails."""
        chargepoint = MagicMock(spec=ChargePoint)
        chargepoint._command_queue = CommandQueue()
        chargepoint.id = "test_charger"

        # Create a command that fails on execution
        failing_fn = AsyncMock(side_effect=RuntimeError("Command failed"))
        cmd = QueuedCommand(
            call_type="SetChargingProfile",
            call_fn=failing_fn,
            connector_id=1,
        )

        await chargepoint._command_queue.enqueue(cmd)

        # Bind the real _replay_queue method
        chargepoint._replay_queue = ChargePoint._replay_queue.__get__(
            chargepoint, ChargePoint
        )

        # Replay should handle the error gracefully
        await chargepoint._replay_queue()

        # Verify command was attempted
        failing_fn.assert_called_once()

        # Queue should be empty after replay
        assert chargepoint._command_queue.is_empty()


class TestProfilePurpose:
    """The coalescing key has to survive every shape a request arrives in."""

    async def test_v16_wire_key(self):
        """1.6 builds the dict with OcppMisc's wire names."""
        req = MagicMock()
        req.cs_charging_profiles = {"chargingProfilePurpose": "TxProfile"}
        assert profile_purpose(req) == "TxProfile"

    async def test_snake_case_key(self):
        """A snake_case dict is read too."""
        req = MagicMock()
        req.cs_charging_profiles = {"charging_profile_purpose": "TxDefaultProfile"}
        assert profile_purpose(req) == "TxDefaultProfile"

    async def test_v201_charging_profile_attribute(self):
        """2.0.1 names the field differently and may pass a dataclass."""
        req = MagicMock(spec=["charging_profile"])
        req.charging_profile = SimpleNamespace(
            charging_profile_purpose="ChargingStationMaxProfile"
        )
        assert profile_purpose(req) == "ChargingStationMaxProfile"

    async def test_v201_charging_profile_dict(self):
        """2.0.1 as a snake_case dict under its own field name."""
        req = MagicMock(spec=["charging_profile"])
        req.charging_profile = {"charging_profile_purpose": "TxProfile"}
        assert profile_purpose(req) == "TxProfile"

    async def test_dict_without_a_purpose(self):
        """A profile dict carrying no purpose coalesces by connector alone."""
        req = MagicMock()
        req.cs_charging_profiles = {"chargingProfileId": 1, "stackLevel": 0}
        assert profile_purpose(req) is None

    async def test_no_profile_at_all(self):
        """A request that is not a charging profile has no purpose."""
        assert profile_purpose(MagicMock(spec=[])) is None

    async def test_explicit_none_purpose(self):
        """An explicit null is None, not the string "None"."""
        req = MagicMock()
        req.cs_charging_profiles = {"chargingProfilePurpose": None}
        assert profile_purpose(req) is None


class TestFailConnectionOnTimeout:
    """Queueing only matters if something then causes a reconnect.

    The ocpp library raises TimeoutError without touching the socket, so
    without an explicit failure here the queue is never drained.
    """

    def _cp(self, state=State.OPEN):
        chargepoint = MagicMock(spec=ChargePoint)
        chargepoint.id = "test_charger"
        chargepoint._command_queue = CommandQueue()
        chargepoint._replaying = False
        chargepoint._closing_tasks = set()
        chargepoint._connection = MagicMock()
        chargepoint._connection.state = state
        chargepoint._connection.close = AsyncMock()

        async def mock_call(*args, **kwargs):
            raise TimeoutError("Timeout")

        chargepoint.call = mock_call
        for name in ("_call_with_timeout_handling", "_fail_connection_for_replay"):
            setattr(
                chargepoint,
                name,
                getattr(ChargePoint, name).__get__(chargepoint, ChargePoint),
            )
        return chargepoint

    @pytest.mark.asyncio
    async def test_timeout_fails_the_connection(self):
        """A timed-out call queues the command and drops the transport."""
        cp = self._cp()

        with pytest.raises(TimeoutError):
            await cp._call_with_timeout_handling(
                MagicMock(), call_type="SetChargingProfile", connector_id=1
            )

        # The close runs as a background task, so let it start.
        await asyncio.sleep(0)
        cp._connection.close.assert_awaited_once()
        assert not cp._command_queue.is_empty()

    @pytest.mark.asyncio
    async def test_caller_is_not_blocked_by_the_close(self):
        """The caller must not wait on the closing handshake.

        A charger that has just let a response deadline pass is in no hurry to
        answer a close either; measured against a real one this cost 9.6s of a
        10s budget, all of it with the calling automation held open.
        """
        cp = self._cp()
        cp._retirement_timeout = 30
        entered = asyncio.Event()

        async def never_completes():
            entered.set()
            await asyncio.Event().wait()

        cp._connection.close = AsyncMock(side_effect=never_completes)

        # Fails fast despite the close never finishing.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                cp._call_with_timeout_handling(
                    MagicMock(), call_type="SetChargingProfile", connector_id=1
                ),
                timeout=1,
            )

        # The close really was started, and is still outstanding.
        await asyncio.wait_for(entered.wait(), 1)
        assert len(cp._closing_tasks) == 1
        task = next(iter(cp._closing_tasks))
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_finished_close_stops_being_tracked(self):
        """A completed close releases its own reference."""
        cp = self._cp()

        with pytest.raises(TimeoutError):
            await cp._call_with_timeout_handling(
                MagicMock(), call_type="SetChargingProfile", connector_id=1
            )

        task = next(iter(cp._closing_tasks))
        await task
        assert cp._closing_tasks == set()

    @pytest.mark.asyncio
    async def test_replay_timeout_does_not_drop_the_new_session(self):
        """A replayed command that times out again must not start a drop loop."""
        cp = self._cp()
        cp._replaying = True

        with pytest.raises(TimeoutError):
            await cp._call_with_timeout_handling(
                MagicMock(), call_type="SetChargingProfile", connector_id=1
            )

        cp._connection.close.assert_not_awaited()
        # Still queued, to go out on a reconnect this session did not cause.
        assert not cp._command_queue.is_empty()

    @pytest.mark.asyncio
    async def test_already_closed_connection_is_left_alone(self):
        """Nothing to fail when the transport has already gone."""
        cp = self._cp(state=State.CLOSED)

        with pytest.raises(TimeoutError):
            await cp._call_with_timeout_handling(
                MagicMock(), call_type="SetChargingProfile", connector_id=1
            )

        cp._connection.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_failure_is_not_fatal(self):
        """An unresponsive peer is the expected case, not an error."""
        cp = self._cp()
        cp._connection.close = AsyncMock(side_effect=RuntimeError("peer is gone"))

        # The caller still sees the original timeout, not the close failure.
        with pytest.raises(TimeoutError):
            await cp._call_with_timeout_handling(
                MagicMock(), call_type="SetChargingProfile", connector_id=1
            )

    @pytest.mark.asyncio
    async def test_background_close_is_still_bounded(self):
        """Not awaiting it must not let the close linger forever."""
        cp = self._cp()
        cp._retirement_timeout = 0.05

        async def never_completes():
            await asyncio.Event().wait()

        cp._connection.close = AsyncMock(side_effect=never_completes)

        with pytest.raises(TimeoutError):
            await cp._call_with_timeout_handling(
                MagicMock(), call_type="SetChargingProfile", connector_id=1
            )

        task = next(iter(cp._closing_tasks))
        # Completes on its own via the bound, without being cancelled.
        await asyncio.wait_for(task, timeout=2)
        assert task.done() and not task.cancelled()


class TestReconnectReplay:
    """Replay is driven by the real reconnect path, not a stand-in for it.

    Generic reconnect teardown/exception behaviour belongs to
    test_reconnect_lifecycle.py; what matters here is only that a session
    established by reconnect drains the queue, with the receiver already live.
    """

    @pytest.mark.asyncio
    async def test_reconnect_replays_queued_commands(self, lifecycle):
        """A command queued by a timeout is replayed on the next session."""
        cp, socket, spawn, began, releases = lifecycle
        replayed = []

        async def record(*args, **kwargs):
            replayed.append(args)

        await cp._command_queue.enqueue(
            QueuedCommand(
                call_type="SetChargingProfile",
                call_fn=record,
                args=("profile",),
                connector_id=1,
            )
        )

        old = cp._connection
        spawn(cp.start(), "old-run")
        await began(old)
        new = socket()
        spawn(cp.reconnect(new))
        await began(new)
        await ticks()

        assert replayed == [("profile",)], "queued command was not replayed"
        assert cp._command_queue.is_empty()

    @pytest.mark.asyncio
    async def test_replay_failure_does_not_kill_the_session(self, lifecycle):
        """A command that fails on replay is logged, not fatal to the session."""
        cp, socket, spawn, began, releases = lifecycle
        survivor = []

        async def boom(*args, **kwargs):
            raise RuntimeError("charger still unhappy")

        async def record(*args, **kwargs):
            survivor.append(args)

        await cp._command_queue.enqueue(
            QueuedCommand(call_type="SetChargingProfile", call_fn=boom, connector_id=1)
        )
        await cp._command_queue.enqueue(
            QueuedCommand(
                call_type="ChangeAvailability",
                call_fn=record,
                args=("after",),
                connector_id=1,
            )
        )

        old = cp._connection
        spawn(cp.start(), "old-run")
        await began(old)
        new = socket()
        spawn(cp.reconnect(new))
        await began(new)
        await ticks()

        # The later command still ran, and the new session is still up.
        assert survivor == [("after",)]
        assert cp.status == STATE_OK
        assert cp._connection is new


class TestOcppV201TimeoutIntegration:
    """Tests that ocppv201 call sites are correctly wired to the timeout handler."""

    @pytest.mark.asyncio
    async def test_set_availability_station_level(self):
        """Test set_availability with connector_id=0 (station-level) call site."""
        cp = MagicMock(spec=ChargePointV201)
        cp._call_with_timeout_handling = AsyncMock()
        cp.set_availability = ChargePointV201.set_availability.__get__(
            cp, ChargePointV201
        )

        await cp.set_availability(state=True, connector_id=0)

        cp._call_with_timeout_handling.assert_called_once()
        _, kwargs = cp._call_with_timeout_handling.call_args
        assert kwargs["call_type"] == "ChangeAvailability"
        assert kwargs["connector_id"] == 0

    @pytest.mark.asyncio
    async def test_set_availability_with_evse_mapping(self):
        """Test set_availability when connector_id resolves to an EVSE."""
        cp = MagicMock(spec=ChargePointV201)
        cp._call_with_timeout_handling = AsyncMock()
        cp._global_to_pair = MagicMock(return_value=(5, 1))
        cp.set_availability = ChargePointV201.set_availability.__get__(
            cp, ChargePointV201
        )

        await cp.set_availability(state=False, connector_id=2)

        cp._call_with_timeout_handling.assert_called_once()
        _, kwargs = cp._call_with_timeout_handling.call_args
        assert kwargs["call_type"] == "ChangeAvailability"
        assert kwargs["connector_id"] == 2

    @pytest.mark.asyncio
    async def test_set_availability_without_evse_mapping(self):
        """Test set_availability falls back when no EVSE mapping is available."""
        cp = MagicMock(spec=ChargePointV201)
        cp._call_with_timeout_handling = AsyncMock()
        cp._global_to_pair = MagicMock(side_effect=Exception("no mapping"))
        cp.set_availability = ChargePointV201.set_availability.__get__(
            cp, ChargePointV201
        )

        await cp.set_availability(state=True, connector_id=3)

        cp._call_with_timeout_handling.assert_called_once()
        _, kwargs = cp._call_with_timeout_handling.call_args
        assert kwargs["call_type"] == "ChangeAvailability"
        assert kwargs["connector_id"] == 3

    @pytest.mark.asyncio
    async def test_start_transaction(self):
        """Test start_transaction routes through the timeout handler."""
        cp = MagicMock(spec=ChargePointV201)
        cp._remote_id_tag = "TAG123"
        cp._global_to_pair = MagicMock(return_value=(2, 1))
        fake_resp = MagicMock()
        fake_resp.status = RequestStartStopStatusEnumType.accepted.value
        cp._call_with_timeout_handling = AsyncMock(return_value=fake_resp)
        cp.start_transaction = ChargePointV201.start_transaction.__get__(
            cp, ChargePointV201
        )

        result = await cp.start_transaction(connector_id=1)

        assert result is True
        cp._call_with_timeout_handling.assert_called_once()
        _, kwargs = cp._call_with_timeout_handling.call_args
        assert kwargs["call_type"] == "RequestStartTransaction"

    @pytest.mark.asyncio
    async def test_stop_transaction(self):
        """Test stop_transaction routes through the timeout handler."""
        from custom_components.ocpp.enums import HAChargerSession as csess

        cp = MagicMock(spec=ChargePointV201)
        cp._get_inventory = AsyncMock()
        cp._total_connectors = MagicMock(return_value=1)
        metric = MagicMock()
        metric.value = "12345"
        cp._metrics = {(1, csess.transaction_id.value): metric}
        fake_resp = MagicMock()
        fake_resp.status = RequestStartStopStatusEnumType.accepted.value
        cp._call_with_timeout_handling = AsyncMock(return_value=fake_resp)
        cp.stop_transaction = ChargePointV201.stop_transaction.__get__(
            cp, ChargePointV201
        )

        result = await cp.stop_transaction(connector_id=1)

        assert result is True
        cp._call_with_timeout_handling.assert_called_once()
        _, kwargs = cp._call_with_timeout_handling.call_args
        assert kwargs["call_type"] == "RequestStopTransaction"
