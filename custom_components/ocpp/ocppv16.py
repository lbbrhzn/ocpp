"""Representation of a OCPP 1.6 charging station."""

from datetime import datetime, timedelta, UTC
import hashlib
import logging
import math

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import UnitOfTime
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify
import voluptuous as vol
from websockets.asyncio.server import ServerConnection

from ocpp.routing import on
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    Measurand,
    MessageTrigger,
    Phase,
    ReadingContext,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnlockStatus,
)

from .chargepoint import (
    OcppVersion,
    MeasurandValue,
    SetVariableResult,
)
from .chargepoint import ChargePoint as cp

from .enums import (
    ConfigurationKey as ckey,
    HAChargerDetails as cdet,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    OcppMisc as om,
    Profiles as prof,
)

from .const import (
    CentralSystemSettings,
    ChargerSystemSettings,
    DEFAULT_MEASURAND,
    DEFAULT_MAX_CURRENT,
    DOMAIN,
    HA_ENERGY_UNIT,
    MEASURANDS,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)


def _to_message_trigger(name: str) -> MessageTrigger | None:
    if isinstance(name, MessageTrigger):
        return name
    key = str(name).strip().replace(" ", "").replace("_", "").lower()
    mapping = {
        "bootnotification": MessageTrigger.boot_notification,
        "heartbeat": MessageTrigger.heartbeat,
        "metervalues": MessageTrigger.meter_values,
        "statusnotification": MessageTrigger.status_notification,
        "diagnosticsstatusnotification": MessageTrigger.diagnostics_status_notification,
        "firmwarestatusnotification": MessageTrigger.firmware_status_notification,
    }
    return mapping.get(key)


# Charge-rate defaults plus conservative electrical conversion fallbacks.
_DEFAULT_LIMIT_AMPS = DEFAULT_MAX_CURRENT
_DEFAULT_LIMIT_WATTS = 22000
_DEFAULT_LINE_VOLTAGE = 230.0
_DEFAULT_PHASES = 1

# Limit connectors to prevent OOM in case a corrupted charger reports an invalid number.
_MAX_CONNECTORS = 10

# Persisted transaction identity: the last id handed out (so ids stay unique
# across restarts) and each running transaction's start time (so a session
# adopted after a restart keeps its real duration). Best effort by design: a
# handler never waits for the write, because charging must not depend on
# Home Assistant's disk.
_TX_STORE_VERSION = 1
_TX_STORE_SAVE_DELAY = 1.0

# Connector statuses that prove a transaction is running, and ones that prove
# the connector has none. Anything else - Faulted, Preparing - says nothing
# either way about a transaction the connector was holding.
_TX_RUNNING_STATUSES = (
    ChargePointStatus.charging.value,
    ChargePointStatus.suspended_ev.value,
    ChargePointStatus.suspended_evse.value,
)
_TX_ENDED_STATUSES = (
    ChargePointStatus.available.value,
    ChargePointStatus.finishing.value,
    ChargePointStatus.unavailable.value,
    ChargePointStatus.reserved.value,
)
# Ids adopted from a charger above this are outside the range the integration
# allocates in, so there is nothing to stay clear of.
_TX_ID_CEILING = 2**31 - 2


def tx_store_key(entry_id: str, cp_id: str) -> str:
    """Return the storage key for a charge point's transaction state.

    slugify alone would collide: "CP-A", "CP_A" and "CP A" all become "cp_a",
    and two chargers of one entry would then overwrite each other's state. A
    digest of the raw id keeps the key readable and distinct.
    """
    digest = hashlib.sha256(cp_id.encode()).hexdigest()[:8]
    return f"{DOMAIN}.v16_transactions.{entry_id}.{slugify(cp_id)}_{digest}"


_AMPS_UNIT_TOKENS = frozenset({"current", "a", "amp", "amps", "ampere", "amperes"})
_WATTS_UNIT_TOKENS = frozenset({"power", "w", "watt", "watts"})
_PHASE_KEY_GROUPS = (
    frozenset({Phase.l1.value, Phase.l2.value, Phase.l3.value}),
    frozenset({Phase.l1_n.value, Phase.l2_n.value, Phase.l3_n.value}),
    frozenset({Phase.l1_l2.value, Phase.l2_l3.value, Phase.l3_l1.value}),
)


