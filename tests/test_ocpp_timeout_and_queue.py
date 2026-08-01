"""Tests for OCPP timeout handling and command queue."""

from unittest.mock import MagicMock, AsyncMock
import pytest
from websockets.exceptions import WebSocketException
from custom_components.ocpp.command_queue import CommandQueue, QueuedCommand
from custom_components.ocpp.chargepoint import ChargePoint
from custom_components.ocpp.ocppv201 import ChargePoint as ChargePointV201
from ocpp.v201.enums import RequestStartStopStatusEnumType

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


class TestReconnectExceptionHandling:
    """Tests for reconnect() exception handling branches."""

    def _make_chargepoint(self, monkeypatch, monitor_side_effect):
        from custom_components.ocpp.enums import HAChargerStatuses as cstat

        chargepoint = MagicMock(spec=ChargePoint)
        chargepoint.id = "test_charger"
        chargepoint.stop = AsyncMock()
        chargepoint._replay_queue = AsyncMock()
        chargepoint.monitor_connection = AsyncMock(side_effect=monitor_side_effect)
        chargepoint._metrics = {(0, cstat.reconnects.value): MagicMock(value=0)}

        import custom_components.ocpp.chargepoint as cp_module

        monkeypatch.setattr(cp_module.cp, "start", AsyncMock(return_value=None))

        chargepoint.reconnect = ChargePoint.reconnect.__get__(chargepoint, ChargePoint)
        return chargepoint

    @pytest.mark.asyncio
    async def test_reconnect_handles_timeout_error(self, monkeypatch):
        """Test reconnect() swallows TimeoutError from gathered tasks."""
        chargepoint = self._make_chargepoint(monkeypatch, TimeoutError("ping timeout"))

        await chargepoint.reconnect(MagicMock())

        # stop() called once up-front and once in the finally block
        assert chargepoint.stop.call_count == 2
        chargepoint._replay_queue.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_handles_websocket_exception(self, monkeypatch):
        """Test reconnect() logs and swallows WebSocketException."""
        chargepoint = self._make_chargepoint(
            monkeypatch, WebSocketException("connection closed")
        )

        await chargepoint.reconnect(MagicMock())

        assert chargepoint.stop.call_count == 2

    @pytest.mark.asyncio
    async def test_reconnect_handles_generic_exception(self, monkeypatch):
        """Test reconnect() logs and swallows unexpected exceptions."""
        chargepoint = self._make_chargepoint(monkeypatch, RuntimeError("boom"))

        await chargepoint.reconnect(MagicMock())

        assert chargepoint.stop.call_count == 2


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
