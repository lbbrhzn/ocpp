"""Transaction-scoped charging-profile controller.

The session number is deliberately conservative: it publishes only values a
charger accepted for the exact transaction currently displayed.  Persistence
is a best-effort cleanup hint and is never used to restore availability.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import logging
import math
from typing import Any

from homeassistant.const import STATE_OK
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DATA_UPDATED, DOMAIN
from .enums import Profiles

_LOGGER = logging.getLogger(__package__)

SESSION_PROFILE_MIN_ID = 4000
SESSION_PROFILE_MAX_ID = 4999
SESSION_MAX_CONNECTOR = 99
SESSION_SLOT_COUNT = 2
SESSION_STORE_VERSION = 1
SESSION_RETRY_MIN = 2.0
SESSION_RETRY_MAX = 60.0
SESSION_RETRY_ATTEMPTS_PER_GENERATION = 8
SESSION_DRAIN_TIMEOUT = 35.0
SESSION_BOOT_TIMEOUT = 10.0


class SlotState(StrEnum):
    """Lifecycle states for one reserved profile id."""

    CLEAN = "clean"
    PENDING_SET = "pending_set"
    OWNED = "owned"
    PENDING_CLEAR = "pending_clear"
    UNCERTAIN = "uncertain"


class CallOutcome(StrEnum):
    """Phase-aware result of an outbound OCPP call."""

    SUCCESS = "success"
    LOCAL_REQUEST_INVALID = "local_request_invalid"
    REMOTE_VALIDATION_ERROR = "remote_validation_callerror"
    REMOTE_ERROR = "remote_error"
    RESPONSE_INVALID = "response_invalid"
    TRANSPORT_FAILURE = "transport_send_failure"
    TIMEOUT = "timeout"


@dataclass(slots=True)
class ClassifiedCallResult:
    """An outbound call result whose side-effect certainty is explicit."""

    outcome: CallOutcome
    response: Any = None
    error: BaseException | None = None

    @property
    def conclusively_not_applied(self) -> bool:
        """Whether the request is known not to have changed charger state."""
        return self.outcome in {
            CallOutcome.LOCAL_REQUEST_INVALID,
            CallOutcome.REMOTE_VALIDATION_ERROR,
        }

    @property
    def uncertain(self) -> bool:
        """Whether the charger may have applied the request."""
        return self.outcome in {
            CallOutcome.REMOTE_ERROR,
            CallOutcome.RESPONSE_INVALID,
            CallOutcome.TRANSPORT_FAILURE,
            CallOutcome.TIMEOUT,
        }


@dataclass(frozen=True, slots=True)
class SessionToken:
    """Identity of exactly one online-observed charging transaction."""

    generation: int
    transaction_id: int | str
    connector_id: int
    target: int | tuple[int, int]


@dataclass(slots=True)
class SessionSlot:
    """State associated with one deterministic charging-profile id."""

    index: int
    state: SlotState = SlotState.CLEAN
    transaction_id: int | str | None = None
    generation: int = 0
    confirmed_amps: float | None = None
    requested_amps: float | None = None
    transmitted_unit: str | None = None
    transmitted_value: float | None = None
    conversion_voltage: float | None = None
    conversion_phases: int | None = None
    retry_count: int = 0

    @property
    def dirty(self) -> bool:
        """Whether the deterministic id may still exist on the charger."""
        return self.state is not SlotState.CLEAN


@dataclass(slots=True)
class AdmittedOperation:
    """One logical connector operation, potentially containing several calls."""

    identifier: int
    generation: int
    kind: str
    phase: str = "preparing"


@dataclass(slots=True)
class ConnectorSession:
    """Current session and two independent profile slots for a connector."""

    connector_id: int
    current_session: SessionToken | None = None
    active_slot: int | None = None
    admitted_operation: AdmittedOperation | None = None
    operation_generation: int = 0
    slots: list[SessionSlot] = field(
        default_factory=lambda: [SessionSlot(0), SessionSlot(1)]
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def session_profile_id(connector_id: int, slot: int) -> int:
    """Return the reserved id for one validated connector and slot."""
    if not 1 <= connector_id <= SESSION_MAX_CONNECTOR:
        raise ValueError(f"connector {connector_id} is outside 1..99")
    if slot not in (0, 1):
        raise ValueError(f"slot {slot} is not 0 or 1")
    return SESSION_PROFILE_MIN_ID + 10 * connector_id + slot


def session_store_key(entry_id: str, cp_id: str) -> str:
    """Return a collision-resistant store key for one charge point."""
    digest = hashlib.sha256(cp_id.encode()).hexdigest()[:10]
    return f"{DOMAIN}.session_limits.{entry_id}.{digest}"


def custom_profile_ids(profile: dict[str, Any]) -> set[int]:
    """Read every protocol spelling of a custom charging profile id."""
    result: set[int] = set()
    for key in ("chargingProfileId", "charging_profile_id", "id"):
        value = profile.get(key)
        if value is not None:
            try:
                if not isinstance(value, bool):
                    result.add(int(value))
            except (TypeError, ValueError):
                continue
    return result


def is_tx_profile(profile: dict[str, Any]) -> bool:
    """Whether a custom profile declares TxProfile purpose."""
    value = profile.get("chargingProfilePurpose")
    if value is None:
        value = profile.get("charging_profile_purpose")
    return str(getattr(value, "value", value) or "").casefold() == "txprofile"


class SessionLimitController:
    """Own transaction-scoped profiles for one configured charge point."""

    def __init__(
        self,
        hass,
        entry_id: str,
        cp_id: str,
        cpid: str,
        max_current: float,
        configured_connectors: int,
    ) -> None:
        """Initialize isolated state and persistence for one charge point."""
        self.hass = hass
        self.entry_id = entry_id
        self.cp_id = cp_id
        self.cpid = cpid
        self.max_current = float(max_current)
        try:
            connectors = int(configured_connectors or 1)
        except (TypeError, ValueError):
            # A malformed count in a hand-edited entry must not stop setup;
            # the number platform falls back to one connector the same way.
            connectors = 1
        self.configured_connectors = max(1, connectors)
        self._store = Store(
            hass,
            SESSION_STORE_VERSION,
            session_store_key(entry_id, cp_id),
        )
        self._connectors: dict[int, ConnectorSession] = {}
        self._quarantine: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self._cleanup_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._cleanup_exhausted: set[tuple[int, int, int]] = set()
        self._entity_ids: dict[int, set[str]] = defaultdict(set)
        self._load_lock = asyncio.Lock()
        self._gate_lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()
        self._active_operations: dict[int, tuple[int, AdmittedOperation]] = {}
        self._barrier_owner: object | None = None
        self._owned_tasks: set[asyncio.Task] = set()
        self._release_barrier_when_drained = False
        self._next_operation_id = 0
        self._generation = 0
        self._cp: Any = None
        self._ready = False
        self._loaded = False
        self._initial_gate = True
        self._expected_boot = False
        self._boot_timeout_task: asyncio.Task | None = None

    @property
    def generation(self) -> int:
        """Current connection generation."""
        return self._generation

    def _connector(self, connector_id: int) -> ConnectorSession:
        """Return connector state, creating it without OCPP side effects."""
        connector_id = int(connector_id)
        state = self._connectors.get(connector_id)
        if state is None:
            state = ConnectorSession(connector_id)
            self._connectors[connector_id] = state
        return state

    def _configured_connector_id(
        self, connector_id: Any, *, zero_for_single: bool = False
    ) -> int:
        """Return one configured connector without lossy integer coercion."""
        if isinstance(connector_id, bool):
            raise HomeAssistantError("connector must be an integer")
        try:
            parsed = int(connector_id)
        except (TypeError, ValueError) as ex:
            raise HomeAssistantError("connector must be an integer") from ex
        if isinstance(connector_id, float) and not connector_id.is_integer():
            raise HomeAssistantError("connector must be an integer")
        if parsed == 0 and zero_for_single and self.configured_connectors == 1:
            parsed = 1
        upper = min(SESSION_MAX_CONNECTOR, self.configured_connectors)
        if not 1 <= parsed <= upper:
            raise HomeAssistantError(
                f"connector must be between 1 and {upper} for this charger"
            )
        return parsed

    async def async_load(self) -> None:
        """Load and validate best-effort cleanup records once."""
        async with self._load_lock:
            if self._loaded:
                return
            await self._async_load_records()

    async def _async_load_records(self) -> None:
        """Load cleanup records while the one-time load lock is held."""
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.exception(
                "%s: failed to load session-limit cleanup records", self.cp_id
            )
            data = None
        records = data.get("records", []) if isinstance(data, dict) else []
        if not isinstance(records, list):
            records = []
            self._quarantine[0].append({"reason": "records is not a list"})
        maximum_records = SESSION_SLOT_COUNT * min(
            SESSION_MAX_CONNECTOR, self.configured_connectors
        )
        if len(records) > maximum_records:
            self._quarantine[0].append(
                {
                    "reason": (
                        f"record count {len(records)} exceeds the maximum "
                        f"{maximum_records} for this charger"
                    )
                }
            )
            records = records[:maximum_records]
        seen: set[tuple[int, int]] = set()
        for raw in records:
            try:
                connector_id, slot = self._validate_record(raw, seen)
                rec = self._connector(connector_id).slots[slot]
                rec.state = SlotState(raw["state"])
                rec.transaction_id = raw["transaction_id"]
                rec.generation = int(raw["generation"])
                # A lowered maximum must not quarantine an otherwise valid
                # record: its currents only describe what was sent, and the
                # id still needs its exact clear. Clamp them instead.
                rec.confirmed_amps = self._clamped_current(
                    raw.get("confirmed_amps"), connector_id, slot
                )
                rec.requested_amps = self._clamped_current(
                    raw.get("requested_amps"), connector_id, slot
                )
                rec.transmitted_unit = raw.get("transmitted_unit")
                rec.transmitted_value = self._optional_number(
                    raw.get("transmitted_value")
                )
                rec.conversion_voltage = self._optional_number(
                    raw.get("conversion_voltage")
                )
                phases = raw.get("conversion_phases")
                rec.conversion_phases = int(phases) if phases is not None else None
                rec.retry_count = max(0, min(int(raw.get("retry_count", 0)), 1000))
            except (KeyError, TypeError, ValueError) as ex:
                connector = 0
                if isinstance(raw, dict):
                    with contextlib.suppress(TypeError, ValueError):
                        connector = int(raw.get("connector_id", 0))
                self._quarantine[connector].append({"reason": str(ex)})
                _LOGGER.warning(
                    "%s: quarantined invalid session-limit record for connector %s: %s",
                    self.cp_id,
                    connector,
                    ex,
                )
        self._loaded = True
        self._sync_repair_issues()

    def _clamped_current(
        self, value: Any, connector_id: int, slot: int
    ) -> float | None:
        current = self._optional_number(value)
        if current is not None and current > self.max_current:
            _LOGGER.debug(
                "%s[%s]: clamped a stored session current of %.1f A to the "
                "configured maximum %.1f A for slot %s",
                self.cp_id,
                connector_id,
                current,
                self.max_current,
                slot,
            )
            return self.max_current
        return current

    @staticmethod
    def _optional_number(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("numeric value has invalid type")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError("numeric value is not finite and non-negative")
        return result

    def _validate_record(self, raw: Any, seen: set[tuple[int, int]]) -> tuple[int, int]:
        if not isinstance(raw, dict):
            raise TypeError("record is not an object")
        required = {
            "connector_id",
            "slot",
            "state",
            "transaction_id",
            "generation",
            "confirmed_amps",
            "requested_amps",
            "transmitted_unit",
            "transmitted_value",
            "conversion_voltage",
            "conversion_phases",
            "retry_count",
        }
        missing = required - raw.keys()
        if missing:
            raise ValueError(f"record is missing fields: {', '.join(sorted(missing))}")

        connector_id = self._stored_int(raw["connector_id"], "connector")
        slot = self._stored_int(raw["slot"], "slot")
        if (
            not 1
            <= connector_id
            <= min(SESSION_MAX_CONNECTOR, self.configured_connectors)
        ):
            raise ValueError("connector is not configured for this charger")
        if slot not in (0, 1):
            raise ValueError("slot is not 0 or 1")
        if (connector_id, slot) in seen:
            raise ValueError("slot is claimed more than once")
        state = SlotState(raw["state"])
        if state is SlotState.CLEAN:
            raise ValueError("clean records must not be persisted")
        tx_id = raw["transaction_id"]
        if isinstance(tx_id, bool) or not isinstance(tx_id, int | str) or tx_id == "":
            raise ValueError("transaction id has invalid type or value")
        generation = self._stored_int(raw["generation"], "generation")
        if generation < 0:
            raise ValueError("generation is negative")
        unit = raw.get("transmitted_unit")
        if unit not in (None, "A", "W"):
            raise ValueError("transmitted unit is invalid")
        diagnostic_id = raw.get("profile_id")
        if diagnostic_id is not None and self._stored_int(
            diagnostic_id, "diagnostic profile id"
        ) != session_profile_id(connector_id, slot):
            raise ValueError("diagnostic profile id does not match connector and slot")
        confirmed = self._optional_number(raw.get("confirmed_amps"))
        requested = self._optional_number(raw.get("requested_amps"))
        transmitted = self._optional_number(raw.get("transmitted_value"))
        self._optional_number(raw.get("conversion_voltage"))
        phases = raw.get("conversion_phases")
        if phases is not None and self._stored_int(
            phases, "conversion phase count"
        ) not in (1, 2, 3):
            raise ValueError("conversion phase count is invalid")
        retry_count = self._stored_int(raw["retry_count"], "retry count")
        if not 0 <= retry_count <= 1000:
            raise ValueError("retry count is outside 0..1000")
        if (unit is None) != (transmitted is None):
            raise ValueError("transmitted unit and value must appear together")
        if state is SlotState.OWNED and confirmed is None:
            raise ValueError("owned record has no confirmed current")
        if state is SlotState.PENDING_SET and requested is None:
            raise ValueError("pending set has no requested current")
        seen.add((connector_id, slot))
        return connector_id, slot

    @staticmethod
    def _stored_int(value: Any, field_name: str) -> int:
        """Reject booleans, floats and strings in integer Store fields."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{field_name} has invalid integer type")
        return value

    def _serialize(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for connector_id, connector in sorted(self._connectors.items()):
            for slot in connector.slots:
                if not slot.dirty:
                    continue
                records.append(
                    {
                        "connector_id": connector_id,
                        "slot": slot.index,
                        "state": slot.state.value,
                        "transaction_id": slot.transaction_id,
                        "generation": slot.generation,
                        "confirmed_amps": slot.confirmed_amps,
                        "requested_amps": slot.requested_amps,
                        "transmitted_unit": slot.transmitted_unit,
                        "transmitted_value": slot.transmitted_value,
                        "conversion_voltage": slot.conversion_voltage,
                        "conversion_phases": slot.conversion_phases,
                        "retry_count": slot.retry_count,
                    }
                )
        return {"records": records}

    def _schedule_save(self) -> None:
        if not self._loaded:
            return
        try:
            self._store.async_delay_save(self._serialize, 1.0)
        except Exception:
            _LOGGER.exception(
                "%s: failed to schedule session-limit record save", self.cp_id
            )

    async def async_bind(self, charge_point: Any) -> None:
        """Bind a new protocol object and start a fresh connection generation."""
        await self.async_load()
        if self._cp is charge_point:
            # A same-object reconnect already crossed its boundary in
            # on_disconnect; a second one here would only double-count.
            return
        self._cp = charge_point
        charge_point.session_controller = self
        self._connection_boundary("protocol bind")

    def _connection_boundary(self, reason: str) -> None:
        """Invalidate current sessions without trusting outstanding calls."""
        self._generation += 1
        self._ready = False
        self._initial_gate = True
        self._expected_boot = False
        if self._boot_timeout_task:
            self._boot_timeout_task.cancel()
            self._boot_timeout_task = None
        for task in self._cleanup_tasks.values():
            task.cancel()
        self._cleanup_tasks.clear()
        self._cleanup_exhausted.clear()
        # Only controller-owned charger calls are cancelled. The callers that
        # admitted them - an automation run or a service call - keep running
        # and receive an error from the interrupted call instead.
        self._cancel_owned_tasks()
        # A closed connection is the other conclusive end to a timed-out
        # barrier drain.  Admitted operations remain registered until their
        # own finally blocks run, but they cannot become an unaccounted late
        # send on this connection.
        self._barrier_owner = None
        self._release_barrier_when_drained = False
        for connector in self._connectors.values():
            connector.operation_generation += 1
            connector.current_session = None
            connector.active_slot = None
            if connector.admitted_operation is not None:
                for slot in connector.slots:
                    if slot.state is SlotState.PENDING_SET:
                        slot.state = SlotState.UNCERTAIN
            self._sync_repair_issues(connector.connector_id)
        _LOGGER.debug(
            "%s: session-limit connection generation %s (%s)",
            self.cp_id,
            self._generation,
            reason,
        )
        self._schedule_save()
        self._notify()

    def on_disconnect(self) -> None:
        """Handle loss of the connection synchronously from charge-point teardown."""
        self._connection_boundary("disconnect")

    def on_boot_notification(self) -> None:
        """Treat every BootNotification as a genuine generation boundary."""
        self._connection_boundary("BootNotification")
        if self._quarantine:
            self._quarantine.clear()
            self._sync_repair_issues()
        # The notification itself completes the gate for the new generation,
        # whether it was requested, spontaneous, or arrived after our timeout.
        self._initial_gate = False
        self._ready = bool(
            self._cp and self._cp.status == STATE_OK and self._cp.post_connect_success
        )
        self._schedule_all_cleanup()
        self._notify()

    async def async_post_connect_ready(self, expect_boot: bool) -> None:
        """Open startup admission, or wait for a requested boot to arrive."""
        self._ready = True
        if expect_boot:
            self._expected_boot = True
            self._initial_gate = True
            if self._boot_timeout_task:
                self._boot_timeout_task.cancel()
            self._boot_timeout_task = self.hass.async_create_task(
                self._boot_gate_timeout(),
                f"ocpp-session-boot-gate-{self.cp_id}",
            )
        else:
            self._expected_boot = False
            self._initial_gate = False
            if self._boot_timeout_task:
                self._boot_timeout_task.cancel()
                self._boot_timeout_task = None
        self._schedule_all_cleanup()
        self._notify()

    async def _boot_gate_timeout(self) -> None:
        try:
            await asyncio.sleep(SESSION_BOOT_TIMEOUT)
            if self._expected_boot:
                self._expected_boot = False
                self._initial_gate = False
                _LOGGER.warning(
                    "%s: requested BootNotification did not arrive within %.0fs; "
                    "session controls opened and any later boot remains a boundary",
                    self.cp_id,
                    SESSION_BOOT_TIMEOUT,
                )
                self._notify()
        except asyncio.CancelledError:
            raise

    def current_token(self, connector_id: int) -> SessionToken | None:
        """Return the immutable token currently backing an entity."""
        return self._connector(connector_id).current_session

    def register_entity(self, connector_id: int, entity_id: str):
        """Register one session entity for targeted dispatcher updates."""
        self._entity_ids[connector_id].add(entity_id)

        def _remove() -> None:
            ids = self._entity_ids.get(connector_id)
            if ids is None:
                return
            ids.discard(entity_id)
            if not ids:
                self._entity_ids.pop(connector_id, None)

        return _remove

    def on_transaction_start(
        self,
        connector_id: int,
        transaction_id: int | str,
        target: int | tuple[int, int] | None = None,
    ) -> None:
        """Record a transaction observed starting online in this generation."""
        if isinstance(connector_id, bool):
            _LOGGER.warning(
                "%s: ignored transaction start on boolean connector", self.cp_id
            )
            return
        connector_id = int(connector_id)
        if (
            not 1
            <= connector_id
            <= min(SESSION_MAX_CONNECTOR, self.configured_connectors)
        ):
            _LOGGER.warning(
                "%s: connector %s is outside the session-limit id range",
                self.cp_id,
                connector_id,
            )
            return
        if (
            isinstance(transaction_id, bool)
            or not isinstance(transaction_id, int | str)
            or transaction_id == ""
        ):
            _LOGGER.warning(
                "%s[%s]: ignored transaction start with invalid transaction id",
                self.cp_id,
                connector_id,
            )
            return
        token = SessionToken(
            self._generation,
            transaction_id,
            connector_id,
            target if target is not None else connector_id,
        )
        connector = self._connector(connector_id)
        if connector.current_session == token:
            return
        connector.operation_generation += 1
        if connector.active_slot is not None:
            old = connector.slots[connector.active_slot]
            if old.dirty:
                if old.state is SlotState.PENDING_SET:
                    old.state = SlotState.UNCERTAIN
                else:
                    old.state = SlotState.PENDING_CLEAR
                self._schedule_cleanup(connector_id, old.index)
        connector.current_session = token
        connector.active_slot = next(
            (slot.index for slot in connector.slots if not slot.dirty), None
        )
        if connector_id in self._quarantine:
            self._quarantine.pop(connector_id, None)
        self._sync_repair_issues(connector_id)
        self._schedule_save()
        self._notify()

    def on_transaction_end(
        self, connector_id: int, transaction_id: int | str | None = None
    ) -> None:
        """Make a completed session unavailable and begin exact-id cleanup."""
        if isinstance(connector_id, bool):
            return
        connector_id = int(connector_id)
        if (
            not 1
            <= connector_id
            <= min(SESSION_MAX_CONNECTOR, self.configured_connectors)
        ):
            return
        connector = self._connector(connector_id)
        token = connector.current_session
        if token is None:
            return
        if transaction_id is not None and token.transaction_id != transaction_id:
            return
        connector.current_session = None
        if connector.active_slot is not None:
            slot = connector.slots[connector.active_slot]
            if slot.dirty:
                slot.state = (
                    SlotState.UNCERTAIN
                    if slot.state is SlotState.PENDING_SET
                    else SlotState.PENDING_CLEAR
                )
                self._schedule_cleanup(connector.connector_id, slot.index)
        connector.active_slot = None
        self._schedule_save()
        self._notify()

    def is_available(self, connector_id: int) -> bool:
        """Whether a session number may accept a request right now."""
        if (
            not self._loaded
            or not self._ready
            or self._initial_gate
            or self._barrier_owner is not None
        ):
            return False
        cp = self._cp
        if cp is None or cp.status != STATE_OK:
            return False
        if not bool(cp.supported_features & Profiles.SMART):
            return False
        connector = self._connector(connector_id)
        if connector.current_session is None:
            return False
        unsafe = getattr(cp, "transaction_is_unsafe", None)
        if unsafe is not None and unsafe(connector_id):
            return False
        if connector.active_slot is None:
            return any(not slot.dirty for slot in connector.slots)
        active = connector.slots[connector.active_slot]
        if active.state in {SlotState.CLEAN, SlotState.OWNED}:
            return True
        # The entity's own change is in flight: stay available, keep showing
        # the last confirmed value and report the pending operation. A second
        # change in this window is refused rather than greyed out.
        pending = connector.admitted_operation
        return (
            pending is not None
            and pending.kind == "set"
            and active.state in {SlotState.PENDING_SET, SlotState.PENDING_CLEAR}
        )

    def value(self, connector_id: int) -> float:
        """Return only a confirmed value applicable to the displayed session."""
        connector = self._connector(connector_id)
        token = connector.current_session
        if token is not None and connector.active_slot is not None:
            slot = connector.slots[connector.active_slot]
            if (
                slot.state
                in {SlotState.OWNED, SlotState.PENDING_SET, SlotState.PENDING_CLEAR}
                and slot.transaction_id == token.transaction_id
                and slot.generation == token.generation
                and slot.confirmed_amps is not None
            ):
                return slot.confirmed_amps
        return self.max_current

    def attributes(self, connector_id: int) -> dict[str, Any]:
        """Expose confirmed transmission details without leaking profile contents."""
        connector = self._connector(connector_id)
        result: dict[str, Any] = {
            "connection_generation": self._generation,
            "session_transaction_id": (
                connector.current_session.transaction_id
                if connector.current_session
                else None
            ),
            "slot_states": [slot.state.value for slot in connector.slots],
            # A change in flight keeps the entity available; a second change
            # is refused as pending until this clears.
            "operation_pending": (
                connector.admitted_operation.kind
                if connector.admitted_operation is not None
                else None
            ),
        }
        if connector.active_slot is not None:
            slot = connector.slots[connector.active_slot]
            result.update(
                {
                    "transmitted_unit": slot.transmitted_unit,
                    "transmitted_value": slot.transmitted_value,
                    "conversion_voltage": slot.conversion_voltage,
                    "conversion_phases": slot.conversion_phases,
                }
            )
        return result

    async def _admit(
        self,
        connector_id: int,
        kind: str,
        *,
        expected: SessionToken | None = None,
        require_available: bool = False,
    ) -> AdmittedOperation:
        connector = self._connector(connector_id)
        async with self._gate_lock:
            if self._barrier_owner is not None:
                raise HomeAssistantError("session-limit operation pending")
            async with connector.lock:
                if connector.admitted_operation is not None:
                    raise HomeAssistantError("session-limit operation pending")
                if expected is not None and connector.current_session != expected:
                    raise HomeAssistantError("the displayed charging session changed")
                if require_available and not self.is_available(connector_id):
                    raise HomeAssistantError("session current limit is unavailable")
                self._next_operation_id += 1
                op = AdmittedOperation(
                    self._next_operation_id,
                    connector.operation_generation,
                    kind,
                )
                connector.admitted_operation = op
                self._active_operations[op.identifier] = (connector_id, op)
                self._drained.clear()
        self._notify()
        return op

    async def _release(self, connector_id: int, op: AdmittedOperation) -> None:
        connector = self._connector(connector_id)
        release_barrier = False
        async with self._gate_lock:
            async with connector.lock:
                if connector.admitted_operation is op:
                    connector.admitted_operation = None
                self._active_operations.pop(op.identifier, None)
                if not self._active_operations:
                    self._drained.set()
                    release_barrier = self._release_barrier_when_drained
                    self._release_barrier_when_drained = False
            if release_barrier:
                self._barrier_owner = None
        self._notify()

    def _cancel_owned_tasks(self) -> None:
        current = asyncio.current_task()
        for task in list(self._owned_tasks):
            if task is not current:
                task.cancel()

    async def _run_owned(self, coro, name: str) -> tuple[bool, Any]:
        """Await a charger call in a controller-owned task.

        A connection boundary cancels only this task, never the caller: an
        automation that was changing a limit gets an error from the
        interrupted call instead of having its whole run cancelled. Returns
        (interrupted, value); on interruption the value is the send phase the
        call recorded, when it recorded one.
        """
        holder: dict[str, Any] = {}

        async def _capture():
            try:
                return await coro
            except asyncio.CancelledError as ex:
                holder["result"] = getattr(ex, "classified_result", None)
                raise

        task = self.hass.async_create_task(_capture(), name)
        self._owned_tasks.add(task)
        try:
            return False, await task
        except asyncio.CancelledError as ex:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or not current.cancelling()):
                return True, holder.get("result")
            # The caller itself was cancelled. Keep the send phase visible to
            # its own handler, which awaiting the task would otherwise lose.
            if holder.get("result") is not None:
                ex.classified_result = holder["result"]
            raise
        finally:
            self._owned_tasks.discard(task)

    async def _classified_call(self, request) -> ClassifiedCallResult:
        """Send one classified call that a boundary can interrupt safely."""
        cp = self._cp
        if cp is None:
            # Unbound between admission and the send, which only a shutdown
            # can do: nothing was sent, but the slot was already marked, so
            # report it the same way as an interrupted call.
            return ClassifiedCallResult(
                CallOutcome.TRANSPORT_FAILURE,
                error=HomeAssistantError("charger is not connected"),
            )
        interrupted, result = await self._run_owned(
            cp.call_classified(request), f"ocpp-session-call-{self.cp_id}"
        )
        if not interrupted:
            return result
        if isinstance(result, ClassifiedCallResult):
            return result
        return ClassifiedCallResult(
            CallOutcome.TRANSPORT_FAILURE,
            error=asyncio.CancelledError("charger connection boundary"),
        )

    @staticmethod
    def _status(response: Any) -> str:
        value = getattr(response, "status", "")
        return str(getattr(value, "value", value) or "").casefold()

    @staticmethod
    def _cancelled_result(error: asyncio.CancelledError) -> ClassifiedCallResult:
        """Recover the send phase recorded by ``call_classified``."""
        result = getattr(error, "classified_result", None)
        if isinstance(result, ClassifiedCallResult):
            return result
        return ClassifiedCallResult(CallOutcome.TRANSPORT_FAILURE, error=error)

    def _log_call_failure(
        self,
        operation: str,
        connector_id: int,
        result: ClassifiedCallResult,
        status: str,
        *,
        profile_id: int | None = None,
    ) -> None:
        """Log a bounded, payload-free description of a classified failure."""
        safe_status = (
            status
            if status in {"accepted", "rejected", "unknown"}
            else ("other" if status else "none")
        )
        error_name = type(result.error).__name__ if result.error is not None else "none"
        _LOGGER.warning(
            "%s[%s]: session-profile %s failed "
            "(profile_id=%s, outcome=%s, status=%s, error=%s)",
            self.cp_id,
            connector_id,
            operation,
            profile_id if profile_id is not None else "foreign",
            result.outcome.value,
            safe_status,
            error_name,
        )

    async def async_set_limit(
        self,
        connector_id: int,
        expected: SessionToken,
        amps: float,
        *,
        source_watts: float | None = None,
    ) -> None:
        """Apply or clear the limit for exactly the entity's displayed session."""
        connector_id = self._configured_connector_id(connector_id)
        if expected.connector_id != connector_id:
            raise HomeAssistantError("the displayed charging session changed")
        if isinstance(amps, bool):
            raise HomeAssistantError("session current must be numeric")
        amps = float(amps)
        if not math.isfinite(amps) or amps < 0 or amps > self.max_current:
            raise HomeAssistantError(
                f"session current must be between 0 and {self.max_current:g} A"
            )
        op = await self._admit(
            connector_id, "set", expected=expected, require_available=True
        )
        connector = self._connector(connector_id)
        try:
            if amps >= self.max_current:
                await self._clear_active_slot(connector, op, expected)
                return
            cp = self._cp
            if cp is None:
                raise HomeAssistantError("charger is not connected")
            try:
                prepared = await cp.prepare_session_limit(
                    connector_id,
                    amps,
                    source_watts=source_watts,
                )
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                _LOGGER.warning(
                    "%s[%s]: could not prepare session limit: %s",
                    self.cp_id,
                    connector_id,
                    ex,
                )
                raise HomeAssistantError(
                    f"could not prepare session limit: {ex}"
                ) from ex

            canonical_amps = float(prepared["amps"])
            if (
                not math.isfinite(canonical_amps)
                or canonical_amps < 0
                or canonical_amps > self.max_current
            ):
                raise HomeAssistantError(
                    "prepared session limit is outside the configured current range"
                )
            unit = prepared.get("unit")
            transmitted_value = float(prepared["value"])
            if unit not in {"A", "W"} or not math.isfinite(transmitted_value):
                raise HomeAssistantError("prepared session limit is invalid")
            if transmitted_value < 0:
                raise HomeAssistantError("prepared session limit is negative")
            conversion_voltage = prepared.get("conversion_voltage")
            if conversion_voltage is not None and (
                isinstance(conversion_voltage, bool)
                or not math.isfinite(float(conversion_voltage))
                or float(conversion_voltage) <= 0
            ):
                raise HomeAssistantError("prepared conversion voltage is invalid")
            conversion_phases = prepared.get("conversion_phases")
            if conversion_phases is not None and (
                isinstance(conversion_phases, bool)
                or not isinstance(conversion_phases, int)
                or conversion_phases not in (1, 2, 3)
            ):
                raise HomeAssistantError("prepared conversion phase count is invalid")

            async with connector.lock:
                if connector.current_session != expected:
                    raise HomeAssistantError(
                        "the charging session ended during preparation"
                    )
                if connector.admitted_operation is not op:
                    raise HomeAssistantError("session-limit operation was superseded")
                if prepared.get("target") != expected.target:
                    raise HomeAssistantError(
                        "the charging-session target changed during preparation"
                    )
                slot_index = connector.active_slot
                if slot_index is None:
                    slot_index = next(
                        (slot.index for slot in connector.slots if not slot.dirty), None
                    )
                    if slot_index is None:
                        self._sync_repair_issues(connector_id)
                        raise HomeAssistantError(
                            "both session profile slots need repair"
                        )
                    connector.active_slot = slot_index
                slot = connector.slots[slot_index]
                if slot.state not in {SlotState.CLEAN, SlotState.OWNED}:
                    raise HomeAssistantError("the current session profile is uncertain")
                previous = (
                    slot.state,
                    slot.transaction_id,
                    slot.generation,
                    slot.confirmed_amps,
                    slot.requested_amps,
                    slot.transmitted_unit,
                    slot.transmitted_value,
                    slot.conversion_voltage,
                    slot.conversion_phases,
                )
                request = cp.build_session_limit_request(
                    connector_id,
                    expected.transaction_id,
                    session_profile_id(connector_id, slot_index),
                    prepared,
                )
                slot.state = SlotState.PENDING_SET
                slot.transaction_id = expected.transaction_id
                slot.generation = expected.generation
                slot.requested_amps = canonical_amps
                slot.transmitted_unit = unit
                slot.transmitted_value = transmitted_value
                slot.conversion_voltage = conversion_voltage
                slot.conversion_phases = conversion_phases
                op.phase = "sent"
                self._schedule_save()
                self._notify()

            try:
                result = await self._classified_call(request)
            except asyncio.CancelledError as ex:
                cancelled = self._cancelled_result(ex)
                async with connector.lock:
                    if cancelled.conclusively_not_applied:
                        self._restore_slot(slot, previous)
                    else:
                        slot.state = SlotState.UNCERTAIN
                    self._schedule_save()
                if slot.dirty and not cancelled.conclusively_not_applied:
                    self._schedule_cleanup(connector_id, slot.index)
                self._notify()
                raise
            cleanup = False
            failure: HomeAssistantError | None = None
            async with connector.lock:
                stale = (
                    connector.current_session != expected
                    or connector.admitted_operation is not op
                    or connector.operation_generation != op.generation
                )
                status = self._status(result.response)
                if stale:
                    if result.outcome is CallOutcome.SUCCESS and status == "accepted":
                        slot.state = SlotState.PENDING_CLEAR
                    elif result.uncertain:
                        slot.state = SlotState.UNCERTAIN
                    elif previous[0] is SlotState.OWNED:
                        slot.state = SlotState.PENDING_CLEAR
                    else:
                        self._clean_slot(connector_id, slot)
                    cleanup = slot.dirty
                    if not (
                        result.outcome is CallOutcome.SUCCESS and status == "accepted"
                    ):
                        failure = HomeAssistantError(
                            "the session limit was not applied before the "
                            f"charging session changed ({status or result.outcome.value})"
                        )
                elif result.outcome is CallOutcome.SUCCESS and status == "accepted":
                    slot.state = SlotState.OWNED
                    slot.confirmed_amps = canonical_amps
                    slot.retry_count = 0
                elif (
                    result.outcome is CallOutcome.SUCCESS
                    or result.conclusively_not_applied
                ):
                    self._restore_slot(slot, previous)
                    failure = HomeAssistantError(
                        f"charger rejected the session limit ({status or result.outcome.value})"
                    )
                else:
                    slot.state = SlotState.UNCERTAIN
                    cleanup = True
                    failure = HomeAssistantError(
                        f"session limit outcome is uncertain ({result.outcome.value})"
                    )
                self._schedule_save()
            if failure is not None:
                self._log_call_failure(
                    "set",
                    connector_id,
                    result,
                    status,
                    profile_id=session_profile_id(connector_id, slot.index),
                )
            if cleanup:
                self._schedule_cleanup(connector_id, slot.index)
            self._sync_repair_issues(connector_id)
            self._notify()
            if failure is not None:
                raise failure
        finally:
            await self._release(connector_id, op)

    async def _clear_active_slot(
        self,
        connector: ConnectorSession,
        op: AdmittedOperation,
        expected: SessionToken,
    ) -> None:
        """Clear the current owned profile, or confirm maximum without a call."""
        async with connector.lock:
            if connector.current_session != expected:
                raise HomeAssistantError("the displayed charging session changed")
            if connector.active_slot is None:
                return
            slot = connector.slots[connector.active_slot]
            if slot.state is SlotState.CLEAN:
                return
            if slot.state is not SlotState.OWNED:
                raise HomeAssistantError("the current session profile is uncertain")
            try:
                request = self._cp.build_session_clear_request(
                    session_profile_id(connector.connector_id, slot.index)
                )
            except Exception as ex:
                raise HomeAssistantError(
                    f"could not build the session profile clear: {ex}"
                ) from ex
            slot.state = SlotState.PENDING_CLEAR
            op.phase = "sent"
            self._schedule_save()
        try:
            result = await self._classified_call(request)
        except asyncio.CancelledError as ex:
            cancelled = self._cancelled_result(ex)
            async with connector.lock:
                slot.state = (
                    SlotState.OWNED
                    if cancelled.conclusively_not_applied
                    else SlotState.UNCERTAIN
                )
                self._schedule_save()
            if not cancelled.conclusively_not_applied:
                self._schedule_cleanup(connector.connector_id, slot.index)
            self._notify()
            raise
        status = self._status(result.response)
        success = result.outcome is CallOutcome.SUCCESS and status in {
            "accepted",
            "unknown",
        }
        async with connector.lock:
            if success:
                self._clean_slot(connector.connector_id, slot)
            elif result.uncertain:
                slot.state = SlotState.UNCERTAIN
                self._schedule_cleanup(connector.connector_id, slot.index)
            else:
                # The clear was conclusively refused or never sent, so the
                # previously Accepted profile remains the last known truth.
                slot.state = SlotState.OWNED
            self._schedule_save()
            self._notify()
        if not success:
            self._log_call_failure(
                "clear",
                connector.connector_id,
                result,
                status,
                profile_id=session_profile_id(connector.connector_id, slot.index),
            )
            raise HomeAssistantError(
                f"charger did not confirm profile removal ({status or result.outcome.value})"
            )

    @staticmethod
    def _restore_slot(slot: SessionSlot, previous: tuple[Any, ...]) -> None:
        (
            slot.state,
            slot.transaction_id,
            slot.generation,
            slot.confirmed_amps,
            slot.requested_amps,
            slot.transmitted_unit,
            slot.transmitted_value,
            slot.conversion_voltage,
            slot.conversion_phases,
        ) = previous

    def _clean_slot(self, connector_id: int, slot: SessionSlot) -> None:
        slot.state = SlotState.CLEAN
        slot.transaction_id = None
        slot.generation = 0
        slot.confirmed_amps = None
        slot.requested_amps = None
        slot.transmitted_unit = None
        slot.transmitted_value = None
        slot.conversion_voltage = None
        slot.conversion_phases = None
        slot.retry_count = 0
        self._cleanup_exhausted = {
            key
            for key in self._cleanup_exhausted
            if key[:2] != (connector_id, slot.index)
        }

    def _schedule_all_cleanup(self) -> None:
        if not self._ready:
            return
        for connector_id, connector in self._connectors.items():
            for slot in connector.slots:
                if slot.dirty:
                    self._schedule_cleanup(connector_id, slot.index)

    def _schedule_cleanup(self, connector_id: int, slot: int) -> None:
        if not self._ready or self._cp is None:
            return
        if (connector_id, slot, self._generation) in self._cleanup_exhausted:
            return
        key = (connector_id, slot)
        task = self._cleanup_tasks.get(key)
        if task is not None and not task.done():
            return
        task = self.hass.async_create_task(
            self._cleanup_loop(connector_id, slot),
            f"ocpp-session-cleanup-{self.cp_id}-{connector_id}-{slot}",
        )
        self._cleanup_tasks[key] = task

    async def _cleanup_loop(self, connector_id: int, slot_index: int) -> None:
        key = (connector_id, slot_index)
        attempts = 0
        try:
            while True:
                connector = self._connector(connector_id)
                slot = connector.slots[slot_index]
                if not slot.dirty:
                    return
                if not self._ready or self._cp is None:
                    return
                if attempts >= SESSION_RETRY_ATTEMPTS_PER_GENERATION:
                    self._cleanup_exhausted.add(
                        (connector_id, slot_index, self._generation)
                    )
                    self._sync_repair_issues(connector_id)
                    _LOGGER.error(
                        "%s[%s]: stopped cleanup for session profile %s after "
                        "%s attempts in connection generation %s; manual reset "
                        "or reconnect is required",
                        self.cp_id,
                        connector_id,
                        session_profile_id(connector_id, slot_index),
                        attempts,
                        self._generation,
                    )
                    self._notify(connector_id)
                    return
                delay = min(
                    SESSION_RETRY_MAX,
                    SESSION_RETRY_MIN * (2 ** min(slot.retry_count, 5)),
                )
                if slot.retry_count:
                    await asyncio.sleep(delay)
                try:
                    op = await self._admit(connector_id, "cleanup")
                except HomeAssistantError:
                    await asyncio.sleep(SESSION_RETRY_MIN)
                    continue
                attempts += 1
                try:
                    async with connector.lock:
                        if not slot.dirty:
                            return
                        slot.state = SlotState.PENDING_CLEAR
                        try:
                            request = self._cp.build_session_clear_request(
                                session_profile_id(connector_id, slot_index)
                            )
                        except Exception as ex:
                            slot.retry_count = min(slot.retry_count + 1, 1000)
                            self._schedule_save()
                            _LOGGER.warning(
                                "%s[%s]: could not build cleanup for session profile %s: %s",
                                self.cp_id,
                                connector_id,
                                session_profile_id(connector_id, slot_index),
                                ex,
                            )
                            continue
                        op.phase = "sent"
                    result = await self._classified_call(request)
                    status = self._status(result.response)
                    async with connector.lock:
                        if result.outcome is CallOutcome.SUCCESS and status in {
                            "accepted",
                            "unknown",
                        }:
                            self._clean_slot(connector_id, slot)
                        else:
                            slot.state = (
                                SlotState.UNCERTAIN
                                if result.uncertain
                                else SlotState.PENDING_CLEAR
                            )
                            slot.retry_count = min(slot.retry_count + 1, 1000)
                            if (
                                slot.retry_count <= 512
                                and slot.retry_count & (slot.retry_count - 1) == 0
                            ):
                                self._log_call_failure(
                                    "cleanup",
                                    connector_id,
                                    result,
                                    status,
                                    profile_id=session_profile_id(
                                        connector_id, slot_index
                                    ),
                                )
                        self._schedule_save()
                finally:
                    await self._release(connector_id, op)
                self._sync_repair_issues(connector_id)
                self._notify()
                if not slot.dirty:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "%s[%s]: session-profile cleanup task failed", self.cp_id, connector_id
            )
        finally:
            if self._cleanup_tasks.get(key) is asyncio.current_task():
                self._cleanup_tasks.pop(key, None)

    async def async_clear_profiles(self) -> None:
        """Route the public broad clear through a charge-point-wide barrier."""
        await self._barrier_clear(include_station=True, broad_v16=True)

    async def async_custom_profile(
        self, connector_id: int, profile: dict[str, Any]
    ) -> None:
        """Order a custom TxProfile before yielding any managed profile."""
        connector_id = self._configured_connector_id(connector_id, zero_for_single=True)
        op = await self._admit(connector_id, "custom_profile")
        connector = self._connector(connector_id)
        managed: SessionSlot | None = None
        try:
            cp = self._cp
            if cp is None:
                # Custom profiles are admitted without the availability check,
                # so an unload can unbind the charger before this point.
                raise HomeAssistantError("charger is not connected")
            request = cp.build_custom_profile_request(connector_id, profile)
            op.phase = "sent"
            try:
                result = await self._classified_call(request)
            except asyncio.CancelledError as ex:
                cancelled = self._cancelled_result(ex)
                if not cancelled.conclusively_not_applied:
                    async with connector.lock:
                        if connector.active_slot is not None:
                            candidate = connector.slots[connector.active_slot]
                            if candidate.state is SlotState.OWNED:
                                candidate.state = SlotState.UNCERTAIN
                                self._schedule_cleanup(connector_id, candidate.index)
                                self._schedule_save()
                    self._notify()
                raise
            status = self._status(result.response)
            async with connector.lock:
                if connector.active_slot is not None:
                    candidate = connector.slots[connector.active_slot]
                    if candidate.state is SlotState.OWNED:
                        managed = candidate
                if result.outcome is CallOutcome.SUCCESS and status == "accepted":
                    if managed is not None:
                        managed.state = SlotState.PENDING_CLEAR
                        self._schedule_save()
                elif (
                    result.outcome is CallOutcome.SUCCESS
                    or result.conclusively_not_applied
                ):
                    self._log_call_failure("custom set", connector_id, result, status)
                    raise HomeAssistantError(
                        f"charger rejected custom TxProfile ({status or result.outcome.value})"
                    )
                else:
                    self._log_call_failure("custom set", connector_id, result, status)
                    if managed is not None:
                        managed.state = SlotState.UNCERTAIN
                        self._schedule_cleanup(connector_id, managed.index)
                        self._schedule_save()
                    raise HomeAssistantError(
                        f"custom TxProfile outcome is uncertain ({result.outcome.value})"
                    )
            if managed is None:
                return
            try:
                clear_request = self._cp.build_session_clear_request(
                    session_profile_id(connector_id, managed.index)
                )
            except Exception as ex:
                self._schedule_cleanup(connector_id, managed.index)
                self._schedule_save()
                self._notify()
                raise HomeAssistantError(
                    "custom TxProfile was accepted but the managed clear "
                    f"could not be built: {ex}"
                ) from ex
            try:
                clear = await self._classified_call(clear_request)
            except asyncio.CancelledError as ex:
                cancelled = self._cancelled_result(ex)
                async with connector.lock:
                    managed.state = (
                        SlotState.PENDING_CLEAR
                        if cancelled.conclusively_not_applied
                        else SlotState.UNCERTAIN
                    )
                    self._schedule_cleanup(connector_id, managed.index)
                    self._schedule_save()
                self._notify()
                raise
            clear_status = self._status(clear.response)
            success = clear.outcome is CallOutcome.SUCCESS and clear_status in {
                "accepted",
                "unknown",
            }
            async with connector.lock:
                if success:
                    self._clean_slot(connector_id, managed)
                else:
                    managed.state = (
                        SlotState.UNCERTAIN
                        if clear.uncertain
                        else SlotState.PENDING_CLEAR
                    )
                    self._schedule_cleanup(connector_id, managed.index)
                self._schedule_save()
                self._notify()
            if not success:
                self._log_call_failure(
                    "custom handoff clear",
                    connector_id,
                    clear,
                    clear_status,
                    profile_id=session_profile_id(connector_id, managed.index),
                )
                raise HomeAssistantError(
                    "custom TxProfile was accepted but the managed profile "
                    "could not be confirmed absent"
                )
        finally:
            await self._release(connector_id, op)

    async def async_reset(
        self, connector_id: int | None = None, *, force: bool = False
    ) -> None:
        """Clear recorded session ids, or explicitly discard them unsafely."""
        if force:
            await self._force_release(connector_id)
            return
        if connector_id is None:
            await self._barrier_clear(include_station=False, broad_v16=False)
            return
        connector_id = self._configured_connector_id(connector_id)
        connector = self._connector(connector_id)
        op = await self._admit(connector.connector_id, "reset")
        try:
            failures: list[int] = []
            for slot in connector.slots:
                if not slot.dirty:
                    continue
                previous_state = slot.state
                slot.state = SlotState.PENDING_CLEAR
                try:
                    request = self._cp.build_session_clear_request(
                        session_profile_id(connector.connector_id, slot.index)
                    )
                    result = await self._classified_call(request)
                except asyncio.CancelledError as ex:
                    cancelled = self._cancelled_result(ex)
                    slot.state = (
                        previous_state
                        if cancelled.conclusively_not_applied
                        else SlotState.UNCERTAIN
                    )
                    if not cancelled.conclusively_not_applied:
                        self._schedule_cleanup(connector.connector_id, slot.index)
                    self._schedule_save()
                    raise
                except Exception as ex:
                    _LOGGER.warning(
                        "%s[%s]: session-profile reset failed before classification: %s",
                        self.cp_id,
                        connector.connector_id,
                        ex,
                    )
                    slot.state = SlotState.UNCERTAIN
                    self._schedule_cleanup(connector.connector_id, slot.index)
                    failures.append(slot.index)
                    continue
                status = self._status(result.response)
                if result.outcome is CallOutcome.SUCCESS and status in {
                    "accepted",
                    "unknown",
                }:
                    self._clean_slot(connector.connector_id, slot)
                else:
                    if result.uncertain:
                        slot.state = SlotState.UNCERTAIN
                        self._schedule_cleanup(connector.connector_id, slot.index)
                    else:
                        slot.state = previous_state
                    self._log_call_failure(
                        "reset clear",
                        connector.connector_id,
                        result,
                        status,
                        profile_id=session_profile_id(
                            connector.connector_id, slot.index
                        ),
                    )
                    failures.append(slot.index)
            self._schedule_save()
            self._sync_repair_issues(connector.connector_id)
            self._notify()
            if failures:
                raise HomeAssistantError(
                    f"could not clear session profile slots {failures}"
                )
        finally:
            await self._release(connector.connector_id, op)

    async def _force_release(self, connector_id: int | None) -> None:
        if connector_id is not None:
            connector_id = self._configured_connector_id(connector_id)
        async with self._gate_lock:
            if self._barrier_owner is not None or self._active_operations:
                raise HomeAssistantError("session-limit operation pending")
            targets = (
                [self._connector(connector_id)]
                if connector_id is not None
                else list(self._connectors.values())
            )
            _LOGGER.warning(
                "%s: UNSAFE force release of session-limit records for %s",
                self.cp_id,
                connector_id if connector_id is not None else "all connectors",
            )
            for connector in targets:
                connector.operation_generation += 1
                for slot in connector.slots:
                    task = self._cleanup_tasks.pop(
                        (connector.connector_id, slot.index), None
                    )
                    if task:
                        task.cancel()
                    self._clean_slot(connector.connector_id, slot)
                connector.active_slot = (
                    0 if connector.current_session is not None else None
                )
                self._quarantine.pop(connector.connector_id, None)
                self._sync_repair_issues(connector.connector_id)
            if connector_id is None:
                self._quarantine.clear()
        self._schedule_save()
        self._notify()

    async def _barrier_clear(self, *, include_station: bool, broad_v16: bool) -> None:
        owner = object()
        timed_out = False
        async with self._gate_lock:
            if self._barrier_owner is not None:
                raise HomeAssistantError("charge-point-wide operation pending")
            self._barrier_owner = owner
        generation = self._generation
        self._notify()
        try:
            try:
                await asyncio.wait_for(self._drained.wait(), SESSION_DRAIN_TIMEOUT)
            except TimeoutError as ex:
                timed_out = True
                async with self._gate_lock:
                    if self._barrier_owner is owner:
                        self._release_barrier_when_drained = True
                raise HomeAssistantError(
                    "timed out draining session-limit operations; admission remains closed"
                ) from ex

            failures: list[str] = []
            cp = self._cp
            if cp is None:
                raise HomeAssistantError("charger is not connected")
            self._assert_barrier_connection(owner, cp, generation)
            if include_station:
                try:
                    interrupted, ok = await self._run_owned(
                        cp.clear_profile(), f"ocpp-session-station-clear-{self.cp_id}"
                    )
                    if interrupted:
                        ok = False
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    _LOGGER.warning(
                        "%s: station/broad charging-profile clear failed: %s",
                        self.cp_id,
                        ex,
                    )
                    ok = False
                self._assert_barrier_connection(owner, cp, generation)
                if not ok:
                    _LOGGER.warning(
                        "%s: station/broad charging-profile clear was not confirmed",
                        self.cp_id,
                    )
                    failures.append("station/broad profile clear")
                    if broad_v16 and cp._ocpp_version == "1.6":
                        # The legacy bool erases whether failure was a charger
                        # rejection or a timeout. Treat it as ambiguous: the
                        # broad request may have removed any owned profile.
                        for connector_id, connector in self._connectors.items():
                            async with connector.lock:
                                for slot in connector.slots:
                                    if slot.dirty:
                                        slot.state = SlotState.UNCERTAIN
                                        self._schedule_cleanup(connector_id, slot.index)
                elif broad_v16 and cp._ocpp_version == "1.6":
                    for connector in self._connectors.values():
                        async with connector.lock:
                            for slot in connector.slots:
                                self._clean_slot(connector.connector_id, slot)
            if not (broad_v16 and cp._ocpp_version == "1.6" and include_station):
                for connector_id, connector in sorted(self._connectors.items()):
                    for slot in connector.slots:
                        if not slot.dirty:
                            continue
                        self._assert_barrier_connection(owner, cp, generation)
                        profile_id = session_profile_id(connector_id, slot.index)
                        previous_state = slot.state
                        async with connector.lock:
                            slot.state = SlotState.PENDING_CLEAR
                            self._schedule_save()
                        try:
                            request = cp.build_session_clear_request(profile_id)
                            result = await self._classified_call(request)
                        except asyncio.CancelledError as ex:
                            cancelled = self._cancelled_result(ex)
                            async with connector.lock:
                                slot.state = (
                                    previous_state
                                    if cancelled.conclusively_not_applied
                                    else SlotState.UNCERTAIN
                                )
                                self._schedule_save()
                            raise
                        except Exception as ex:
                            _LOGGER.warning(
                                "%s[%s]: exact session-profile clear %s failed "
                                "before classification: %s",
                                self.cp_id,
                                connector_id,
                                profile_id,
                                ex,
                            )
                            async with connector.lock:
                                slot.state = SlotState.UNCERTAIN
                                self._schedule_cleanup(connector_id, slot.index)
                                self._schedule_save()
                            failures.append(f"connector {connector_id} id {profile_id}")
                            continue
                        if result.uncertain and self._generation != generation:
                            # The boundary interrupted this clear: the id may
                            # still exist, so leave it for the next cleanup.
                            async with connector.lock:
                                slot.state = SlotState.UNCERTAIN
                                self._schedule_save()
                        self._assert_barrier_connection(owner, cp, generation)
                        status = self._status(result.response)
                        async with connector.lock:
                            if result.outcome is CallOutcome.SUCCESS and status in {
                                "accepted",
                                "unknown",
                            }:
                                self._clean_slot(connector_id, slot)
                            else:
                                if result.uncertain:
                                    slot.state = SlotState.UNCERTAIN
                                    self._schedule_cleanup(connector_id, slot.index)
                                else:
                                    slot.state = previous_state
                                self._log_call_failure(
                                    "charge-point-wide clear",
                                    connector_id,
                                    result,
                                    status,
                                    profile_id=profile_id,
                                )
                                failures.append(
                                    f"connector {connector_id} id {profile_id}"
                                )
            self._schedule_save()
            for connector_id in self._connectors:
                self._sync_repair_issues(connector_id)
            self._notify()
            if failures:
                raise HomeAssistantError(
                    "profile clear was partial: " + ", ".join(failures)
                )
        finally:
            if not timed_out:
                async with self._gate_lock:
                    if self._barrier_owner is owner:
                        self._barrier_owner = None
                self._notify()

    def _assert_barrier_connection(
        self, owner: object, charge_point: Any, generation: int
    ) -> None:
        """Refuse to classify a clear against a superseded connection."""
        if (
            self._barrier_owner is not owner
            or self._cp is not charge_point
            or self._generation != generation
        ):
            raise HomeAssistantError(
                "charger connection changed during charging-profile clear"
            )

    def _repair_id(self, connector_id: int, kind: str) -> str:
        return f"session_limits_{self.cp_id}_{connector_id}_{kind}"

    def _sync_repair_issues(self, connector_id: int | None = None) -> None:
        targets = (
            [connector_id]
            if connector_id is not None
            else set(self._connectors) | set(self._quarantine)
        )
        for conn in targets:
            state = self._connectors.get(conn)
            exhausted = bool(state and all(slot.dirty for slot in state.slots))
            issue_id = self._repair_id(conn, "exhausted")
            if exhausted:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="session_limit_slots_exhausted",
                    translation_placeholders={
                        "charger": self.cpid,
                        "connector": str(conn),
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            quarantine_id = self._repair_id(conn, "quarantine")
            if self._quarantine.get(conn):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    quarantine_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="session_limit_record_quarantined",
                    translation_placeholders={
                        "charger": self.cpid,
                        "connector": str(conn) if conn else "unknown",
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, quarantine_id)
            stalled_id = self._repair_id(conn, "cleanup_stalled")
            stalled = any(
                connector_id == conn and generation == self._generation
                for connector_id, _slot, generation in self._cleanup_exhausted
            )
            if stalled:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    stalled_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="session_limit_cleanup_stalled",
                    translation_placeholders={
                        "charger": self.cpid,
                        "connector": str(conn),
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, stalled_id)

    def _notify(self, connector_id: int | None = None) -> None:
        """Refresh only this controller's session-number entities."""
        if connector_id is None:
            entity_ids = {
                entity_id
                for registered in self._entity_ids.values()
                for entity_id in registered
            }
        else:
            entity_ids = set(self._entity_ids.get(connector_id, ()))
        if entity_ids:
            async_dispatcher_send(self.hass, DATA_UPDATED, entity_ids)

    async def async_shutdown(self) -> None:
        """Cancel controller-owned work without releasing dirty ids."""
        self._ready = False
        # Unbind first so an interrupted barrier reports a changed connection
        # rather than classifying against an object that is going away.
        self._cp = None
        if self._boot_timeout_task:
            self._boot_timeout_task.cancel()
        tasks = list(self._cleanup_tasks.values())
        self._cleanup_tasks.clear()
        current = asyncio.current_task()
        tasks.extend(self._owned_tasks)
        tasks = [task for task in dict.fromkeys(tasks) if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._notify()
