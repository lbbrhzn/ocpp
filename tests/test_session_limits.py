"""Contract and race tests for transaction-scoped current limits."""

# ruff: noqa: D102, D103, D107

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.const import STATE_OK
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from ocpp.exceptions import InternalError, NotSupportedError
from ocpp.messages import CallError, CallResult
from ocpp.v16 import call, call_result
import pytest

from custom_components.ocpp import chargepoint as chargepoint_module
from custom_components.ocpp import session as session_module
from custom_components.ocpp.chargepoint import ChargePoint as BaseChargePoint
from custom_components.ocpp.const import DATA_UPDATED
from custom_components.ocpp.enums import Profiles
from custom_components.ocpp.session import (
    CallOutcome,
    ClassifiedCallResult,
    SESSION_PROFILE_MAX_ID,
    SESSION_PROFILE_MIN_ID,
    SessionLimitController,
    SessionToken,
    SlotState,
    custom_profile_ids,
    session_profile_id,
)


def _result(
    status: str = "Accepted", outcome: CallOutcome = CallOutcome.SUCCESS
) -> ClassifiedCallResult:
    return ClassifiedCallResult(outcome, SimpleNamespace(status=status))


class FakeChargePoint:
    """Small protocol adapter exposing only the controller contract."""

    def __init__(self, *, version: str = "1.6") -> None:
        self.status = STATE_OK
        self.post_connect_success = True
        self.supported_features = Profiles.SMART
        self.session_controller = None
        self._ocpp_version = version
        self.results: list[ClassifiedCallResult] = []
        self.requests: list[dict] = []
        self.station_clear = True
        self.call_entered: asyncio.Event | None = None
        self.call_release: asyncio.Event | None = None
        # Phase the real call_classified would report if cancelled mid-call.
        self.cancel_phase: CallOutcome | None = None

    def transaction_is_unsafe(self, _connector: int) -> bool:
        return False

    async def prepare_session_limit(
        self,
        connector_id: int,
        amps: float,
        *,
        source_watts: float | None = None,
    ) -> dict:
        value = source_watts if source_watts is not None else amps
        return {
            "target": connector_id,
            "amps": amps,
            "unit": "W" if source_watts is not None else "A",
            "value": value,
            "stack_level": 2,
        }

    def build_session_limit_request(
        self,
        connector_id: int,
        transaction_id: int | str,
        profile_id: int,
        prepared: dict,
    ) -> dict:
        return {
            "kind": "set",
            "connector": connector_id,
            "transaction": transaction_id,
            "profile_id": profile_id,
            "prepared": prepared,
        }

    def build_session_clear_request(self, profile_id: int) -> dict:
        return {"kind": "clear", "profile_id": profile_id}

    def build_custom_profile_request(self, connector_id: int, profile: dict) -> dict:
        return {"kind": "custom", "connector": connector_id, "profile": profile}

    async def call_classified(self, request: dict) -> ClassifiedCallResult:
        self.requests.append(request)
        if self.call_entered is not None:
            self.call_entered.set()
        if self.call_release is not None:
            try:
                await self.call_release.wait()
            except asyncio.CancelledError as ex:
                if self.cancel_phase is not None:
                    ex.classified_result = ClassifiedCallResult(
                        self.cancel_phase, error=ex
                    )
                raise
        return self.results.pop(0)

    async def clear_profile(self) -> bool:
        self.requests.append({"kind": "station_clear"})
        return self.station_clear


class ClassifiedCallAdapter:
    """Minimal receiver for the real phase-aware call implementation."""

    call_classified = BaseChargePoint.call_classified

    def __init__(self) -> None:
        self._ocpp_version = "1.6"
        self._call_result = call_result
        self._call_lock = asyncio.Lock()
        self._response_timeout = 1
        self._unique_id_generator = lambda: "request-id"
        self._send = AsyncMock()
        self._get_specific_response = AsyncMock(
            return_value=CallResult("request-id", {"status": "Accepted"})
        )


async def _controller(hass, *, version: str = "1.6", stored=None):
    controller = SessionLimitController(hass, "entry", "CP_A", "charger", 32, 2)
    controller._store.async_load = AsyncMock(return_value=stored)
    controller._store.async_delay_save = Mock()
    cp = FakeChargePoint(version=version)
    await controller.async_bind(cp)
    await controller.async_post_connect_ready(False)
    return controller, cp


def test_reserved_profile_id_formula_and_bounds():
    """Each connector has two stable ids and nothing escapes the reservation."""
    assert session_profile_id(1, 0) == 4010
    assert session_profile_id(99, 1) == 4991
    assert session_profile_id(37, 0) >= SESSION_PROFILE_MIN_ID
    assert session_profile_id(37, 1) <= SESSION_PROFILE_MAX_ID
    with pytest.raises(ValueError):
        session_profile_id(0, 0)
    with pytest.raises(ValueError):
        session_profile_id(100, 0)
    with pytest.raises(ValueError):
        session_profile_id(1, 2)
    assert custom_profile_ids(
        {"chargingProfileId": "invalid", "charging_profile_id": 7, "id": 4010}
    ) == {7, 4010}


async def test_controller_notifications_target_only_registered_session_entities(hass):
    controller, _cp = await _controller(hass)
    received = []
    unregister_signal = async_dispatcher_connect(
        hass, DATA_UPDATED, lambda *args: received.append(args)
    )
    unregister_entity = controller.register_entity(1, "number.charger_session")

    controller.on_transaction_start(1, 73, 1)

    assert received[-1] == ({"number.charger_session"},)
    unregister_entity()
    unregister_signal()
    await controller.async_shutdown()


async def test_set_is_bound_and_published_only_after_accepted(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.append(_result())

    await controller.async_set_limit(1, token, 16)

    assert cp.requests == [
        {
            "kind": "set",
            "connector": 1,
            "transaction": 73,
            "profile_id": 4010,
            "prepared": {
                "target": 1,
                "amps": 16.0,
                "unit": "A",
                "value": 16.0,
                "stack_level": 2,
            },
        }
    ]
    assert controller.value(1) == 16
    assert controller.is_available(1)
    await controller.async_shutdown()


async def test_rejected_update_keeps_old_confirmed_value(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.extend([_result(), _result("Rejected")])
    await controller.async_set_limit(1, token, 16)

    with pytest.raises(HomeAssistantError, match="rejected"):
        await controller.async_set_limit(1, token, 10)

    slot = controller._connector(1).slots[0]
    assert slot.state is SlotState.OWNED
    assert controller.value(1) == 16
    assert controller.is_available(1)
    await controller.async_shutdown()


async def test_uncertain_update_is_not_republished(hass, monkeypatch, caplog):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.extend(
        [_result(), ClassifiedCallResult(CallOutcome.TIMEOUT, error=TimeoutError())]
    )
    await controller.async_set_limit(1, token, 16)
    scheduled = []
    monkeypatch.setattr(
        controller,
        "_schedule_cleanup",
        lambda connector, slot: scheduled.append((connector, slot)),
    )

    with (
        caplog.at_level("WARNING", logger="custom_components.ocpp"),
        pytest.raises(HomeAssistantError, match="uncertain"),
    ):
        await controller.async_set_limit(1, token, 10)

    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert controller.value(1) == 32
    assert not controller.is_available(1)
    assert scheduled == [(1, 0)]
    failure_logs = [
        record.getMessage()
        for record in caplog.records
        if "session-profile set failed" in record.getMessage()
    ]
    assert len(failure_logs) == 1
    assert "profile_id=4010" in failure_logs[0]
    assert "outcome=timeout" in failure_logs[0]
    assert "error=TimeoutError" in failure_logs[0]
    await controller.async_shutdown()


async def test_stop_between_set_and_result_enters_exact_cleanup(hass, monkeypatch):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.append(_result())
    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()
    scheduled = []
    monkeypatch.setattr(
        controller,
        "_schedule_cleanup",
        lambda connector, slot: scheduled.append((connector, slot)),
    )

    task = asyncio.create_task(controller.async_set_limit(1, token, 16))
    await cp.call_entered.wait()
    controller.on_transaction_end(1, 73)
    cp.call_release.set()
    await task

    assert controller._connector(1).slots[0].state is SlotState.PENDING_CLEAR
    assert scheduled
    assert not controller.is_available(1)
    await controller.async_shutdown()


async def test_next_transaction_uses_spare_and_two_dirty_slots_exhaust(
    hass, monkeypatch
):
    controller, cp = await _controller(hass)
    monkeypatch.setattr(controller, "_schedule_cleanup", lambda *_args: None)
    controller.on_transaction_start(1, 1, 1)
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 16)
    controller.on_transaction_end(1, 1)

    controller.on_transaction_start(1, 2, 1)
    assert controller._connector(1).active_slot == 1
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 12)
    assert cp.requests[-1]["profile_id"] == 4011
    controller.on_transaction_end(1, 2)
    controller.on_transaction_start(1, 3, 1)

    assert controller._connector(1).active_slot is None
    assert not controller.is_available(1)
    await controller.async_shutdown()