def _allowed_charging_rate_units(units_resp: str | None) -> tuple[bool, bool]:
    """Parse ChargingScheduleAllowedChargingRateUnit into (amps, watts) support."""
    if not units_resp:
        return True, False
    tokens = {
        tok.strip().lower()
        for tok in str(units_resp).replace(";", ",").split(",")
        if tok.strip()
    }
    supports_amps = bool(tokens & _AMPS_UNIT_TOKENS)
    supports_watts = bool(tokens & _WATTS_UNIT_TOKENS)
    if not supports_amps and not supports_watts:
        return True, False
    return supports_amps, supports_watts


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(
        self,
        id: str,
        connection: ServerConnection,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        charger: ChargerSystemSettings,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(
            id,
            connection,
            OcppVersion.V16,
            hass,
            entry,
            central,
            charger,
        )
        self._active_tx: dict[int, int] = {}  # connector_id -> transaction_id
        self._ended_tx: dict[int, int] = {}  # connector_id -> last stopped tx
        # Transaction identity and timing (#2123). The id was int(time.time())
        # and doubled as the session start epoch: two connectors starting in
        # the same second collided, and a charger-supplied id adopted by the
        # self-heal path made the session timer count from a bogus reference.
        self._last_tx_id: int = 0
        self._tx_started_at: dict[int, float] = {}  # connector_id -> epoch
        # Connectors whose transaction state could not be resolved from a
        # StopTransaction: timer frozen, settled by the next status report.
        self._tx_indeterminate: set[int] = set()
        # connector_id -> (tx_id, started_at) as persisted before a restart
        self._persisted_tx: dict[int, tuple[int, float]] = {}
        # Payloads of StopTransactions that could not be attributed yet, each
        # with the held connectors it could belong to. One is applied only when
        # a settled connector is its sole possible owner; otherwise the final
        # figures are omitted rather than knowingly cross-applied.
        self._pending_stops: list[dict] = []
        # Not atomic on purpose: Home Assistant's atomic writes fsync, which it
        # documents as able to block for seconds, and this state is best
        # effort - the loader tolerates a torn file and the clock still bounds
        # new ids. An fsync per session on an SD-card install is not worth it.
        self._tx_store = Store(
            hass, _TX_STORE_VERSION, tx_store_key(entry.entry_id, id)
        )
        self._tx_store_load = None

    # ------------------------------------------------------------------
    # Transaction identity, timing and containment (#2123)
    # ------------------------------------------------------------------

    async def start(self):
        """Start the charge point once its persisted transaction state is in.

        The message loop would otherwise race the load: a StartTransaction
        could allocate an id below the persisted ceiling, and MeterValues
        could settle on an estimated start for the very session whose real
        start was persisted. Writes stay asynchronous; only the one-time load
        is waited for, and a broken store resolves immediately.
        """
        await self._async_tx_store_ready()
        await super().start()

    async def _async_tx_store_ready(self) -> None:
        """Wait for the one-time load; safe to call repeatedly."""
        self._ensure_tx_store_loaded()
        await self._tx_store_load

    def _ensure_tx_store_loaded(self) -> None:
        """Start loading persisted transaction state once, without blocking.

        start() awaits the load before the first message; handlers call this
        as a backstop for paths that bypass start(), where the reconciliation
        in the loader repairs anything decided before it finished.
        """
        if self._tx_store_load is None:
            self._tx_store_load = self.hass.async_create_task(
                self._async_load_tx_store()
            )

    async def _async_load_tx_store(self) -> None:
        """Adopt the persisted last id and start times, tolerating any failure."""
        try:
            data = await self._tx_store.async_load()
        except Exception as ex:  # a broken store must never stop charging
            _LOGGER.warning("%s: could not load transaction state: %s", self.id, ex)
            data = None
        if not isinstance(data, dict):
            return
        try:
            persisted_last = int(data.get("last_tx_id", 0) or 0)
        except (TypeError, ValueError):
            persisted_last = 0
        # Never hand out an id at or below one returned before the restart.
        self._last_tx_id = max(self._last_tx_id, persisted_last)
        connectors = data.get("connectors", {})
        if not isinstance(connectors, dict):
            return
        for key, item in connectors.items():
            try:
                conn, tx, started = (
                    int(key),
                    int(item["tx_id"]),
                    float(item["started_at"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._note_transaction_id(tx)
            # NaN and infinities parse as floats but would make every later
            # session-time calculation raise; treat them as no start at all,
            # which leaves the session with an estimated start instead.
            if not math.isfinite(started) or started <= 0:
                continue
            self._persisted_tx[conn] = (tx, started)
            # A session adopted before this finished settled on an estimated
            # start; if it is the persisted one, give it its real start.
            metric = self._metrics[(conn, csess.session_time)]
            if self._active_tx.get(conn) == tx and metric.extra_attr.get(
                "start_time_estimated"
            ):
                self._set_session_start(conn, started, estimated=False)

    def _note_transaction_id(self, transaction_id) -> None:
        """Keep allocations clear of an id that is live on some connector.

        A transaction restored from Home Assistant state or adopted from
        MeterValues is live but was not allocated here; without this, a
        StartTransaction in the same second could hand out its id again.
        """
        try:
            tx = int(transaction_id or 0)
        except (TypeError, ValueError):
            return
        if 0 < tx <= _TX_ID_CEILING:
            self._last_tx_id = max(self._last_tx_id, tx)

    def _tx_store_snapshot(self) -> dict:
        """Serialise what a restart needs: the last id and running sessions."""
        return {
            "last_tx_id": int(self._last_tx_id),
            "connectors": {
                str(conn): {
                    "tx_id": int(tx),
                    "started_at": float(self._tx_started_at[conn]),
                }
                for conn, tx in self._active_tx.items()
                if tx and conn in self._tx_started_at
            },
        }

    def _schedule_tx_store_save(self) -> None:
        """Persist best-effort: coalesced, and never awaited by a handler."""
        try:
            self._tx_store.async_delay_save(
                self._tx_store_snapshot, _TX_STORE_SAVE_DELAY
            )
        except Exception as ex:
            _LOGGER.debug("%s: transaction state not persisted: %s", self.id, ex)

    def _allocate_transaction_id(self) -> int:
        """Return an id unique for this charge point, even within one second."""
        tx_id = max(int(time.time()), self._last_tx_id + 1)
        self._last_tx_id = tx_id
        return tx_id

    def _set_session_start(
        self, connector_id: int, started_at: float, *, estimated: bool
    ) -> None:
        """Record when the connector's transaction began, flagging a guess."""
        self._tx_started_at[connector_id] = started_at
        metric = self._metrics[(connector_id, csess.session_time)]
        metric.extra_attr = {"start_time_estimated": True} if estimated else {}

    def _ensure_session_start(self, connector_id: int, transaction_id: int) -> float:
        """Return the transaction's start time, adopting one if none is known.

        A transaction learned from MeterValues rather than StartTransaction has
        no recorded start. If it is the one persisted before a restart, its
        real start is used; otherwise the first observation is, and the
        session-time sensor says so with `start_time_estimated`.
        """
        started = self._tx_started_at.get(connector_id)
        if started is not None:
            return started
        persisted = self._persisted_tx.get(connector_id)
        if persisted is not None and persisted[0] == int(transaction_id or 0):
            self._set_session_start(connector_id, persisted[1], estimated=False)
        else:
            self._set_session_start(connector_id, time.time(), estimated=True)
        return self._tx_started_at[connector_id]

    def _metric_transaction(self, connector_id: int) -> int:
        """Return the transaction id the connector's metric holds, or 0."""
        try:
            return int(self._metrics[(connector_id, csess.transaction_id)].value or 0)
        except (TypeError, ValueError):
            return 0

    def _live_connectors(self) -> list[int]:
        """Return the connectors that have a transaction recorded anywhere."""
        live = {c for c, tx in self._active_tx.items() if tx}
        for conn in range(1, int(self.num_connectors or 1) + 1):
            if self._metric_transaction(conn):
                live.add(conn)
        return sorted(live)

    def _resolve_stop_connector(self, transaction_id: int) -> int | None:
        """Find the connector a StopTransaction belongs to, or None if unsure.

        Never guesses: applying a stop to the wrong connector ends a session
        that is still running and files the other one's energy against it.
        None means the caller must contain the uncertainty instead.
        """
        tx = int(transaction_id or 0)
        by_map = [c for c, t in self._active_tx.items() if t and int(t) == tx]
        if len(by_map) == 1:
            return by_map[0]
        if by_map:
            return None  # duplicate ids on several connectors: cannot tell
        by_metric = [
            c
            for c in range(1, int(self.num_connectors or 1) + 1)
            if self._metric_transaction(c) == tx
        ]
        if len(by_metric) == 1:
            return by_metric[0]
        if by_metric:
            return None
        live = self._live_connectors()
        if len(live) == 1:
            _LOGGER.info(
                "%s: StopTransaction for unrecorded id=%s applied to connector %s, "
                "the only one with a transaction",
                self.id,
                tx,
                live[0],
            )
            return live[0]
        return None

    def _clear_transaction_state(
        self, connector_id: int, transaction_id: int, stop_reason=None
    ) -> None:
        """Forget the connector's transaction: the per-connector part of a stop."""
        self._ended_tx[connector_id] = int(transaction_id or 0)
        self._active_tx[connector_id] = 0
        self._tx_started_at.pop(connector_id, None)
        self._tx_indeterminate.discard(connector_id)
        self._metrics[(connector_id, cstat.id_tag)].value = ""
        self._metrics[(connector_id, csess.transaction_id)].value = 0
        self._metrics[(connector_id, cstat.stop_reason)].value = stop_reason

    def _apply_stop_energy(self, connector_id: int, meter_stop) -> None:
        """Set the session's energy from the meter reading at its stop."""
        ms_key = (connector_id, csess.meter_start)
        if self._metrics[ms_key].value is None or self._charger_reports_session_energy:
            return
        try:
            session_kwh = int(meter_stop) / 1000.0 - float(self._metrics[ms_key].value)
        except Exception:
            session_kwh = 0.0
        self._metrics[(connector_id, csess.session_energy)].value = session_kwh

    def _forget_stop_candidate(self, connector_id: int) -> None:
        """Record that this connector can no longer own any pending stop.

        Used when the connector proves its transaction is still running: the
        stop was not its, so it is kept for the remaining candidates.
        """
        for pending in self._pending_stops:
            pending["candidates"].discard(connector_id)
        self._pending_stops = [p for p in self._pending_stops if p["candidates"]]

    def _invalidate_pending_stops(self, connector_id: int, evidence: str) -> None:
        """Drop every pending stop this connector could have owned.

        Evidence that a candidate's transaction ended by another route - its
        own StopTransaction, its closing values, a new transaction starting on
        it - means an unattributed stop naming it may have been a malformed
        duplicate for it, so no other candidate can be shown to own it.
        """
        remaining = [
            p for p in self._pending_stops if connector_id not in p["candidates"]
        ]
        dropped = len(self._pending_stops) - len(remaining)
        self._pending_stops = remaining
        if dropped:
            _LOGGER.warning(
                "%s: connector %s %s; %s unattributed stop(s) that could have been "
                "its can no longer be attributed and are discarded",
                self.id,
                connector_id,
                evidence,
                dropped,
            )

    def transaction_is_unsafe(self, connector_id: int | None) -> bool:
        """Return whether a remote stop of this connector would be refused.

        True while the connector is held, and also while it shares its id with
        a held connector: the same rule stop_transaction applies, so an
        entity gating on this never offers a stop that cannot be sent.
        """
        if connector_id is None:
            return False
        if connector_id in self._tx_indeterminate:
            return True
        tx = int(self._active_tx.get(connector_id, 0) or 0)
        return bool(tx) and tx in self._unsafe_transaction_ids()

    def _claim_pending_stop(self, connector_id: int) -> dict | None:
        """Return the one unattributed stop this connector can own, if any.

        A stop is applied only when the settled connector is a candidate for
        exactly one pending stop and no other evidence has since shown how a
        candidate's transaction ended. With two unattributed stops outstanding,
        neither can be shown to belong to whichever connector reports idle
        first, so both sessions keep their last periodic figures rather than
        knowingly receiving another session's final reading and reason.
        """
        mine = [p for p in self._pending_stops if connector_id in p["candidates"]]
        self._forget_stop_candidate(connector_id)
        if len(mine) == 1:
            self._pending_stops = [p for p in self._pending_stops if p is not mine[0]]
            return mine[0]
        if mine:
            _LOGGER.warning(
                "%s: %s unattributed stops could belong to connector %s; its final "
                "energy and stop reason are not recorded",
                self.id,
                len(mine),
                connector_id,
            )
        return None

    def _unsafe_transaction_ids(self) -> set[int]:
        """Return ids a held connector may still own; never stop these remotely."""
        unsafe = set()
        for conn in self._tx_indeterminate:
            unsafe.add(int(self._active_tx.get(conn, 0) or 0))
            unsafe.add(self._metric_transaction(conn))
        unsafe.discard(0)
        return unsafe

    def _resolve_indeterminate(
        self, connector_id: int, *, running: bool, claim: bool = True
    ) -> None:
        """Settle a connector whose transaction state was held as unresolved.

        `claim` is True only for a status report saying the connector is idle
        with nothing else known: then a pending stop may be attributed to it.
        Any other evidence that its transaction ended (closing values) passes
        claim=False, and the stops it could have owned are discarded instead.
        """
        if connector_id not in self._tx_indeterminate:
            return
        if running:
            self._tx_indeterminate.discard(connector_id)
            # It did not end, so no unattributed stop can be its.
            self._forget_stop_candidate(connector_id)
            _LOGGER.info(
                "%s: connector %s is still in its transaction", self.id, connector_id
            )
        else:
            _LOGGER.info(
                "%s: connector %s reports no transaction; ending the one it held",
                self.id,
                connector_id,
            )
            if claim:
                pending = self._claim_pending_stop(connector_id)
            else:
                pending = None
                self._invalidate_pending_stops(
                    connector_id, "sent its own closing meter values"
                )
            self._clear_transaction_state(
                connector_id,
                self._active_tx.get(connector_id, 0),
                pending.get("reason") if pending else None,
            )
            if pending is not None:
                self._apply_stop_energy(connector_id, pending.get("meter_stop"))
            self._zero_flow_measurands(connector_id)
        self._schedule_tx_store_save()

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        resp = None

        try:
            req = call.GetConfiguration(key=["NumberOfConnectors"])
            resp = await self.call(req)
        except Exception:
            resp = None

        cfg = None
        if resp is not None:
            cfg = getattr(resp, "configuration_key", None)

            if (
                cfg is None
                and isinstance(resp, list | tuple)
                and len(resp) >= 3
                and isinstance(resp[2], dict)
            ):
                cfg = resp[2].get("configurationKey") or resp[2].get(
                    "configuration_key"
                )

        if cfg:
            for kv in cfg:
                k = getattr(kv, "key", None)
                v = getattr(kv, "value", None)
                if k is None and isinstance(kv, dict):
                    k = kv.get("key")
                    v = kv.get("value")
                if k == "NumberOfConnectors" and v not in (None, ""):
                    try:
                        n = int(str(v).strip())
                        if n > 0:
                            return min(n, _MAX_CONNECTORS)
                    except (ValueError, TypeError):
                        pass

        return 1

    async def get_heartbeat_interval(self):
        """Retrieve heartbeat interval from the charger and store it."""
        await self.get_configuration(ckey.heartbeat_interval)

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""

        def _filter_measurands(raw_csv: str) -> str:
            """Keep only compliant measurands found as tokens in the charger's string."""
            # Protect against empty lists and the "Unknown" sentinel (checked by test_measurands_manual_set_rejected_returns_empty)
            if not raw_csv or raw_csv.strip().lower() == "unknown":
                return ""

            matched = []
            for token in raw_csv.split(","):
                token = token.strip()
                if not token:
                    continue

                for m in MEASURANDS:
                    # Token-aware match: Exact match OR prefix match with a dot (e.g. "Voltage.L1")
                    if token == m or token.startswith(f"{m}."):
                        if m not in matched:
                            matched.append(m)
                        break  # Match found for this token, move to the next one

            if not matched:
                _LOGGER.debug(
                    "Charger '%s' returned no valid measurands; falling back to %s.",
                    self.id,
                    DEFAULT_MEASURAND,
                )
                return DEFAULT_MEASURAND

            return ",".join(matched)

        all_measurands = self.settings.monitored_variables or ""
        autodetect_measurands = bool(self.settings.monitored_variables_autoconfig)
        key = ckey.meter_values_sampled_data

        desired_csv = all_measurands.strip().strip(",")
        cfg_ok = {ConfigurationStatus.accepted, ConfigurationStatus.reboot_required}

        effective_csv: str = ""

        if autodetect_measurands:
            if desired_csv:
                _LOGGER.debug(
                    "'%s' attempting CSV set for measurands: %s", self.id, desired_csv
                )
                try:
                    resp = await self.call(
                        call.ChangeConfiguration(key=key, value=desired_csv)
                    )
                    if getattr(resp, "status", None) in cfg_ok:
                        _LOGGER.debug(
                            "'%s' measurands CSV accepted with status=%s",
                            self.id,
                            resp.status,
                        )
                        effective_csv = desired_csv
                    else:
                        _LOGGER.debug(
                            "'%s' measurands CSV rejected with status=%s; falling back to GetConfiguration",
                            self.id,
                            getattr(resp, "status", None),
                        )
                except Exception as ex:
                    _LOGGER.debug(
                        "get_supported_measurands CSV set raised for '%s': %s",
                        self.id,
                        ex,
                    )

            # Read from charger and filter it using lenient logic
            chgr_csv = await self.get_configuration(key)
            chgr_csv = _filter_measurands(chgr_csv)

            if not effective_csv:
                _LOGGER.debug(
                    "'%s' measurands not configurable by integration", self.id
                )
                _LOGGER.debug("'%s' allowed measurands: '%s'", self.id, chgr_csv)
                return chgr_csv

            _LOGGER.debug(
                "Returning accepted measurands for '%s': '%s'", self.id, effective_csv
            )
            await self.configure(key, effective_csv)
            return effective_csv

        # Non-autodetect path:
        if desired_csv:
            try:
                resp = await self.call(
                    call.ChangeConfiguration(key=key, value=desired_csv)
                )
                _LOGGER.debug(
                    "'%s' measurands set manually to %s", self.id, desired_csv
                )
                if getattr(resp, "status", None) in cfg_ok:
                    effective_csv = desired_csv
                else:
                    _LOGGER.debug(
                        "'%s' manual measurands set not accepted (status=%s); using charger's value",
                        self.id,
                        getattr(resp, "status", None),
                    )
                    effective_csv = await self.get_configuration(key)
            except Exception as ex:
                _LOGGER.debug(
                    "Manual measurands set failed for '%s': %s; using charger's value",
                    self.id,
                    ex,
                )
                effective_csv = await self.get_configuration(key)
        else:
            effective_csv = await self.get_configuration(key)

        # Filter whatever resulted from the manual path
        effective_csv = _filter_measurands(effective_csv)

        if effective_csv:
            _LOGGER.debug("'%s' allowed measurands: '%s'", self.id, effective_csv)
            await self.configure(key, effective_csv)
        else:
            _LOGGER.debug("'%s' measurands not configurable by integration", self.id)

        return effective_csv

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        await self.configure(
            ckey.meter_value_sample_interval,
            str(self.settings.meter_interval),
        )
        await self.configure(
            ckey.clock_aligned_data_interval,
            str(self.settings.idle_interval),
        )

    async def get_supported_features(self) -> prof:
        """Get features supported by the charger."""
        features = prof.NONE
        req = call.GetConfiguration(key=[ckey.supported_feature_profiles])
        resp = await self.call(req)
        try:
            feature_list = (resp.configuration_key[0][om.value]).split(",")
        except (IndexError, KeyError, TypeError):
            feature_list = [""]
        if feature_list[0] == "":
            _LOGGER.warning("No feature profiles detected, defaulting to Core")
            await self.notify_ha("No feature profiles detected, defaulting to Core")
            feature_list = [om.feature_profile_core]

        if self.settings.force_smart_charging:
            _LOGGER.warning("Force Smart Charging feature profile")
            features |= prof.SMART

        for item in feature_list:
            item = item.strip().replace(" ", "")
            if item == om.feature_profile_core:
                features |= prof.CORE
            elif item == om.feature_profile_firmware:
                features |= prof.FW
            elif item == om.feature_profile_smart:
                features |= prof.SMART
            elif item == om.feature_profile_reservation:
                features |= prof.RES
            elif item == om.feature_profile_remote:
                features |= prof.REM
            elif item == om.feature_profile_auth:
                features |= prof.AUTH
            else:
                _LOGGER.warning("Unknown feature profile detected ignoring: %s", item)
                await self.notify_ha(
                    f"Warning: Unknown feature profile detected ignoring {item}"
                )
        return features

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        req = call.TriggerMessage(requested_message=MessageTrigger.boot_notification)
        resp = await self.call(req)
        if resp.status == TriggerMessageStatus.accepted:
            self.triggered_boot_notification = True
            return True
        else:
            self.triggered_boot_notification = False
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        try:
            n = int(self._metrics[0][cdet.connectors].value or 1)
        except Exception:
            n = 1

        # Single connector: only probe 1. Multi: probe 0 then 1..n.
        attempts = [1] if n <= 1 else [0] + list(range(1, n + 1))

        for cid in attempts:
            _LOGGER.debug("trigger status notification for connector=%s", cid)
            try:
                req = call.TriggerMessage(
                    requested_message=MessageTrigger.status_notification,
                    connector_id=int(cid),
                )
                resp = await self.call(req)
                status = getattr(resp, "status", None)
            except Exception as ex:
                _LOGGER.debug("TriggerMessage failed for connector=%s: %s", cid, ex)
                status = None

            if status != TriggerMessageStatus.accepted:
                if cid > 0:
                    _LOGGER.warning("Failed with response: %s", status)
                    # Reduce to the last known-good connector index.
                    self._metrics[0][cdet.connectors].value = max(1, cid - 1)
                    return False
                # If connector 0 is rejected, continue probing numbered connectors.

        return True

    async def trigger_custom_message(
        self,
        requested_message: str | MessageTrigger = "StatusNotification",
    ):
        """Trigger Custom Message."""
        trig = _to_message_trigger(requested_message)
        if trig is None:
            _LOGGER.warning("Unsupported TriggerMessage: %s", requested_message)
            return False

        req = call.TriggerMessage(requested_message=trig)
        resp = await self.call(req)
        _LOGGER.debug("TriggerMessage %s to %s answered: %s", trig, self.id, resp)
        if resp.status != TriggerMessageStatus.accepted:
            _LOGGER.warning("Failed with response: %s", resp.status)
            return False
        return True

    async def clear_profile(
        self,
        conn_id: int | None = None,
        purpose: ChargingProfilePurposeType | None = None,
    ) -> bool:
        """Clear charging profiles (per connector and/or purpose)."""
        try:
            req = call.ClearChargingProfile(
                connector_id=(int(conn_id) if conn_id is not None else None),
                charging_profile_purpose=(purpose.value if purpose else None),
            )
            resp = await self.call(req)
            return resp.status in (
                ClearChargingProfileStatus.accepted,
                ClearChargingProfileStatus.unknown,
            )
        except Exception as ex:
            _LOGGER.debug("ClearChargingProfile raised %s (ignored)", ex)
            return False

    def _lookup_metric(self, measurand: str, conn_id: int):
        """Return a connector metric if it has a value, else None."""
        metrics = getattr(self, "_metrics", None)
        if metrics is None:
            return None
        try:
            target = int(conn_id) if conn_id and int(conn_id) > 0 else 1
        except (TypeError, ValueError):
            target = 1
        # Connector 0 contains legacy/global telemetry. Never fall back to
        # connector 1 for another connector, as that can mix unrelated ports.
        connector_ids = (target, 0)
        for cid in connector_ids:
            key = (cid, measurand)
            if key not in metrics:
                continue
            metric = metrics[key]
            if metric is not None and getattr(metric, "value", None) is not None:
                return metric
        return None

    def _line_voltage(self, conn_id: int) -> float:
        """Return a plausible line-to-neutral voltage, or the 230 V default."""
        metric = self._lookup_metric(Measurand.voltage.value, conn_id)
        if metric is not None:
            try:
                voltage = float(metric.value)
            except (TypeError, ValueError):
                voltage = 0.0
            if 50.0 <= voltage <= 500.0:
                return voltage
        return _DEFAULT_LINE_VOLTAGE

    def _phase_count(self, conn_id: int) -> int:
        """Count electrically active phases; conservatively default to one.

        Some chargers publish placeholders for every phase even on a
        single-phase installation.  Counting those keys turns a 16 A limit
        into 16 A * 230 V * 3 for power-only chargers, although L2 and L3 are
        explicitly reported as zero.  Count only phase values that carry a
        meaningful voltage/current instead.
        """
        measurands = (
            Measurand.voltage.value,
            Measurand.current_import.value,
            Measurand.current_offered.value,
        )
        best = 0
        for measurand in measurands:
            metric = self._lookup_metric(measurand, conn_id)
            if metric is None:
                continue
            phase_values = {
                str(key): value for key, value in (metric.extra_attr or {}).items()
            }
            threshold = 50.0 if measurand == Measurand.voltage.value else 0.1
            for group in _PHASE_KEY_GROUPS:
                n = 0
                for phase in group:
                    if phase not in phase_values:
                        continue
                    try:
                        value = abs(float(phase_values[phase]))
                    except (TypeError, ValueError):
                        continue
                    if value >= threshold:
                        n += 1
                if n > best:
                    best = n
        return best if best > 0 else _DEFAULT_PHASES

    def _amps_to_watts(self, amps: float, conn_id: int) -> float:
        """Convert a current limit to watts for Power-only chargers."""
        return float(
            round(amps * self._line_voltage(conn_id) * self._phase_count(conn_id))
        )

    def _watts_to_amps(self, watts: float, conn_id: int) -> float:
        """Convert a power limit to amps for Current-only chargers."""
        denom = self._line_voltage(conn_id) * self._phase_count(conn_id)
        if denom <= 0:
            return float(_DEFAULT_LIMIT_AMPS)
        return round(watts / denom, 1)

    async def _resolve_charge_rate(
        self,
        limit_amps: int | float | None,
        limit_watts: int | float | None,
        conn_id: int,
    ) -> tuple[str, float, int]:
        """Resolve units, electrical conversion and stack level for managed limits."""
        # Determine allowed unit (default to Amps if not reported)
        units_resp = await self.get_configuration(
            ckey.charging_schedule_allowed_charging_rate_unit
        )
        if not units_resp:
            _LOGGER.debug("Charging rate unit not reported; assuming Amps")

        supports_amps, supports_watts = _allowed_charging_rate_units(units_resp)
        # Watt-only chargers (Huawei FusionCharge reports "Power") must not
        # fall back to the old limit_watts=22000 default when the HA number
        # entity passes only limit_amps.
        if supports_amps and not supports_watts:
            use_amps = True
        elif supports_watts and not supports_amps:
            use_amps = False
        else:
            use_amps = limit_amps is not None or limit_watts is None

        if use_amps:
            if limit_amps is not None:
                limit_value = float(limit_amps)
            elif limit_watts is not None:
                limit_value = self._watts_to_amps(float(limit_watts), conn_id)
            else:
                limit_value = float(_DEFAULT_LIMIT_AMPS)
        elif limit_watts is not None:
            limit_value = float(limit_watts)
        elif limit_amps is not None:
            limit_value = self._amps_to_watts(float(limit_amps), conn_id)
            _LOGGER.debug(
                "Converted %.1f A to %.0f W for Power-only charger",
                float(limit_amps),
                limit_value,
            )
        else:
            limit_value = float(_DEFAULT_LIMIT_WATTS)

        units_value = (
            ChargingRateUnitType.amps.value
            if use_amps
            else ChargingRateUnitType.watts.value
        )

        try:
            stack_level_resp = await self.get_configuration(
                ckey.charge_profile_max_stack_level
            )
            stack_level = int(stack_level_resp)
        except Exception:
            stack_level = 1

        return units_value, limit_value, stack_level

    @staticmethod
    def _station_charge_rate_request(
        units_value: str, limit_value: float, stack_level: int
    ) -> call.SetChargingProfile:
        """Build the shared station ceiling used by the slider and action."""
        return call.SetChargingProfile(
            connector_id=0,
            cs_charging_profiles={
                om.charging_profile_id: 1000,
                om.stack_level: stack_level,
                om.charging_profile_kind: ChargingProfileKindType.relative.value,
                om.charging_profile_purpose: ChargingProfilePurposeType.charge_point_max_profile.value,
                om.charging_schedule: {
                    om.charging_rate_unit: units_value,
                    om.charging_schedule_period: [
                        {om.start_period: 0, om.limit: limit_value}
                    ],
                },
            },
        )

    async def set_station_charge_rate(self, limit_amps: int | float) -> bool:
        """Set only a station ceiling; transaction defaults cannot replace one."""
        try:
            if not (int(self.supported_features or 0) & prof.SMART):
                raise HomeAssistantError(
                    "Smart charging is not supported by this charger"
                )
            units, limit, stack_level = await self._resolve_charge_rate(
                limit_amps, None, 0
            )
            req = self._station_charge_rate_request(units, limit, stack_level)
            # _get_specific_response already raises CALLERRORs. Be explicit
            # here so preserving the charger's reason does not rely on it.
            resp = await self.call(req, suppress=False)
            status = resp.status
        except Exception as ex:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_variables_error",
                translation_placeholders={"message": str(ex)},
            ) from ex
        if status != ChargingProfileStatus.accepted:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_variables_error",
                translation_placeholders={
                    "message": f"ChargePointMaxProfile: {status}"
                },
            )
        return True

    async def set_charge_rate(
        self,
        limit_amps: int | float | None = None,
        limit_watts: int | float | None = None,
        conn_id: int = 0,
        profile: dict | None = None,
    ) -> bool:
        """Set charge rate."""
        if profile is not None:
            try:
                req = call.SetChargingProfile(
                    connector_id=int(conn_id), cs_charging_profiles=profile
                )
                resp = await self.call(req)
                if resp.status == ChargingProfileStatus.accepted:
                    return True
                _LOGGER.warning("Custom SetChargingProfile rejected: %s", resp.status)
            except Exception as ex:
                _LOGGER.warning("Custom SetChargingProfile failed: %s", ex)
                await self.notify_ha(
                    "Warning: Set charging profile failed with response Exception"
                )
            return False

        if not (int(self.supported_features or 0) & prof.SMART):
            _LOGGER.info("Smart charging is not supported by this charger")
            return False

        units_value, limit_value, stack_level = await self._resolve_charge_rate(
            limit_amps, limit_watts, conn_id
        )

        # Helper to build a simple relative schedule with one period
        def _mk_schedule(_units: str, _limit: float) -> dict:
            return {
                om.charging_rate_unit: _units,
                om.charging_schedule_period: [{om.start_period: 0, om.limit: _limit}],
            }

        # Helper to generate a unique, stable chargingProfileId per purpose+connector
        def _profile_id(purpose: str, cid: int) -> int:
            base = {
                ChargingProfilePurposeType.charge_point_max_profile.value: 1000,
                ChargingProfilePurposeType.tx_default_profile.value: 2000,
                ChargingProfilePurposeType.tx_profile.value: 3000,
            }.get(purpose, 9000)
            try:
                n = int(cid or 0)
            except Exception:
                n = 0
            return base + max(0, n)

        # Try ChargePointMaxProfile (connectorId = 0)
        try:
            req = self._station_charge_rate_request(
                units_value, limit_value, stack_level
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                return True
            _LOGGER.debug(
                "ChargePointMaxProfile not accepted (%s); will continue.",
                resp.status,
            )
        except Exception as ex:
            _LOGGER.debug("ChargePointMaxProfile call raised: %s", ex)

        # Target connector (default 1 if unspecified/0)
        target_cid = int(conn_id) if conn_id and int(conn_id) > 0 else 1

        # Read active transaction on this connector. A held connector's id may
        # belong to a transaction that has already ended, so no profile is
        # bound to it until the charger settles the connector.
        try:
            active_tx_id = int(self._active_tx.get(target_cid, 0) or 0)
        except Exception:
            active_tx_id = 0
        if target_cid in self._tx_indeterminate:
            active_tx_id = 0

        txp_ok = False
        txd_ok = False

        # If an active transaction exists on this connector, try TxProfile first (affects ongoing charging)
        if active_tx_id > 0:
            try:
                txp_stack = max(1, stack_level)  # keep same or higher than defaults
                req = call.SetChargingProfile(
                    connector_id=target_cid,
                    cs_charging_profiles={
                        om.charging_profile_id: _profile_id(
                            ChargingProfilePurposeType.tx_profile.value, target_cid
                        ),
                        om.stack_level: txp_stack,
                        om.charging_profile_kind: ChargingProfileKindType.relative.value,
                        om.charging_profile_purpose: ChargingProfilePurposeType.tx_profile.value,
                        om.charging_schedule: _mk_schedule(units_value, limit_value),
                        # Bind to the ongoing transaction
                        om.transaction_id: active_tx_id,
                    },
                )
                resp = await self.call(req)
                if resp.status == ChargingProfileStatus.accepted:
                    txp_ok = True
                else:
                    _LOGGER.debug("TxProfile not accepted (%s).", resp.status)
            except Exception as ex:
                _LOGGER.debug("TxProfile call raised: %s.", ex)

        # Always attempt TxDefaultProfile as well (for future sessions)
        try:
            tx_stack = max(
                1, stack_level - 1
            )  # slightly lower to avoid overriding TxProfile
            req = call.SetChargingProfile(
                connector_id=target_cid,
                cs_charging_profiles={
                    om.charging_profile_id: _profile_id(
                        ChargingProfilePurposeType.tx_default_profile.value, target_cid
                    ),
                    om.stack_level: tx_stack,
                    om.charging_profile_kind: ChargingProfileKindType.relative.value,
                    om.charging_profile_purpose: ChargingProfilePurposeType.tx_default_profile.value,
                    om.charging_schedule: _mk_schedule(units_value, limit_value),
                },
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                txd_ok = True
            else:
                _LOGGER.debug("Set TxDefaultProfile rejected: %s", resp.status)
                if txp_ok:
                    _LOGGER.debug(
                        f"Note: Active TxProfile applied, but TxDefaultProfile was rejected ({resp.status})."
                    )
        except Exception as ex:
            _LOGGER.debug("Set TxDefaultProfile failed: %s", ex)
            if txp_ok:
                _LOGGER.debug(
                    f"Note: Active TxProfile applied, but TxDefaultProfile failed: {ex}"
                )

        return bool(txp_ok or txd_ok)

    async def set_availability(self, state: bool = True, connector_id: int | None = 0):
        """Change availability."""
        try:
            conn = 0 if connector_id in (None, 0) else int(connector_id)
        except Exception:
            conn = 0

        typ = AvailabilityType.operative if state else AvailabilityType.inoperative
        req = call.ChangeAvailability(connector_id=conn, type=typ)

        try:
            resp = await self.call(req)
        except TimeoutError as ex:
            _LOGGER.debug("ChangeAvailability timed out (conn=%s): %s", conn, ex)
            return False
        except Exception as ex:
            _LOGGER.debug("ChangeAvailability failed (conn=%s): %s", conn, ex)
            return False

        try:
            status = getattr(resp, "status", None)

            # Fallback: some single-connector chargers reject station-level (connectorId=0).
            if status == AvailabilityStatus.rejected and conn == 0:
                try:
                    n = int(getattr(self, "num_connectors", 1) or 1)
                except Exception:
                    n = 1
                if n == 1:
                    _LOGGER.debug(
                        "Station-level ChangeAvailability rejected; retrying on connector 1."
                    )
                    return await self.set_availability(state=state, connector_id=1)

            pending_key = "availability_pending"
            target_str = "Operative" if state else "Inoperative"
            scope_str = "station" if conn == 0 else "connector"

            metric_key = (conn, cstat.status_connector)
            metric = self._metrics.get(metric_key)

            if status == AvailabilityStatus.scheduled:
                info = {
                    "target": target_str,
                    "scope": scope_str,
                    "since": datetime.now(tz=UTC).isoformat(),
                }
                if metric is not None:
                    metric.extra_attr[pending_key] = info

                self.hass.async_create_task(self.update(self.settings.cpid))
                return True

            if status == AvailabilityStatus.accepted:
                if metric is not None:
                    metric.extra_attr.pop(pending_key, None)
                self.hass.async_create_task(self.update(self.settings.cpid))
                return True

            _LOGGER.warning("Failed with response: %s", resp.status)
            return False

        except Exception:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Set availability failed with response {resp.status}"
            )
            return False

    async def start_transaction(self, connector_id: int = 1):
        """Remote start a transaction."""
        _LOGGER.info("Start transaction with remote ID tag: %s", self._remote_id_tag)
        req = call.RemoteStartTransaction(
            connector_id=connector_id, id_tag=self._remote_id_tag
        )
        resp = await self.call(req)
        _LOGGER.debug(
            "RemoteStartTransaction to %s connector=%s answered: %s",
            self.id,
            connector_id,
            resp,
        )
        if resp.status == RemoteStartStopStatus.accepted:
            # Reset id tag if set by on authorize (RFID)
            self._metrics[0][cstat.id_tag.value].value = self._remote_id_tag
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Start transaction failed with response {resp.status}"
            )
            return False

    async def stop_transaction(self, connector_id: int | None = None):
        """Request remote stop of current transaction.

        If connector_id is provided, only stop the transaction running on that connector.
        """
        # Resolve which transaction to stop. An id a held connector may still
        # own is never sent: with duplicate charger-supplied ids the stop could
        # end another connector's running session, and this is the path the
        # Charge Control switch takes, since it always names its connector.
        unsafe = self._unsafe_transaction_ids()
        tx_id = 0
        if connector_id is not None:
            # Per-connector stop: do NOT fall back to other connectors
            try:
                tx_id = int(self._active_tx.get(int(connector_id), 0) or 0)
            except Exception:
                tx_id = 0

            # For single-connector chargers, maintain compatibility with legacy global field
            if tx_id == 0:
                try:
                    n = int(getattr(self, "num_connectors", 0) or 0)
                except Exception:
                    n = 0
                if n == 1 and int(connector_id) in (0, 1):
                    tx_id = int(self.active_transaction_id or 0)

            if int(connector_id) in self._tx_indeterminate or tx_id in unsafe:
                _LOGGER.warning(
                    "%s: not stopping connector %s: its transaction state is "
                    "unresolved until the charger reports its status",
                    self.id,
                    connector_id,
                )
                await self.notify_ha(
                    f"Warning: Stop transaction on connector {connector_id} refused: "
                    "transaction state unresolved"
                )
                return False
        else:
            # Global stop (legacy behaviour): the known active transaction, or
            # any active one - never an id a held connector may still own.
            candidates = [
                int(v)
                for c, v in self._active_tx.items()
                if v and c not in self._tx_indeterminate and int(v) not in unsafe
            ]
            legacy = int(self.active_transaction_id or 0)
            if legacy in unsafe:
                legacy = 0
            if legacy and (legacy in candidates or not self._active_tx):
                tx_id = legacy
            else:
                tx_id = candidates[0] if candidates else 0

        # Nothing to stop - succeed as no-op
        if tx_id == 0:
            return True

        req = call.RemoteStopTransaction(transaction_id=tx_id)
        resp = await self.call(req)
        if resp.status == RemoteStartStopStatus.accepted:
            return True

        _LOGGER.warning("Failed with response: %s", resp.status)
        await self.notify_ha(
            f"Warning: Stop transaction failed with response {resp.status}"
        )
        return False

    async def reset(self, typ: str = ResetType.hard):
        """Hard reset charger unless soft reset requested."""
        self._metrics[0][cstat.reconnects].value = 0
        req = call.Reset(typ)
        resp = await self.call(req)
        if resp.status == ResetStatus.accepted:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Reset failed with response {resp.status}")
            return False

    async def unlock(self, connector_id: int = 1):
        """Unlock charger if requested."""
        req = call.UnlockConnector(connector_id)
        resp = await self.call(req)
        if resp.status == UnlockStatus.unlocked:
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(f"Warning: Unlock failed with response {resp.status}")
            return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available.

        - firmware_url: http/https URL of the new firmware
        - wait_time: hours from now to wait before install
        """
        features = int(self.supported_features or 0)
        if not (features & prof.FW):
            _LOGGER.warning("Charger does not support OCPP firmware updating")
            return False

        schema = vol.Schema(vol.Url())
        try:
            url = schema(firmware_url)
        except vol.MultipleInvalid as e:
            _LOGGER.warning("Failed to parse url: %s", e)
            return False

        try:
            retrieve_time = (
                datetime.now(tz=UTC) + timedelta(hours=max(0, int(wait_time or 0)))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            retrieve_time = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            req = call.UpdateFirmware(location=str(url), retrieve_date=retrieve_time)
            resp = await self.call(req)
            _LOGGER.info("UpdateFirmware response: %s", resp)
            return True
        except Exception as e:
            _LOGGER.error("UpdateFirmware failed: %s", e)
            return False

    async def get_diagnostics(self, upload_url: str):
        """Upload diagnostic data to server from charger."""
        features = int(self.supported_features or 0)
        if features & prof.FW:
            schema = vol.Schema(vol.Url())
            try:
                url = schema(upload_url)
            except vol.MultipleInvalid as e:
                _LOGGER.warning("Failed to parse url: %s", e)
                return
            req = call.GetDiagnostics(location=str(url))
            resp = await self.call(req)
            _LOGGER.info("Response: %s", resp)
            return True
        else:
            _LOGGER.debug(
                "Charger %s does not support ocpp diagnostics uploading",
                self.id,
            )
            return False

    async def data_transfer(self, vendor_id: str, message_id: str = "", data: str = ""):
        """Request vendor specific data transfer from charger."""
        req = call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=data)
        resp = await self.call(req)
        if resp.status == DataTransferStatus.accepted:
            _LOGGER.info(
                "Data transfer [vendorId(%s), messageId(%s), data(%s)] response: %s",
                vendor_id,
                message_id,
                data,
                resp.data,
            )
            self._metrics[0][cdet.data_response].value = datetime.now(tz=UTC)
            self._metrics[0][cdet.data_response].extra_attr = {message_id: resp.data}
            return True
        else:
            _LOGGER.warning("Failed with response: %s", resp.status)
            await self.notify_ha(
                f"Warning: Data transfer failed with response {resp.status}"
            )
            return False

    async def get_configuration(self, key: str = "") -> str | dict | None:
        """Get Configuration of charger for supported keys.

        When key is empty, returns a dict of all configuration key-value pairs.
        When key is specified, returns the value as a string.
        """
        if key == "":
            req = call.GetConfiguration()
        else:
            req = call.GetConfiguration(key=[key])
        resp = await self.call(req)
        if resp.configuration_key:
            if key == "":
                result = {}
                for entry in resp.configuration_key:
                    entry_key = entry.get("key", "")
                    entry_value = entry.get(om.value, "")
                    result[entry_key] = entry_value
                _LOGGER.debug("Get Configuration returned %d keys", len(result))
                return result
            value = resp.configuration_key[0][om.value]
            _LOGGER.debug("Get Configuration for %s: %s", key, value)
            self._metrics[0][cdet.config_response].value = datetime.now(tz=UTC)
            self._metrics[0][cdet.config_response].extra_attr = {key: value}
            return value
        if resp.unknown_key:
            _LOGGER.warning("Get Configuration returned unknown key for: %s", key)
            await self.notify_ha(f"Warning: charger reports {key} is unknown")
            return "Unknown"

    async def configure(self, key: str, value: str):
        """Configure charger by setting the key to target value.

        First the configuration key is read using GetConfiguration. The key's
        value is compared with the target value. If the key is already set to
        the correct value nothing is done.

        If the key has a different value a ChangeConfiguration request is issued.

        """
        req = call.GetConfiguration(key=[key])

        resp = await self.call(req)

        if resp.unknown_key is not None:
            if key in resp.unknown_key:
                _LOGGER.warning("%s is unknown (not supported)", key)
                return "Unknown"

        for key_value in resp.configuration_key:
            # If the key already has the targeted value we don't need to set
            # it.
            if key_value[om.key] == key and key_value[om.value] == value:
                return

            if key_value.get(om.readonly.name, False):
                _LOGGER.warning("%s is a read only setting", key)
                await self.notify_ha(f"Warning: {key} is read-only")

        req = call.ChangeConfiguration(key=key, value=value)

        resp = await self.call(req)

        if resp.status in [
            ConfigurationStatus.rejected,
            ConfigurationStatus.not_supported,
        ]:
            _LOGGER.warning("%s while setting %s to %s", resp.status, key, value)
            await self.notify_ha(
                f"Warning: charger reported {resp.status} while setting {key}={value}"
            )
            return resp.status

        if resp.status == ConfigurationStatus.reboot_required:
            self._requires_reboot = True
            await self.notify_ha(f"A reboot is required to apply {key}={value}")
            return SetVariableResult.reboot_required

        return SetVariableResult.accepted

    async def async_update_device_info_v16(self, boot_info: dict):
        """Update device info asynchronuously."""

        _LOGGER.debug("Updating device info %s: %s", self.settings.cpid, boot_info)
        await self.async_update_device_info(
            boot_info.get(om.charge_point_serial_number.name, None),
            boot_info.get(om.charge_point_vendor.name, None),
            boot_info.get(om.charge_point_model.name, None),
            boot_info.get(om.firmware_version.name, None),
        )

    @on(Action.meter_values)
    def on_meter_values(self, connector_id: int, meter_value: dict, **kwargs):
        """Request handler for MeterValues Calls (multi-connector aware)."""

        self._ensure_tx_store_loaded()
        transaction_id: int = int(kwargs.get(om.transaction_id.name, 0) or 0)
        tx_has_id: bool = transaction_id not in (None, 0)
        if tx_has_id:
            # Seeing an id, including on closing values, is enough to keep a
            # later allocation clear of it; it does not make the id live.
            self._note_transaction_id(transaction_id)
        tx_end_context = any(
            sampled_value.get(om.context) == ReadingContext.transaction_end.value
            for bucket in meter_value
            for sampled_value in bucket.get(om.sampled_value.name, [])
        )

        # Restore missing per-connector meter_start / active_transaction_id from HA if possible.
        ms_key = (connector_id, csess.meter_start)
        tx_key = (connector_id, csess.transaction_id)
        session_key = (connector_id, csess.session_time)

        if self._metrics[ms_key].value is None:
            value = self.get_ha_metric(csess.meter_start, connector_id)
            if value is None:
                m = self._metrics.get((connector_id, DEFAULT_MEASURAND))
                value = m.value if m is not None else None
            else:
                try:
                    value = float(value)
                    _LOGGER.debug(
                        "%s[%s] was None, restored value=%s from HA.",
                        csess.meter_start,
                        connector_id,
                        value,
                    )
                except (ValueError, TypeError):
                    value = None
            self._metrics[ms_key].value = value

        if self._metrics[tx_key].value is None:
            value = self.get_ha_metric(csess.transaction_id, connector_id)
            if value is None:
                # A first sighting normally restores a transaction after a
                # restart. Closing values are different: their id names a
                # transaction that has already ended and must not revive it.
                value = (
                    transaction_id if transaction_id and not tx_end_context else None
                )
            else:
                try:
                    value = int(value)
                    _LOGGER.debug(
                        "%s[%s] was None, restored value=%s from HA.",
                        csess.transaction_id,
                        connector_id,
                        value,
                    )
                except (ValueError, TypeError):
                    value = None
            self._metrics[tx_key].value = value
            # Track active tx per connector, and keep new ids clear of it
            self._active_tx[connector_id] = int(value or 0)
            self._note_transaction_id(value)

        if connector_id not in self._active_tx:
            try:
                self._active_tx[connector_id] = int(self._metrics[tx_key].value or 0)
            except Exception:
                self._active_tx[connector_id] = 0

        recorded_tx = int(self._metrics[tx_key].value or 0)
        active_tx = int(self._active_tx.get(connector_id, 0) or 0)

        # A transaction's closing values arrive after its StopTransaction, so
        # adopting their id below would revive the session that just ended. A
        # charger says so with a Transaction.End context, but OCPP leaves that
        # field optional, so fall back to the id of the transaction we last saw
        # stop on this connector. The context speaks only for the id it comes
        # with: closing values for some other transaction, arriving while one
        # is known on this connector, end nothing here.
        current_tx = active_tx or recorded_tx
        tx_ended: bool = (
            bool(transaction_id)
            and current_tx
            in (
                0,
                transaction_id,
            )
            and (
                transaction_id == int(self._ended_tx.get(connector_id, 0) or 0)
                or tx_end_context
            )
        )

        # Self-heal after restart: adopt incoming txId if we have none recorded yet
        if transaction_id and not tx_ended and (recorded_tx == 0 and active_tx == 0):
            self._metrics[tx_key].value = transaction_id
            self._active_tx[connector_id] = transaction_id
            active_tx = transaction_id
            recorded_tx = transaction_id
            self._note_transaction_id(transaction_id)
            # The charger chose this id, so it says nothing about when the
            # session began; use the persisted start if this is the session
            # that was running before the restart, else the first sighting.
            self._ensure_session_start(connector_id, transaction_id)
            self._schedule_tx_store_save()
            _LOGGER.debug(
                "Restored transactionId=%s on conn %s from MeterValues.",
                transaction_id,
                connector_id,
            )

        # Keep legacy field synced for single-connector chargers,
        # even if self-heal did not run (e.g., values were already restored).
        try:
            n_con = int(getattr(self, "num_connectors", 1) or 1)
        except Exception:
            n_con = 1
        if n_con == 1:
            try:
                legacy = int(getattr(self, "active_transaction_id", 0) or 0)
            except Exception:
                legacy = 0
            if legacy != int(active_tx or 0):
                self.active_transaction_id = int(active_tx or 0)

        transaction_matches: bool = False
        # Match is also false if no transaction is in progress, i.e. active_tx==transaction_id==0
        if transaction_id == active_tx and transaction_id != 0:
            transaction_matches = True
        elif transaction_id != 0 and tx_ended:
            # The closing values arrive once the transaction has been cleared, but
            # they belong to it and carry its final energy figures. Treating them
            # as outside a transaction would file session energy as lifetime
            # energy on chargers that report the two in the same measurand.
            transaction_matches = True
        elif transaction_id != 0 and active_tx != 0 and transaction_id != active_tx:
            _LOGGER.warning(
                "Unknown transaction detected on conn %s with id=%i (expected %s)",
                connector_id,
                transaction_id,
                active_tx,
            )

        meter_values: list[list[MeasurandValue]] = []
        for bucket in meter_value:
            measurands: list[MeasurandValue] = []
            for sampled_value in bucket.get(om.sampled_value.name, []):
                measurand = sampled_value.get(om.measurand, None)
                value = sampled_value.get(om.value, None)
                # Where an empty string is supplied convert to 0
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    value = 0.0
                unit = sampled_value.get(om.unit, None)
                phase = sampled_value.get(om.phase, None)
                location = sampled_value.get(om.location, None)
                context = sampled_value.get(om.context, None)
                measurands.append(
                    MeasurandValue(measurand, value, phase, unit, context, location)
                )
            meter_values.append(measurands)

        self.process_measurands(meter_values, transaction_matches, connector_id)

        # The closing values are the last thing a charger sends for a session and
        # they still carry the final current and power, so they would otherwise
        # leave those sensors reading as though charging never stopped.
        if tx_ended:
            self._zero_flow_measurands(connector_id)

        # A sample for the transaction a held connector still has settles it as
        # running; the closing values of that transaction settle it as over.
        if connector_id in self._tx_indeterminate and tx_has_id:
            if tx_ended:
                # The connector's own transaction is over, which is evidence
                # of how it ended; an unattributed stop is not claimed here.
                self._resolve_indeterminate(connector_id, running=False, claim=False)
            elif transaction_matches:
                self._resolve_indeterminate(connector_id, running=True)

        # Session time comes from the recorded start, never from the id. The
        # closing values leave the final figure alone. A held connector only
        # gets here once the sample above has settled it as running, so its
        # timer stands still until the charger says something about it.
        if tx_has_id and transaction_matches and not tx_ended:
            started_at = self._ensure_session_start(connector_id, transaction_id)
            self._metrics[session_key].value = max(
                0, round((time.time() - started_at) / 60)
            )
            self._metrics[session_key].unit = UnitOfTime.MINUTES
        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.MeterValues()

    @on(Action.boot_notification)
    def on_boot_notification(self, **kwargs):
        """Handle a boot notification."""
        resp = call_result.BootNotification(
            current_time=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            interval=3600,
            status=RegistrationStatus.accepted.value,
        )
        self.received_boot_notification = True
        _LOGGER.debug("Received boot notification for %s: %s", self.id, kwargs)

        self._ensure_tx_store_loaded()
        self.hass.async_create_task(self.async_update_device_info_v16(kwargs))
        self._register_boot_notification()
        return resp

    @on(Action.status_notification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Handle a status notification."""
        _LOGGER.debug(
            "Status notification from %s: connector=%s status=%s error_code=%s %s",
            self.id,
            connector_id,
            status,
            error_code,
            kwargs,
        )

        if connector_id == 0 or connector_id is None:
            self._metrics[(0, cstat.status)].value = status
            self._metrics[(0, cstat.error_code)].value = error_code
        else:
            self._metrics[(connector_id, cstat.status_connector)].value = status
            self._metrics[(connector_id, cstat.error_code_connector)].value = error_code

            if status in (
                ChargePointStatus.suspended_ev.value,
                ChargePointStatus.suspended_evse.value,
            ):
                self._zero_flow_measurands(connector_id)

            # A connector held as unresolved after an unknown StopTransaction
            # is settled only by a status that proves something: Faulted or
            # Preparing leave it held rather than end a session that may be
            # running.
            if status in _TX_RUNNING_STATUSES:
                self._resolve_indeterminate(connector_id, running=True)
            elif status in _TX_ENDED_STATUSES:
                self._resolve_indeterminate(connector_id, running=False)

        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.StatusNotification()

    @on(Action.firmware_status_notification)
    def on_firmware_status(self, status, **kwargs):
        """Handle firmware status notification."""
        self._metrics[0][cstat.firmware_status].value = status
        self.hass.async_create_task(self.update(self.settings.cpid))
        self.hass.async_create_task(self.notify_ha(f"Firmware upload status: {status}"))
        return call_result.FirmwareStatusNotification()

    @on(Action.diagnostics_status_notification)
    def on_diagnostics_status(self, status, **kwargs):
        """Handle diagnostics status notification."""
        _LOGGER.info("Diagnostics upload status: %s", status)
        self.hass.async_create_task(
            self.notify_ha(f"Diagnostics upload status: {status}")
        )
        return call_result.DiagnosticsStatusNotification()

    @on(Action.security_event_notification)
    def on_security_event(self, type, timestamp, **kwargs):
        """Handle security event notification."""
        _LOGGER.info(
            "Security event notification received: %s at %s [techinfo: %s]",
            type,
            timestamp,
            kwargs.get(om.tech_info.name, "none"),
        )
        self.hass.async_create_task(
            self.notify_ha(f"Security event notification received: {type}")
        )
        return call_result.SecurityEventNotification()

    @on(Action.authorize)
    def on_authorize(self, id_tag, **kwargs):
        """Handle an Authorization request."""
        self._metrics[0][cstat.id_tag].value = id_tag
        auth_status = self.get_authorization_status(id_tag)
        return call_result.Authorize(id_tag_info={om.status: auth_status})

    @on(Action.start_transaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, **kwargs):
        """Handle a Start Transaction request."""

        self._ensure_tx_store_loaded()
        auth_status = self.get_authorization_status(id_tag)
        if auth_status == AuthorizationStatus.accepted.value:
            tx_id = self._allocate_transaction_id()
            self._ended_tx.pop(connector_id, None)
            if connector_id in self._tx_indeterminate:
                # Whatever it held has been superseded; a stop that named it
                # can no longer be attributed to anyone.
                self._tx_indeterminate.discard(connector_id)
                self._invalidate_pending_stops(
                    connector_id, "started a new transaction"
                )
            self._active_tx[connector_id] = tx_id
            self.active_transaction_id = tx_id
            self._set_session_start(connector_id, time.time(), estimated=False)
            self._metrics[(connector_id, cstat.id_tag)].value = id_tag
            self._metrics[(connector_id, cstat.stop_reason)].value = ""
            self._metrics[(connector_id, csess.transaction_id)].value = tx_id
            try:
                meter_start_kwh = float(meter_start) / 1000.0
            except Exception:
                meter_start_kwh = 0.0
            self._metrics[(connector_id, csess.meter_start)].value = meter_start_kwh
            self._metrics[(connector_id, csess.meter_start)].unit = HA_ENERGY_UNIT

            self._metrics[(connector_id, csess.session_time)].value = 0
            self._metrics[(connector_id, csess.session_time)].unit = UnitOfTime.MINUTES
            self._metrics[(connector_id, csess.session_energy)].value = 0.0
            self._metrics[(connector_id, csess.session_energy)].unit = HA_ENERGY_UNIT

            self._schedule_tx_store_save()
            result = call_result.StartTransaction(
                id_tag_info={om.status: AuthorizationStatus.accepted.value},
                transaction_id=tx_id,
            )
        else:
            result = call_result.StartTransaction(
                id_tag_info={om.status: auth_status},
                transaction_id=0,
            )

        self.hass.async_create_task(self.update(self.settings.cpid))
        return result

    @on(Action.stop_transaction)
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        """Stop the current transaction (multi-connector)."""

        self._ensure_tx_store_loaded()
        conn = self._resolve_stop_connector(transaction_id)
        if conn is None:
            # Guessing here used to reset connector 1, ending a session that
            # may still be running and filing another connector's energy
            # against it. Hold every connector that could own this stop
            # instead: its timer stops advancing and the next status report
            # from the charger settles it.
            live = self._live_connectors()
            if live:
                self._tx_indeterminate.update(live)
                # Keep the stop's payload for whichever of them ended.
                self._pending_stops.append(
                    {
                        "transaction_id": transaction_id,
                        "meter_stop": meter_stop,
                        "reason": kwargs.get(om.reason.name, None),
                        "candidates": set(live),
                    }
                )
                _LOGGER.warning(
                    "%s: StopTransaction for unknown transaction id=%s; cannot "
                    "tell which of connectors %s it ends, holding their session "
                    "state until the charger reports their status",
                    self.id,
                    transaction_id,
                    live,
                )
            else:
                _LOGGER.warning(
                    "%s: StopTransaction for unknown transaction id=%s with no "
                    "transaction recorded on any connector; nothing to end",
                    self.id,
                    transaction_id,
                )
            self.hass.async_create_task(self.update(self.settings.cpid))
            return call_result.StopTransaction(
                id_tag_info={om.status: AuthorizationStatus.accepted.value}
            )

        # Reset active transaction (global + per-connector). A stop that names
        # this connector as a possible owner could have been a malformed
        # duplicate of this one, so it is discarded rather than left for
        # another connector to claim.
        self._invalidate_pending_stops(conn, "received its own StopTransaction")
        self._clear_transaction_state(
            conn, transaction_id, kwargs.get(om.reason.name, None)
        )
        self.active_transaction_id = 0
        self._apply_stop_energy(conn, meter_stop)

        self._zero_flow_measurands(conn)
        self._schedule_tx_store_save()

        self.hass.async_create_task(self.update(self.settings.cpid))
        return call_result.StopTransaction(
            id_tag_info={om.status: AuthorizationStatus.accepted.value}
        )

    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        """Handle a Data transfer request."""
        _LOGGER.debug("Data transfer received from %s: %s", self.id, kwargs)
        self._metrics[0][cdet.data_transfer].value = datetime.now(tz=UTC)
        self._metrics[0][cdet.data_transfer].extra_attr = {vendor_id: kwargs}
        return call_result.DataTransfer(status=DataTransferStatus.accepted.value)

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle a Heartbeat."""
        now = datetime.now(tz=UTC)
        self._metrics[0][cstat.heartbeat].value = now
        self._async_refresh_metric_entities([cstat.heartbeat])
        return call_result.Heartbeat(current_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def _zero_flow_measurands(self, connector_id: int) -> None:
        """Clear the readings that only have meaning while current is flowing."""
        for meas in [
            Measurand.current_import.value,
            Measurand.power_active_import.value,
            Measurand.power_reactive_import.value,
            Measurand.current_export.value,
            Measurand.power_active_export.value,
            Measurand.power_reactive_export.value,
        ]:
            key = (connector_id, meas)
            if key in self._metrics:
                self._metrics[key].value = 0