async def test_maximum_exact_clears_before_publication(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.extend([_result(), _result("Unknown")])
    await controller.async_set_limit(1, token, 16)
    await controller.async_set_limit(1, token, 32)

    assert cp.requests[-1] == {"kind": "clear", "profile_id": 4010}
    assert controller.value(1) == 32
    assert controller._connector(1).slots[0].state is SlotState.CLEAN
    await controller.async_shutdown()


async def test_custom_profile_is_set_before_managed_profile_is_cleared(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    cp.results.extend([_result(), _result(), _result()])
    await controller.async_set_limit(1, controller.current_token(1), 16)

    profile = {"chargingProfilePurpose": "TxProfile", "chargingProfileId": 9}
    await controller.async_custom_profile(1, profile)

    assert [request["kind"] for request in cp.requests] == ["set", "custom", "clear"]
    assert cp.requests[-1]["profile_id"] == 4010
    assert controller.value(1) == 32
    await controller.async_shutdown()


async def test_cancelled_custom_set_fences_managed_value(hass):
    """A possibly-applied custom profile invalidates the managed display."""
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 16)
    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()

    custom = asyncio.create_task(
        controller.async_custom_profile(
            1, {"chargingProfilePurpose": "TxProfile", "chargingProfileId": 9}
        )
    )
    await cp.call_entered.wait()
    custom.cancel()

    with pytest.raises(asyncio.CancelledError):
        await custom
    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert controller.value(1) == 32
    assert not controller.is_available(1)
    await controller.async_shutdown()


async def test_201_broad_clear_classifies_each_owned_id(hass, monkeypatch):
    controller, cp = await _controller(hass, version="2.0.1")
    monkeypatch.setattr(controller, "_schedule_cleanup", lambda *_args: None)
    connector = controller._connector(1)
    for index, slot in enumerate(connector.slots):
        slot.state = SlotState.UNCERTAIN
        slot.transaction_id = index + 1
    cp.results.extend([_result("Accepted"), _result("Rejected")])

    with pytest.raises(HomeAssistantError, match="4011"):
        await controller.async_clear_profiles()

    assert [request["kind"] for request in cp.requests] == [
        "station_clear",
        "clear",
        "clear",
    ]
    assert connector.slots[0].state is SlotState.CLEAN
    assert connector.slots[1].state is SlotState.UNCERTAIN
    await controller.async_shutdown()


async def test_disconnect_cancels_broad_clear_and_old_reply_cannot_classify(hass):
    controller, cp = await _controller(hass, version="2.0.1")
    connector = controller._connector(1)
    connector.slots[0].state = SlotState.UNCERTAIN
    connector.slots[0].transaction_id = 73
    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()
    cp.results.append(_result())

    clear = asyncio.create_task(controller.async_clear_profiles())
    await cp.call_entered.wait()
    controller.on_disconnect()

    with pytest.raises(HomeAssistantError, match="connection changed"):
        await clear
    assert not clear.cancelled()
    assert connector.slots[0].state is SlotState.UNCERTAIN
    assert controller._barrier_owner is None
    assert not controller._owned_tasks
    await controller.async_shutdown()


async def test_stale_barrier_generation_reports_failure_not_cancellation(hass):
    controller, cp = await _controller(hass, version="2.0.1")
    owner = object()
    controller._barrier_owner = owner

    with pytest.raises(HomeAssistantError, match="connection changed"):
        controller._assert_barrier_connection(owner, cp, controller._generation + 1)

    await controller.async_shutdown()


async def test_broad_clear_barrier_makes_new_session_unavailable(hass):
    controller, cp = await _controller(hass, version="2.0.1")
    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.UNCERTAIN
    slot.transaction_id = 72
    cp.results.append(_result())

    clear = asyncio.create_task(controller.async_clear_profiles())
    await cp.call_entered.wait()
    controller.on_transaction_start(1, 73, 1)

    assert not controller.is_available(1)
    cp.call_release.set()
    await clear
    assert controller.is_available(1)
    await controller.async_shutdown()


async def test_drain_timeout_recovers_availability_after_operation_releases(
    hass, monkeypatch
):
    controller, _cp = await _controller(hass, version="2.0.1")
    controller.on_transaction_start(1, 73, 1)
    op = await controller._admit(1, "held")
    monkeypatch.setattr(session_module, "SESSION_DRAIN_TIMEOUT", 0.01)

    with pytest.raises(HomeAssistantError, match="admission remains closed"):
        await controller.async_clear_profiles()

    assert controller._barrier_owner is not None
    assert not controller.is_available(1)
    await controller._release(1, op)
    assert controller._barrier_owner is None
    assert controller.is_available(1)
    await controller.async_shutdown()


async def test_invalid_persisted_id_is_quarantined_and_never_cleared(hass):
    stored = {
        "records": [
            {
                "connector_id": 1,
                "slot": 0,
                "profile_id": 1000,
                "state": "owned",
                "transaction_id": 1,
                "generation": 1,
                "retry_count": 0,
            }
        ]
    }
    controller, cp = await _controller(hass, stored=stored)

    assert controller._quarantine[1]
    assert cp.requests == []
    controller.on_boot_notification()
    assert not controller._quarantine
    await controller.async_shutdown()


async def test_store_type_confusion_and_oversize_input_are_bounded(hass):
    malformed = {
        "connector_id": True,
        "slot": 0,
        "state": "owned",
        "transaction_id": 1,
        "generation": 1,
        "confirmed_amps": 16,
        "requested_amps": 16,
        "transmitted_unit": "A",
        "transmitted_value": 16,
        "conversion_voltage": None,
        "conversion_phases": None,
        "retry_count": 0,
    }
    controller, cp = await _controller(hass, stored={"records": [malformed] * 100})

    # Two configured connectors have four real slots. The extra marker plus
    # four bounded validations prevents a corrupt Store becoming a log/memory
    # amplification vector.
    assert sum(map(len, controller._quarantine.values())) == 5
    assert cp.requests == []
    await controller.async_shutdown()


async def test_invalid_prepared_wire_value_never_dirties_a_slot(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.prepare_session_limit = AsyncMock(
        return_value={
            "target": 1,
            "amps": 16,
            "unit": "A",
            "value": float("inf"),
            "stack_level": 2,
        }
    )

    with pytest.raises(HomeAssistantError, match="prepared session limit is invalid"):
        await controller.async_set_limit(1, token, 16)

    assert cp.requests == []
    assert controller._connector(1).slots[0].state is SlotState.CLEAN
    assert controller.is_available(1)
    await controller.async_shutdown()


async def test_custom_profile_cannot_default_to_connector_one_on_multi_charger(hass):
    controller, cp = await _controller(hass)

    with pytest.raises(HomeAssistantError, match="between 1 and 2"):
        await controller.async_custom_profile(
            0, {"chargingProfilePurpose": "TxProfile", "id": 9}
        )

    assert cp.requests == []
    await controller.async_shutdown()


async def test_cleanup_records_are_loaded_exactly_once_under_concurrency(hass):
    controller = SessionLimitController(hass, "entry", "CP_A", "charger", 32, 2)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_load():
        entered.set()
        await release.wait()
        return None

    controller._store.async_load = AsyncMock(side_effect=slow_load)
    first = asyncio.create_task(controller.async_load())
    await entered.wait()
    second = asyncio.create_task(controller.async_load())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert controller._store.async_load.await_count == 1


async def test_cleanup_retries_stop_for_the_connection_generation(hass, monkeypatch):
    controller, cp = await _controller(hass)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.UNCERTAIN
    slot.transaction_id = 73
    cp.results.extend(
        [_result("Rejected")] * session_module.SESSION_RETRY_ATTEMPTS_PER_GENERATION
    )
    monkeypatch.setattr(session_module, "SESSION_RETRY_MIN", 0)
    monkeypatch.setattr(session_module, "SESSION_RETRY_MAX", 0)

    await controller._cleanup_loop(1, 0)

    assert len(cp.requests) == session_module.SESSION_RETRY_ATTEMPTS_PER_GENERATION
    assert (1, 0, controller.generation) in controller._cleanup_exhausted
    controller._schedule_cleanup(1, 0)
    assert (1, 0) not in controller._cleanup_tasks
    assert slot.dirty
    await controller.async_shutdown()


async def test_rejected_maximum_clear_keeps_confirmed_limit(hass, monkeypatch):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.extend([_result(), _result("Rejected")])
    await controller.async_set_limit(1, token, 16)
    scheduled = []
    monkeypatch.setattr(
        controller,
        "_schedule_cleanup",
        lambda connector, slot: scheduled.append((connector, slot)),
    )

    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await controller.async_set_limit(1, token, 32)

    assert controller._connector(1).slots[0].state is SlotState.OWNED
    assert controller.value(1) == 16
    assert controller.is_available(1)
    assert scheduled == []
    await controller.async_shutdown()


async def test_ambiguous_reset_clear_fences_owned_value(hass, monkeypatch):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.extend(
        [_result(), ClassifiedCallResult(CallOutcome.TIMEOUT, error=TimeoutError())]
    )
    await controller.async_set_limit(1, token, 16)
    scheduled = []
    monkeypatch.setattr(
        controller,
        "_schedule_cleanup",
        lambda connector, slot: scheduled.append((connector, slot)),
    )

    with pytest.raises(HomeAssistantError, match="could not clear"):
        await controller.async_reset(1)

    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert controller.value(1) == 32
    assert not controller.is_available(1)
    assert scheduled == [(1, 0)]
    await controller.async_shutdown()


async def test_ambiguous_v16_broad_clear_fences_every_owned_value(hass, monkeypatch):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.append(_result())
    await controller.async_set_limit(1, token, 16)
    cp.station_clear = False
    scheduled = []
    monkeypatch.setattr(
        controller,
        "_schedule_cleanup",
        lambda connector, slot: scheduled.append((connector, slot)),
    )

    with pytest.raises(HomeAssistantError, match="partial"):
        await controller.async_clear_profiles()

    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert controller.value(1) == 32
    assert not controller.is_available(1)
    assert scheduled == [(1, 0)]
    await controller.async_shutdown()


async def test_clear_request_build_failure_does_not_discard_owned_truth(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.append(_result())
    await controller.async_set_limit(1, token, 16)
    cp.build_session_clear_request = Mock(side_effect=ValueError("bad clear"))

    with pytest.raises(HomeAssistantError, match="could not build"):
        await controller.async_set_limit(1, token, 32)

    assert controller._connector(1).slots[0].state is SlotState.OWNED
    assert controller.value(1) == 16
    assert controller.is_available(1)
    await controller.async_shutdown()


async def test_classified_call_distinguishes_request_reply_and_remote_errors():
    adapter = ClassifiedCallAdapter()
    invalid = call.SetChargingProfile(
        connector_id=1,
        cs_charging_profiles={"charging_profile_id": "not-an-integer"},
    )
    result = await adapter.call_classified(invalid)
    assert result.outcome is CallOutcome.LOCAL_REQUEST_INVALID
    adapter._send.assert_not_awaited()

    valid = call.ClearChargingProfile(id=4010)
    adapter._get_specific_response.side_effect = NotSupportedError()
    result = await adapter.call_classified(valid)
    assert result.outcome is CallOutcome.REMOTE_VALIDATION_ERROR

    adapter._get_specific_response.side_effect = InternalError()
    result = await adapter.call_classified(valid)
    assert result.outcome is CallOutcome.REMOTE_ERROR

    adapter._get_specific_response.side_effect = None
    adapter._get_specific_response.return_value = CallResult("request-id", {})
    result = await adapter.call_classified(valid)
    assert result.outcome is CallOutcome.RESPONSE_INVALID


async def test_cancelled_before_send_is_clean_but_cancelled_after_send_is_uncertain():
    adapter = ClassifiedCallAdapter()
    request = call.ClearChargingProfile(id=4010)
    await adapter._call_lock.acquire()
    queued = asyncio.create_task(adapter.call_classified(request))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await queued
    result = cancelled.value.classified_result
    assert result.outcome is CallOutcome.LOCAL_REQUEST_INVALID
    adapter._call_lock.release()

    waiting = asyncio.Event()

    async def _wait_for_reply(*_args):
        waiting.set()
        await asyncio.Event().wait()

    adapter._get_specific_response.side_effect = _wait_for_reply
    sent = asyncio.create_task(adapter.call_classified(request))
    await waiting.wait()
    sent.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await sent
    result = cancelled.value.classified_result
    assert result.outcome is CallOutcome.TRANSPORT_FAILURE
    assert result.uncertain


async def test_cancelled_during_reply_validation_is_uncertain(monkeypatch):
    adapter = ClassifiedCallAdapter()
    request = call.ClearChargingProfile(id=4010)
    real_validate = chargepoint_module.validate_payload
    reply_validation = asyncio.Event()
    never = asyncio.Event()
    calls = 0

    async def blocking_reply_validation(message, version):
        nonlocal calls
        calls += 1
        if calls == 2:
            reply_validation.set()
            await never.wait()
        return await real_validate(message, version)

    monkeypatch.setattr(
        chargepoint_module, "validate_payload", blocking_reply_validation
    )
    task = asyncio.create_task(adapter.call_classified(request))
    await reply_validation.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    result = cancelled.value.classified_result

    assert result.outcome is CallOutcome.RESPONSE_INVALID
    assert result.uncertain


def _record(**overrides):
    base = {
        "connector_id": 1,
        "slot": 0,
        "state": "owned",
        "transaction_id": 73,
        "generation": 1,
        "confirmed_amps": 16,
        "requested_amps": 16,
        "transmitted_unit": "A",
        "transmitted_value": 16,
        "conversion_voltage": None,
        "conversion_phases": None,
        "retry_count": 0,
    }
    base.update(overrides)
    return base


def _prepared(**overrides):
    base = {"target": 1, "amps": 16, "unit": "A", "value": 16.0, "stack_level": 2}
    base.update(overrides)
    return base


async def _blocked(cp, coro):
    """Start *coro* and return its task once its charger call is in flight."""
    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()
    cp.results.append(_result())
    task = asyncio.create_task(coro)
    await cp.call_entered.wait()
    return task


def _cancelled_conclusively():
    error = asyncio.CancelledError()
    error.classified_result = ClassifiedCallResult(CallOutcome.LOCAL_REQUEST_INVALID)
    return error


async def test_valid_records_restore_slot_state_without_side_effects(hass):
    controller = SessionLimitController(hass, "entry", "CP_A", "charger", 32, 2)
    controller._store.async_load = AsyncMock(
        return_value={
            "records": [
                _record(profile_id=4010, conversion_voltage=230, conversion_phases=3),
                _record(
                    slot=1, state="pending_set", confirmed_amps=None, retry_count=3
                ),
            ]
        }
    )
    controller._store.async_delay_save = Mock()

    await controller.async_load()

    first, second = controller._connector(1).slots
    assert first.state is SlotState.OWNED
    assert first.confirmed_amps == 16.0
    assert first.conversion_voltage == 230.0
    assert first.conversion_phases == 3
    assert second.state is SlotState.PENDING_SET
    assert second.retry_count == 3
    assert not controller._quarantine
    assert not controller._cleanup_tasks


async def test_store_load_failure_and_non_list_records_are_tolerated(hass, caplog):
    controller = SessionLimitController(hass, "entry", "CP_A", "charger", 32, 2)
    controller._store.async_load = AsyncMock(side_effect=OSError("disk"))
    controller._store.async_delay_save = Mock()
    await controller.async_load()
    assert controller._loaded
    assert not controller._quarantine
    assert "failed to load session-limit cleanup records" in caplog.text

    other = SessionLimitController(hass, "entry", "CP_B", "charger-b", 32, 2)
    other._store.async_load = AsyncMock(return_value={"records": "nope"})
    other._store.async_delay_save = Mock()
    await other.async_load()
    assert other._quarantine[0] == [{"reason": "records is not a list"}]


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("not-a-record", "record is not an object"),
        ({"connector_id": 1}, "missing fields"),
        (_record(connector_id=0), "connector is not configured"),
        (_record(connector_id="1"), "connector has invalid integer type"),
        (_record(slot=2), "slot is not 0 or 1"),
        (_record(state="clean"), "clean records must not be persisted"),
        (_record(state="bogus"), "not a valid SlotState"),
        (_record(transaction_id=""), "transaction id has invalid type"),
        (_record(transaction_id=True), "transaction id has invalid type"),
        (_record(generation=-1), "generation is negative"),
        (_record(transmitted_unit="V"), "transmitted unit is invalid"),
        (_record(profile_id=4011), "diagnostic profile id does not match"),
        (_record(confirmed_amps=-1), "not finite and non-negative"),
        (_record(confirmed_amps="16"), "numeric value has invalid type"),
        (_record(conversion_phases=4), "conversion phase count is invalid"),
        (_record(retry_count=1001), "retry count is outside"),
        (_record(transmitted_value=None), "must appear together"),
        (_record(confirmed_amps=None), "owned record has no confirmed current"),
        (
            _record(state="pending_set", requested_amps=None),
            "pending set has no requested current",
        ),
    ],
)
async def test_each_invalid_record_is_quarantined_with_its_reason(hass, record, reason):
    controller, cp = await _controller(hass, stored={"records": [record]})
    reasons = [
        entry["reason"]
        for entries in controller._quarantine.values()
        for entry in entries
    ]
    assert len(reasons) == 1
    assert reason in reasons[0]
    assert cp.requests == []
    await controller.async_shutdown()


async def test_duplicate_slot_records_keep_only_the_first(hass):
    controller, _cp = await _controller(
        hass, stored={"records": [_record(), _record(confirmed_amps=20)]}
    )
    assert controller._connector(1).slots[0].confirmed_amps == 16.0
    assert controller._quarantine[1][0]["reason"] == "slot is claimed more than once"
    await controller.async_shutdown()


async def test_connector_and_current_arguments_are_validated(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    for bad in (True, "x", 1.5):
        with pytest.raises(HomeAssistantError, match="must be an integer"):
            await controller.async_set_limit(bad, token, 16)
    with pytest.raises(HomeAssistantError, match="between 1 and 2"):
        await controller.async_set_limit(3, token, 16)
    with pytest.raises(HomeAssistantError, match="session changed"):
        await controller.async_set_limit(2, token, 16)
    with pytest.raises(HomeAssistantError, match="must be numeric"):
        await controller.async_set_limit(1, token, True)
    for amps in (-1, 40, float("nan")):
        with pytest.raises(HomeAssistantError, match="between 0 and 32"):
            await controller.async_set_limit(1, token, amps)
    assert cp.requests == []
    await controller.async_shutdown()


async def test_admission_refusals_and_availability_guards(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    stale = SessionToken(token.generation, 99, 1, 1)
    with pytest.raises(HomeAssistantError, match="session changed"):
        await controller.async_set_limit(1, stale, 16)
    controller._barrier_owner = object()
    with pytest.raises(HomeAssistantError, match="operation pending"):
        await controller.async_set_limit(1, token, 16)
    controller._barrier_owner = None
    cp.supported_features = 0
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await controller.async_set_limit(1, token, 16)
    cp.supported_features = Profiles.SMART
    assert controller.is_available(1)
    cp.transaction_is_unsafe = lambda _connector: True
    assert not controller.is_available(1)
    controller._cp = None
    assert not controller.is_available(1)
    controller._cp = cp
    assert cp.requests == []
    await controller.async_shutdown()


async def test_set_without_a_bound_charger_or_with_failing_preparation(
    hass, monkeypatch
):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.prepare_session_limit = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError, match="could not prepare session limit"):
        await controller.async_set_limit(1, token, 16)
    monkeypatch.setattr(controller, "is_available", lambda _connector: True)
    controller._cp = None
    with pytest.raises(HomeAssistantError, match="not connected"):
        await controller.async_set_limit(1, token, 16)
    controller._cp = cp
    await controller.async_shutdown()


@pytest.mark.parametrize(
    ("prepared", "message"),
    [
        ({"amps": 40}, "outside the configured current range"),
        ({"unit": "V"}, "prepared session limit is invalid"),
        ({"value": -1}, "negative"),
        ({"conversion_voltage": 0}, "conversion voltage is invalid"),
        ({"conversion_phases": 4}, "conversion phase count is invalid"),
        ({"target": 2}, "target changed during preparation"),
    ],
)
async def test_prepared_values_are_validated(hass, prepared, message):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.prepare_session_limit = AsyncMock(return_value=_prepared(**prepared))
    with pytest.raises(HomeAssistantError, match=message):
        await controller.async_set_limit(1, token, 16)
    assert cp.requests == []
    assert controller._connector(1).slots[0].state is SlotState.CLEAN
    await controller.async_shutdown()


async def test_session_end_or_supersession_during_preparation_is_refused(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)

    async def end_while_preparing(*_args, **_kwargs):
        controller.on_transaction_end(1, 73)
        return _prepared()

    cp.prepare_session_limit = end_while_preparing
    with pytest.raises(HomeAssistantError, match="ended during preparation"):
        await controller.async_set_limit(1, token, 16)

    controller.on_transaction_start(1, 74, 1)
    token = controller.current_token(1)

    async def supersede_while_preparing(*_args, **_kwargs):
        controller._connector(1).admitted_operation = object()
        return _prepared()

    cp.prepare_session_limit = supersede_while_preparing
    with pytest.raises(HomeAssistantError, match="superseded"):
        await controller.async_set_limit(1, token, 16)
    controller._connector(1).admitted_operation = None
    assert cp.requests == []
    await controller.async_shutdown()


async def test_set_selects_a_spare_slot_or_reports_exhaustion(hass, monkeypatch):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    connector = controller._connector(1)
    connector.active_slot = None
    connector.slots[0].state = SlotState.UNCERTAIN
    cp.results.append(_result())
    await controller.async_set_limit(1, token, 16)
    assert connector.active_slot == 1
    assert cp.requests[-1]["profile_id"] == 4011

    monkeypatch.setattr(controller, "is_available", lambda _connector: True)
    connector.active_slot = None
    connector.slots[1].state = SlotState.UNCERTAIN
    with pytest.raises(HomeAssistantError, match="need repair"):
        await controller.async_set_limit(1, token, 12)
    connector.active_slot = 0
    with pytest.raises(HomeAssistantError, match="profile is uncertain"):
        await controller.async_set_limit(1, token, 12)
    await controller.async_shutdown()


async def test_cancelled_set_restores_or_fences_by_phase(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    slot = controller._connector(1).slots[0]

    task = await _blocked(cp, controller.async_set_limit(1, token, 16))
    cp.cancel_phase = CallOutcome.LOCAL_REQUEST_INVALID  # frame never left
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.CLEAN
    assert controller.is_available(1)

    task = await _blocked(cp, controller.async_set_limit(1, token, 16))
    cp.cancel_phase = None  # cancelled after the send: nothing is known
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.UNCERTAIN
    assert (1, 0) in controller._cleanup_tasks
    await controller.async_shutdown()


@pytest.mark.parametrize(
    ("owned_first", "outcome", "expected", "reported"),
    [
        (False, CallOutcome.TIMEOUT, SlotState.UNCERTAIN, "timeout"),
        (True, "Rejected", SlotState.PENDING_CLEAR, "rejected"),
        (False, "Rejected", SlotState.CLEAN, "rejected"),
        (False, "Accepted", SlotState.PENDING_CLEAR, None),
    ],
)
async def test_stale_set_outcomes_are_settled_by_prior_truth(
    hass, monkeypatch, owned_first, outcome, expected, reported
):
    controller, cp = await _controller(hass)
    monkeypatch.setattr(controller, "_schedule_cleanup", lambda *_args: None)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    if owned_first:
        cp.results.append(_result())
        await controller.async_set_limit(1, token, 16)
    cp.results.append(
        ClassifiedCallResult(outcome, error=TimeoutError())
        if isinstance(outcome, CallOutcome)
        else _result(outcome)
    )
    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()
    task = asyncio.create_task(controller.async_set_limit(1, token, 12))
    await cp.call_entered.wait()
    controller.on_transaction_end(1, 73)
    cp.call_release.set()
    if reported is None:
        await task
    else:
        with pytest.raises(HomeAssistantError, match=f"not applied.*{reported}"):
            await task
    assert controller._connector(1).slots[0].state is expected
    await controller.async_shutdown()


async def test_maximum_with_nothing_owned_sends_nothing(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    await controller.async_set_limit(1, controller.current_token(1), 32)
    assert cp.requests == []
    assert controller.value(1) == 32
    await controller.async_shutdown()


async def test_cancelled_maximum_clear_keeps_or_fences_the_owned_profile(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.append(_result())
    await controller.async_set_limit(1, token, 16)
    slot = controller._connector(1).slots[0]

    task = await _blocked(cp, controller.async_set_limit(1, token, 32))
    cp.cancel_phase = CallOutcome.LOCAL_REQUEST_INVALID
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.OWNED
    assert controller.value(1) == 16

    task = await _blocked(cp, controller.async_set_limit(1, token, 32))
    cp.cancel_phase = None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.UNCERTAIN
    assert not controller.is_available(1)
    await controller.async_shutdown()


async def test_uncertain_maximum_clear_fences_the_owned_profile(hass, monkeypatch):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.extend(
        [_result(), ClassifiedCallResult(CallOutcome.TIMEOUT, error=TimeoutError())]
    )
    await controller.async_set_limit(1, token, 16)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await controller.async_set_limit(1, token, 32)
    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert scheduled == [(1, 0)]
    await controller.async_shutdown()


async def test_cleanup_loop_waits_out_a_barrier_then_clears(hass, monkeypatch):
    controller, cp = await _controller(hass)
    monkeypatch.setattr(session_module, "SESSION_RETRY_MIN", 0)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73
    controller._barrier_owner = object()
    cp.results.append(_result())
    task = asyncio.create_task(controller._cleanup_loop(1, 0))
    await asyncio.sleep(0.01)
    assert cp.requests == []
    controller._barrier_owner = None
    await task
    assert cp.requests == [{"kind": "clear", "profile_id": 4010}]
    assert slot.state is SlotState.CLEAN
    await controller.async_shutdown()


async def test_cleanup_scheduling_respects_readiness_and_exhaustion(hass):
    controller, cp = await _controller(hass)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    controller._ready = False
    controller._schedule_all_cleanup()
    controller._schedule_cleanup(1, 0)
    assert not controller._cleanup_tasks
    await controller._cleanup_loop(1, 0)
    assert cp.requests == []
    controller._ready = True
    controller._cleanup_exhausted.add((1, 0, controller.generation))
    controller._schedule_cleanup(1, 0)
    assert not controller._cleanup_tasks
    await controller.async_shutdown()


async def test_cleanup_build_failures_count_as_attempts(hass, monkeypatch, caplog):
    controller, cp = await _controller(hass)
    monkeypatch.setattr(session_module, "SESSION_RETRY_MIN", 0)
    monkeypatch.setattr(session_module, "SESSION_RETRY_MAX", 0)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73
    cp.build_session_clear_request = Mock(side_effect=ValueError("no id"))
    await controller._cleanup_loop(1, 0)
    assert cp.requests == []
    assert slot.retry_count == session_module.SESSION_RETRY_ATTEMPTS_PER_GENERATION
    assert "could not build cleanup" in caplog.text
    assert (1, 0, controller.generation) in controller._cleanup_exhausted
    await controller.async_shutdown()


async def test_cleanup_loop_survives_an_unexpected_error(hass, caplog):
    controller, cp = await _controller(hass)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73
    cp.call_classified = AsyncMock(side_effect=RuntimeError("boom"))
    controller._schedule_cleanup(1, 0)
    task = controller._cleanup_tasks[(1, 0)]
    await task
    assert "session-profile cleanup task failed" in caplog.text
    # An eagerly started task can finish before it is registered; either way
    # nothing live remains and a fresh loop can be scheduled.
    assert controller._cleanup_tasks.get((1, 0)) in (None, task)
    assert task.done()
    controller._schedule_cleanup(1, 0)
    assert controller._cleanup_tasks[(1, 0)] is not task
    assert slot.dirty
    await controller.async_shutdown()


async def test_transaction_start_and_end_input_guards(hass, caplog):
    controller, _cp = await _controller(hass)
    controller.on_transaction_start(True, 73, 1)
    controller.on_transaction_start(3, 73, 3)
    controller.on_transaction_start(1, "", 1)
    controller.on_transaction_start(1, True, 1)
    assert controller.current_token(1) is None
    assert "boolean connector" in caplog.text
    assert "outside the session-limit id range" in caplog.text
    assert "invalid transaction id" in caplog.text
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    controller.on_transaction_start(1, 73, 1)
    assert controller.current_token(1) is token
    controller.on_transaction_end(True, 73)
    controller.on_transaction_end(3, 73)
    controller.on_transaction_end(2, 73)
    assert controller.current_token(1) is token
    await controller.async_shutdown()


async def test_new_start_retires_the_previous_slot_by_its_state(hass, monkeypatch):
    controller, cp = await _controller(hass)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    controller.on_transaction_start(1, 73, 1)
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 16)
    controller.on_transaction_start(1, 74, 1)
    connector = controller._connector(1)
    assert connector.slots[0].state is SlotState.PENDING_CLEAR
    assert connector.active_slot == 1
    assert scheduled == [(1, 0)]

    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()
    cp.results.append(_result())
    task = asyncio.create_task(
        controller.async_set_limit(1, controller.current_token(1), 12)
    )
    await cp.call_entered.wait()
    controller.on_transaction_start(1, 75, 1)
    assert connector.slots[1].state is SlotState.UNCERTAIN
    cp.call_release.set()
    await task
    assert connector.active_slot is None
    await controller.async_shutdown()


async def test_a_new_transaction_retires_a_quarantined_record(hass):
    controller, _cp = await _controller(hass, stored={"records": [_record(slot=2)]})
    assert controller._quarantine[1]
    controller.on_transaction_start(1, 73, 1)
    assert 1 not in controller._quarantine
    await controller.async_shutdown()


async def test_disconnect_interrupts_in_flight_work_without_cancelling_the_caller(
    hass,
):
    """A boundary cancels the charger call only; the caller gets an error.

    The task awaiting async_set_limit is the automation or service call that
    asked for the change. Cancelling it would abort that run silently.
    """
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    task = await _blocked(
        cp, controller.async_set_limit(1, controller.current_token(1), 16)
    )
    cleanup = asyncio.create_task(asyncio.Event().wait())
    controller._cleanup_tasks[(2, 0)] = cleanup

    controller.on_disconnect()

    with pytest.raises(HomeAssistantError, match="not applied.*transport"):
        await task
    assert not task.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert controller.current_token(1) is None
    assert not controller._cleanup_tasks
    assert not controller._owned_tasks
    await controller.async_shutdown()


async def test_a_boundary_interrupts_a_custom_profile_without_cancelling_the_caller(
    hass,
):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 16)
    slot = controller._connector(1).slots[0]
    task = await _blocked(
        cp,
        controller.async_custom_profile(
            1, {"chargingProfilePurpose": "TxProfile", "chargingProfileId": 9}
        ),
    )

    controller.on_disconnect()

    with pytest.raises(HomeAssistantError, match="uncertain"):
        await task
    assert not task.cancelled()
    # The managed id stays dirty for the next generation's exact cleanup and
    # nothing is displayed for it without a session token.
    assert slot.dirty
    assert controller.value(1) == 32
    assert not controller.is_available(1)
    await controller.async_shutdown()


async def test_a_restarted_transaction_with_the_same_id_is_stale_for_an_older_set(
    hass, monkeypatch
):
    """An end and a restart that rebuild an equal token still supersede a set."""
    controller, cp = await _controller(hass)
    monkeypatch.setattr(controller, "_schedule_cleanup", lambda *_args: None)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    task = await _blocked(cp, controller.async_set_limit(1, token, 16))

    controller.on_transaction_end(1, 73)
    controller.on_transaction_start(1, 73, 1)
    assert controller.current_token(1) == token
    cp.call_release.set()
    await task

    assert controller._connector(1).slots[0].state is SlotState.PENDING_CLEAR
    assert controller.value(1) == 32
    await controller.async_shutdown()


async def test_a_change_in_flight_keeps_the_entity_available_and_reports_pending(
    hass,
):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    cp.results.append(_result())
    await controller.async_set_limit(1, token, 16)

    task = await _blocked(cp, controller.async_set_limit(1, token, 12))
    assert controller.is_available(1)
    assert controller.value(1) == 16  # the last confirmed value, not the request
    assert controller.attributes(1)["operation_pending"] == "set"
    with pytest.raises(HomeAssistantError, match="operation pending"):
        await controller.async_set_limit(1, token, 10)
    cp.call_release.set()
    await task
    assert controller.attributes(1)["operation_pending"] is None
    assert controller.value(1) == 12

    task = await _blocked(cp, controller.async_set_limit(1, token, 32))
    assert controller.is_available(1)
    assert controller.value(1) == 12  # a clear in flight keeps the old truth
    cp.call_release.set()
    await task
    assert controller.value(1) == 32
    await controller.async_shutdown()


async def test_a_lowered_maximum_clamps_stored_currents_instead_of_quarantining(
    hass,
):
    controller = SessionLimitController(hass, "entry", "CP_A", "charger", 32, 2)
    controller._store.async_load = AsyncMock(
        return_value={"records": [_record(confirmed_amps=40, requested_amps=40)]}
    )
    controller._store.async_delay_save = Mock()
    await controller.async_load()

    slot = controller._connector(1).slots[0]
    assert slot.state is SlotState.OWNED
    assert slot.confirmed_amps == 32
    assert slot.requested_amps == 32
    assert not controller._quarantine
    await controller.async_shutdown()


async def test_a_charger_unbound_before_the_send_fences_the_slot(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    real_prepare = cp.prepare_session_limit

    async def unbind_then_prepare(*args, **kwargs):
        controller._cp = None  # a shutdown raced the operation
        return await real_prepare(*args, **kwargs)

    cp.prepare_session_limit = unbind_then_prepare
    with pytest.raises(HomeAssistantError, match="uncertain"):
        await controller.async_set_limit(1, token, 16)
    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert cp.requests == []
    controller._cp = cp
    await controller.async_shutdown()


async def test_a_boundary_before_the_frame_left_leaves_the_slot_clean(hass):
    """An interrupted call that recorded a pre-send phase is conclusive."""
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    task = await _blocked(
        cp, controller.async_set_limit(1, controller.current_token(1), 16)
    )
    cp.cancel_phase = CallOutcome.LOCAL_REQUEST_INVALID

    controller.on_disconnect()

    with pytest.raises(HomeAssistantError, match="not applied.*local_request"):
        await task
    assert not task.cancelled()
    assert controller._connector(1).slots[0].state is SlotState.CLEAN
    await controller.async_shutdown()


async def test_a_boundary_during_the_station_clear_reports_a_changed_connection(
    hass,
):
    controller, cp = await _controller(hass)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.UNCERTAIN
    slot.transaction_id = 73
    clearing = asyncio.Event()

    async def blocking_clear():
        clearing.set()
        await asyncio.Event().wait()

    cp.clear_profile = blocking_clear
    task = asyncio.create_task(controller.async_clear_profiles())
    await clearing.wait()

    controller.on_disconnect()

    with pytest.raises(HomeAssistantError, match="connection changed"):
        await task
    assert not task.cancelled()
    assert slot.state is SlotState.UNCERTAIN
    assert controller._barrier_owner is None
    await controller.async_shutdown()


async def test_a_cancelled_exact_clear_in_the_barrier_fences_by_phase(hass):
    controller, cp = await _controller(hass, version="2.0.1")
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.OWNED
    slot.transaction_id = 73
    slot.confirmed_amps = 16.0

    task = await _blocked(cp, controller.async_clear_profiles())
    cp.requests.clear()
    cp.cancel_phase = CallOutcome.LOCAL_REQUEST_INVALID
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.OWNED
    assert controller._barrier_owner is None

    task = await _blocked(cp, controller.async_clear_profiles())
    cp.cancel_phase = None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.UNCERTAIN
    assert controller._barrier_owner is None
    await controller.async_shutdown()


async def test_a_custom_profile_on_an_unbound_charger_is_refused_cleanly(hass):
    controller, cp = await _controller(hass)
    controller._cp = None  # an unload unbound the charger after admission
    with pytest.raises(HomeAssistantError, match="not connected"):
        await controller.async_custom_profile(
            1, {"chargingProfilePurpose": "TxProfile", "chargingProfileId": 9}
        )
    assert cp.requests == []
    assert controller._connector(1).admitted_operation is None
    controller._cp = cp
    await controller.async_shutdown()


async def test_rebinding_the_same_charge_point_crosses_one_boundary(hass):
    controller, cp = await _controller(hass)
    generation = controller.generation

    controller.on_disconnect()
    await controller.async_bind(cp)
    assert controller.generation == generation + 1

    await controller.async_bind(FakeChargePoint())
    assert controller.generation == generation + 2
    await controller.async_shutdown()


async def test_expected_boot_that_never_arrives_opens_the_gate(
    hass, monkeypatch, caplog
):
    controller, _cp = await _controller(hass)
    monkeypatch.setattr(session_module, "SESSION_BOOT_TIMEOUT", 0.01)
    await controller.async_post_connect_ready(True)
    assert controller._initial_gate
    await controller.async_post_connect_ready(True)
    await asyncio.sleep(0.05)
    assert not controller._initial_gate
    assert not controller._expected_boot
    assert "did not arrive" in caplog.text
    await controller.async_shutdown()


async def test_shutdown_cancels_the_boot_gate_and_a_running_barrier(hass):
    controller, cp = await _controller(hass, version="2.0.1")
    await controller.async_post_connect_ready(True)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.UNCERTAIN
    slot.transaction_id = 73
    cp.call_entered = asyncio.Event()
    cp.call_release = asyncio.Event()
    cp.results.append(_result())
    clear = asyncio.create_task(controller.async_clear_profiles())
    await cp.call_entered.wait()

    await controller.async_shutdown()

    with pytest.raises(HomeAssistantError, match="connection changed"):
        await clear
    assert not clear.cancelled()
    await asyncio.sleep(0)
    assert controller._boot_timeout_task.cancelled()


async def test_save_scheduling_is_guarded(hass, caplog):
    controller, _cp = await _controller(hass)
    controller._store.async_delay_save = Mock(side_effect=RuntimeError("disk"))
    controller._schedule_save()
    assert "failed to schedule session-limit record save" in caplog.text
    controller._loaded = False
    controller._store.async_delay_save = Mock()
    controller._schedule_save()
    controller._store.async_delay_save.assert_not_called()
    controller._loaded = True
    await controller.async_shutdown()


async def test_entity_registration_removal_is_idempotent(hass):
    controller, _cp = await _controller(hass)
    remove = controller.register_entity(1, "number.a")
    remove()
    remove()
    assert 1 not in controller._entity_ids
    await controller.async_shutdown()


async def test_rejected_or_uncertain_custom_profile_reports_and_fences(
    hass, monkeypatch
):
    controller, cp = await _controller(hass)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    controller.on_transaction_start(1, 73, 1)
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 16)
    profile = {"chargingProfilePurpose": "TxProfile", "chargingProfileId": 9}

    cp.results.append(_result("Rejected"))
    with pytest.raises(HomeAssistantError, match="rejected custom TxProfile"):
        await controller.async_custom_profile(1, profile)
    assert controller._connector(1).slots[0].state is SlotState.OWNED

    cp.results.append(ClassifiedCallResult(CallOutcome.TIMEOUT, error=TimeoutError()))
    with pytest.raises(HomeAssistantError, match="uncertain"):
        await controller.async_custom_profile(1, profile)
    assert controller._connector(1).slots[0].state is SlotState.UNCERTAIN
    assert scheduled == [(1, 0)]
    await controller.async_shutdown()


async def test_custom_profile_handoff_clear_failures(hass, monkeypatch):
    controller, cp = await _controller(hass)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    profile = {"chargingProfilePurpose": "TxProfile", "chargingProfileId": 9}
    controller.on_transaction_start(1, 73, 1)
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 16)
    slot = controller._connector(1).slots[0]

    cp.results.append(_result())
    cp.build_session_clear_request = Mock(side_effect=ValueError("no"))
    with pytest.raises(HomeAssistantError, match="could not be built"):
        await controller.async_custom_profile(1, profile)
    assert slot.state is SlotState.PENDING_CLEAR
    assert scheduled[-1] == (1, 0)

    def clear_request(profile_id):
        return {"kind": "clear", "profile_id": profile_id}

    cp.build_session_clear_request = clear_request
    slot.state = SlotState.OWNED
    cp.results.extend([_result(), _result("Rejected")])
    with pytest.raises(HomeAssistantError, match="could not be confirmed absent"):
        await controller.async_custom_profile(1, profile)
    assert slot.state is SlotState.PENDING_CLEAR

    slot.state = SlotState.OWNED
    cp.results.extend(
        [_result(), ClassifiedCallResult(CallOutcome.TIMEOUT, error=TimeoutError())]
    )
    with pytest.raises(HomeAssistantError, match="could not be confirmed absent"):
        await controller.async_custom_profile(1, profile)
    assert slot.state is SlotState.UNCERTAIN
    await controller.async_shutdown()


async def test_cancelled_handoff_clear_leaves_the_managed_profile_dirty(
    hass, monkeypatch
):
    controller, cp = await _controller(hass)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    controller.on_transaction_start(1, 73, 1)
    cp.results.append(_result())
    await controller.async_set_limit(1, controller.current_token(1), 16)
    slot = controller._connector(1).slots[0]
    real_call = cp.call_classified
    calls = 0
    clearing = asyncio.Event()

    async def block_the_clear(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            clearing.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as ex:
                ex.classified_result = ClassifiedCallResult(
                    CallOutcome.LOCAL_REQUEST_INVALID, error=ex
                )
                raise
        return await real_call(request)

    cp.call_classified = block_the_clear
    cp.results.append(_result())
    task = asyncio.create_task(
        controller.async_custom_profile(
            1, {"chargingProfilePurpose": "TxProfile", "chargingProfileId": 9}
        )
    )
    await clearing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.PENDING_CLEAR
    assert scheduled[-1] == (1, 0)
    await controller.async_shutdown()


async def test_reset_without_a_connector_uses_the_barrier_and_force_discards(
    hass, monkeypatch
):
    controller, cp = await _controller(hass, version="2.0.1")
    monkeypatch.setattr(controller, "_schedule_cleanup", lambda *_args: None)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.UNCERTAIN
    slot.transaction_id = 73
    cp.results.append(_result())
    await controller.async_reset(None)
    assert cp.requests == [{"kind": "clear", "profile_id": 4010}]
    assert slot.state is SlotState.CLEAN

    slot.state = SlotState.UNCERTAIN
    await controller.async_reset(None, force=True)
    assert slot.state is SlotState.CLEAN
    assert cp.requests == [{"kind": "clear", "profile_id": 4010}]
    await controller.async_shutdown()


async def test_reset_failures_before_and_after_the_call(hass, monkeypatch):
    controller, cp = await _controller(hass)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73
    cp.build_session_clear_request = Mock(side_effect=ValueError("no"))
    with pytest.raises(HomeAssistantError, match="could not clear"):
        await controller.async_reset(1)
    assert slot.state is SlotState.UNCERTAIN
    assert scheduled == [(1, 0)]

    def clear_request(profile_id):
        return {"kind": "clear", "profile_id": profile_id}

    cp.build_session_clear_request = clear_request
    slot.state = SlotState.PENDING_CLEAR
    cp.results.append(_result("Rejected"))
    with pytest.raises(HomeAssistantError, match="could not clear"):
        await controller.async_reset(1)
    assert slot.state is SlotState.PENDING_CLEAR

    cp.results.append(_result())
    await controller.async_reset(1)
    assert slot.state is SlotState.CLEAN
    await controller.async_shutdown()


async def test_cancelled_reset_fences_by_phase(hass, monkeypatch):
    controller, cp = await _controller(hass)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73

    task = await _blocked(cp, controller.async_reset(1))
    cp.cancel_phase = None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.UNCERTAIN
    assert scheduled == [(1, 0)]

    slot.state = SlotState.PENDING_CLEAR
    task = await _blocked(cp, controller.async_reset(1))
    cp.cancel_phase = CallOutcome.LOCAL_REQUEST_INVALID
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slot.state is SlotState.PENDING_CLEAR
    await controller.async_shutdown()


async def test_force_release_refuses_while_work_is_admitted(hass):
    controller, _cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    op = await controller._admit(1, "held")
    with pytest.raises(HomeAssistantError, match="operation pending"):
        await controller.async_reset(1, force=True)
    await controller._release(1, op)
    controller._barrier_owner = object()
    with pytest.raises(HomeAssistantError, match="operation pending"):
        await controller.async_reset(None, force=True)
    controller._barrier_owner = None
    await controller.async_shutdown()


async def test_force_release_discards_records_and_cancels_cleanup(hass, caplog):
    controller, _cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    connector = controller._connector(1)
    connector.slots[1].state = SlotState.UNCERTAIN
    connector.slots[1].transaction_id = 72
    pending = asyncio.create_task(asyncio.Event().wait())
    controller._cleanup_tasks[(1, 1)] = pending
    controller._quarantine[1].append({"reason": "x"})
    controller._quarantine[2].append({"reason": "y"})

    await controller.async_reset(1, force=True)

    assert connector.slots[1].state is SlotState.CLEAN
    assert connector.active_slot == 0
    assert 1 not in controller._quarantine
    assert 2 in controller._quarantine
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert "UNSAFE force release" in caplog.text

    await controller.async_reset(None, force=True)
    assert not controller._quarantine
    await controller.async_shutdown()


async def test_second_barrier_is_refused_while_one_drains(hass):
    controller, _cp = await _controller(hass, version="2.0.1")
    controller.on_transaction_start(1, 73, 1)
    op = await controller._admit(1, "held")
    first = asyncio.create_task(controller.async_clear_profiles())
    await asyncio.sleep(0)
    with pytest.raises(HomeAssistantError, match="charge-point-wide operation"):
        await controller.async_clear_profiles()
    await controller._release(1, op)
    await first
    await controller.async_shutdown()


async def test_barrier_without_a_charger_or_with_a_failing_station_clear(
    hass, monkeypatch
):
    controller, cp = await _controller(hass)
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.OWNED
    slot.transaction_id = 73
    slot.confirmed_amps = 16.0
    cp.clear_profile = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError, match="partial"):
        await controller.async_clear_profiles()
    assert slot.state is SlotState.UNCERTAIN
    assert scheduled == [(1, 0)]
    controller._cp = None
    with pytest.raises(HomeAssistantError, match="not connected"):
        await controller.async_clear_profiles()
    controller._cp = cp
    await controller.async_shutdown()


async def test_201_barrier_exact_clear_failures_before_and_after_the_call(
    hass, monkeypatch
):
    controller, cp = await _controller(hass, version="2.0.1")
    scheduled = []
    monkeypatch.setattr(
        controller, "_schedule_cleanup", lambda c, s: scheduled.append((c, s))
    )
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73
    cp.build_session_clear_request = Mock(side_effect=ValueError("no"))
    with pytest.raises(HomeAssistantError, match="partial"):
        await controller.async_reset(None)
    assert slot.state is SlotState.UNCERTAIN
    assert scheduled == [(1, 0)]

    def clear_request(profile_id):
        return {"kind": "clear", "profile_id": profile_id}

    cp.build_session_clear_request = clear_request
    cp.results.append(ClassifiedCallResult(CallOutcome.TIMEOUT, error=TimeoutError()))
    with pytest.raises(HomeAssistantError, match="partial"):
        await controller.async_reset(None)
    assert slot.state is SlotState.UNCERTAIN
    assert scheduled == [(1, 0), (1, 0)]
    await controller.async_shutdown()


async def test_classified_call_send_and_reply_failures_by_phase():
    adapter = ClassifiedCallAdapter()
    request = call.ClearChargingProfile(id=4010)
    adapter._send.side_effect = ConnectionError("gone")
    assert (await adapter.call_classified(request)).outcome is (
        CallOutcome.TRANSPORT_FAILURE
    )
    adapter._send.side_effect = None

    adapter._get_specific_response.side_effect = TimeoutError()
    assert (await adapter.call_classified(request)).outcome is CallOutcome.TIMEOUT
    adapter._get_specific_response.side_effect = RuntimeError("odd")
    assert (await adapter.call_classified(request)).outcome is (
        CallOutcome.REMOTE_ERROR
    )
    adapter._get_specific_response.side_effect = None

    adapter._get_specific_response.return_value = object()
    assert (await adapter.call_classified(request)).outcome is (
        CallOutcome.RESPONSE_INVALID
    )
    for code, outcome in (
        ("NotSupported", CallOutcome.REMOTE_VALIDATION_ERROR),
        ("InternalError", CallOutcome.REMOTE_ERROR),
        ("Bogus", CallOutcome.REMOTE_ERROR),
    ):
        adapter._get_specific_response.return_value = CallError(
            "request-id", code, "", {}
        )
        assert (await adapter.call_classified(request)).outcome is outcome


async def test_cancelled_during_request_validation_or_send_is_classified(
    monkeypatch,
):
    adapter = ClassifiedCallAdapter()
    request = call.ClearChargingProfile(id=4010)
    real_validate = chargepoint_module.validate_payload
    validating = asyncio.Event()
    never = asyncio.Event()

    async def blocking_request_validation(message, version):
        validating.set()
        await never.wait()
        return await real_validate(message, version)

    monkeypatch.setattr(
        chargepoint_module, "validate_payload", blocking_request_validation
    )
    task = asyncio.create_task(adapter.call_classified(request))
    await validating.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert cancelled.value.classified_result.outcome is (
        CallOutcome.LOCAL_REQUEST_INVALID
    )
    monkeypatch.setattr(chargepoint_module, "validate_payload", real_validate)

    sending = asyncio.Event()

    async def blocking_send(_payload):
        sending.set()
        await never.wait()

    adapter._send = blocking_send
    task = asyncio.create_task(adapter.call_classified(request))
    await sending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert cancelled.value.classified_result.outcome is (CallOutcome.TRANSPORT_FAILURE)


async def test_cancelled_preparation_propagates_without_dirtying_a_slot(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    preparing = asyncio.Event()

    async def blocking_prepare(*_args, **_kwargs):
        preparing.set()
        await asyncio.Event().wait()

    cp.prepare_session_limit = blocking_prepare
    task = asyncio.create_task(controller.async_set_limit(1, token, 16))
    await preparing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller._connector(1).slots[0].state is SlotState.CLEAN
    assert controller.is_available(1)
    await controller.async_shutdown()


async def test_clear_active_slot_guards_against_stale_or_unusable_state(hass):
    controller, cp = await _controller(hass)
    controller.on_transaction_start(1, 73, 1)
    token = controller.current_token(1)
    connector = controller._connector(1)
    op = await controller._admit(1, "probe")
    stale = SessionToken(token.generation, 99, 1, 1)
    with pytest.raises(HomeAssistantError, match="session changed"):
        await controller._clear_active_slot(connector, op, stale)
    connector.active_slot = None
    await controller._clear_active_slot(connector, op, token)
    connector.active_slot = 0
    connector.slots[0].state = SlotState.PENDING_CLEAR
    with pytest.raises(HomeAssistantError, match="profile is uncertain"):
        await controller._clear_active_slot(connector, op, token)
    connector.slots[0].state = SlotState.CLEAN
    await controller._release(1, op)
    assert cp.requests == []
    await controller.async_shutdown()


async def test_cleanup_scheduling_is_idempotent_and_clears_its_registration(
    hass, monkeypatch
):
    controller, cp = await _controller(hass)
    monkeypatch.setattr(session_module, "SESSION_RETRY_MIN", 0.01)
    slot = controller._connector(1).slots[0]
    controller._schedule_cleanup(1, 0)  # a clean slot ends the loop at once
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73
    controller._barrier_owner = object()  # keeps the loop retrying admission
    controller._schedule_cleanup(1, 0)
    task = controller._cleanup_tasks[(1, 0)]
    controller._schedule_cleanup(1, 0)
    assert controller._cleanup_tasks[(1, 0)] is task
    cp.results.append(_result())
    controller._barrier_owner = None
    await task
    assert slot.state is SlotState.CLEAN
    assert (1, 0) not in controller._cleanup_tasks
    await controller.async_shutdown()


async def test_cleanup_loop_stops_when_the_slot_was_cleaned_meanwhile(hass):
    controller, cp = await _controller(hass)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.PENDING_CLEAR
    slot.transaction_id = 73
    real_admit = controller._admit

    async def admit_then_clean(*args, **kwargs):
        op = await real_admit(*args, **kwargs)
        controller._clean_slot(1, slot)
        return op

    controller._admit = admit_then_clean
    await controller._cleanup_loop(1, 0)
    assert cp.requests == []
    assert slot.state is SlotState.CLEAN
    await controller.async_shutdown()


async def test_cancelled_station_clear_propagates(hass):
    controller, cp = await _controller(hass)
    slot = controller._connector(1).slots[0]
    slot.state = SlotState.OWNED
    slot.transaction_id = 73
    slot.confirmed_amps = 16.0
    clearing = asyncio.Event()

    async def blocking_clear():
        clearing.set()
        await asyncio.Event().wait()

    cp.clear_profile = blocking_clear
    task = asyncio.create_task(controller.async_clear_profiles())
    await clearing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller._barrier_owner is None
    assert slot.state is SlotState.OWNED
    await controller.async_shutdown()
