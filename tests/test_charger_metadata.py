"""Charger metadata sensors: Version.OCPP, Configuration.Keys, Boot.Notification.

The negotiated protocol, the charger's full configuration listing (and the
keys it reported unknown) and the optional BootNotification fields were
only visible in debug/warning logs. These diagnostic sensors record them so
they can be used as evidence rather than scraped from logs.

Most tests drive the server-side ChargePoint directly with a scripted
``call``; one end-to-end test connects a simulated 1.6 charger over a real
websocket and checks the entities in Home Assistant.
"""

import asyncio
import contextlib
import copy
from types import SimpleNamespace

import pytest
import websockets
from websockets.datastructures import Headers
from websockets.protocol import State
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.helpers import entity_registry as er
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cpclass, call, call_result
from ocpp.v16.enums import Action, ConfigurationStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.api import CentralSystem
from custom_components.ocpp.chargepoint import (
    CONFIG_SNAPSHOT_MAX_KEYS,
    CONFIG_SNAPSHOT_MAX_VALUE_LEN,
)
from custom_components.ocpp.const import (
    CONF_NUM_CONNECTORS,
    DEFAULT_MONITORED_VARIABLES,
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
    sensor_unique_id,
)
from custom_components.ocpp.enums import HAChargerDetails as cdet
from custom_components.ocpp.ocppv16 import ChargePoint as ChargePoint16

from .charge_point_test import create_configuration, remove_configuration
from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_PORT,
    MOCK_CONFIG_CP_APPEND,
    MOCK_CONFIG_DATA,
)
from .test_charge_point_core import _mk_entry_data
from .test_v201_heartbeat_metric import _mk_cp as _mk_cp201

# What a Sigenergy EVDC (OCPP 1.6J) returned to GetConfiguration without keys.
SIGEN_CONFIG = {
    "AuthorizeRemoteTxRequests": "0",
    "HeartbeatInterval": "3600",
    "MeterValueSampleInterval": "60",
    "TransactionMessageAttempts": "3",
    "TransactionMessageRetryInterval": "60",
    "SupportedFeatureProfiles": "Core",
}
SIGEN_READONLY = {"SupportedFeatureProfiles"}


def _mk_cp16(hass, *, ssl: bool = False, connection=None) -> ChargePoint16:
    data = {**_mk_entry_data(), "ssl": ssl}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    charger = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    if connection is None:
        connection = SimpleNamespace(
            state=State.CLOSED,
            close=lambda: asyncio.sleep(0),
            subprotocol="ocpp1.6",
            ocpp_offered_subprotocols=["ocpp1.6"],
        )
    return ChargePoint16(
        "CP_A", connection, hass, entry, CentralSystemSettings(**data), charger
    )


def _scripted_call(config: dict, readonly: set, *, snapshot=None):
    """Return a fake ``ChargePoint.call`` answering like a charger with ``config``.

    ``snapshot`` overrides the reply to GetConfiguration without keys: an
    exception instance is raised, anything else is returned.
    """
    calls = []

    def kv(key):
        return {"key": key, "readonly": key in readonly, "value": config[key]}

    async def fake_call(req, *args, **kwargs):
        calls.append(req)
        if isinstance(req, call.GetConfiguration):
            if req.key is None:
                if isinstance(snapshot, BaseException):
                    raise snapshot
                if snapshot is not None:
                    return snapshot
                return call_result.GetConfiguration(
                    configuration_key=[kv(k) for k in config]
                )
            known = [k for k in req.key if k in config]
            unknown = [k for k in req.key if k not in config]
            return call_result.GetConfiguration(
                configuration_key=[kv(k) for k in known] or None,
                unknown_key=unknown or None,
            )
        if isinstance(req, call.ChangeConfiguration):
            status = (
                ConfigurationStatus.accepted
                if req.key in config
                else ConfigurationStatus.not_supported
            )
            return call_result.ChangeConfiguration(status=status)
        if isinstance(req, call.ChangeAvailability):
            return call_result.ChangeAvailability(status="Accepted")
        raise NotImplementedError(type(req).__name__)

    fake_call.calls = calls
    return fake_call


def _attrs(cp, metric):
    return cp._metrics[(0, metric)].extra_attr


# --- Version.OCPP ----------------------------------------------------------


def test_select_subprotocol_stashes_the_offer_without_changing_selection():
    """The charger's offer is remembered on the connection; selection is unchanged."""
    cs = SimpleNamespace(subprotocols=["ocpp2.0.1", "ocpp1.6"])
    conn = SimpleNamespace()
    selected = CentralSystem.select_subprotocol(cs, conn, ["ocpp1.6", "ocpp2.0.1"])
    assert selected == "ocpp2.0.1"
    assert conn.ocpp_offered_subprotocols == ["ocpp1.6", "ocpp2.0.1"]

    # No offer: still defaults to 1.6 (None), and records an empty offer.
    conn = SimpleNamespace()
    assert CentralSystem.select_subprotocol(cs, conn, []) is None
    assert conn.ocpp_offered_subprotocols == []

    # A connection object that refuses attributes must not break negotiation.
    assert CentralSystem.select_subprotocol(cs, object(), ["ocpp1.6"]) == "ocpp1.6"


async def test_version_sensor_reports_version_subprotocol_and_ws(hass):
    """Plain websocket: version, negotiated and offered subprotocols, ws."""
    cp = _mk_cp16(hass)
    assert cp._metrics[(0, cdet.ocpp_version)].value == "1.6"
    assert _attrs(cp, cdet.ocpp_version) == {
        "subprotocol": "ocpp1.6",
        "offered_subprotocols": ["ocpp1.6"],
        "transport": "ws",
    }


async def test_version_sensor_reports_wss_and_falls_back_to_the_header(hass):
    """TLS server reports wss; without a stashed offer the header is parsed."""
    conn = SimpleNamespace(
        state=State.CLOSED,
        close=lambda: asyncio.sleep(0),
        subprotocol="ocpp1.6",
        request=SimpleNamespace(
            headers=Headers([("Sec-WebSocket-Protocol", "ocpp2.0.1, ocpp1.6")])
        ),
    )
    cp = _mk_cp16(hass, ssl=True, connection=conn)
    attrs = _attrs(cp, cdet.ocpp_version)
    assert attrs["transport"] == "wss"
    assert attrs["offered_subprotocols"] == ["ocpp2.0.1", "ocpp1.6"]


async def test_version_sensor_without_a_subprotocol(hass):
    """A charger that offered nothing defaulted to 1.6 with no subprotocol."""
    conn = SimpleNamespace(
        state=State.CLOSED, close=lambda: asyncio.sleep(0), subprotocol=None
    )
    cp = _mk_cp16(hass, connection=conn)
    assert cp._metrics[(0, cdet.ocpp_version)].value == "1.6"
    assert _attrs(cp, cdet.ocpp_version)["subprotocol"] is None
    assert _attrs(cp, cdet.ocpp_version)["offered_subprotocols"] == []


async def test_version_sensor_on_v201(hass):
    """2.0.1 charge points report their negotiated version too."""
    cp = _mk_cp201(hass)
    assert cp._metrics[(0, cdet.ocpp_version)].value == "2.0.1"
    assert _attrs(cp, cdet.ocpp_version)["subprotocol"] == "ocpp2.0.1"


async def test_reconnect_metadata_reflects_the_new_connection(hass):
    """A reconnect re-records the metadata from the new handshake."""
    cp = _mk_cp16(hass)
    cp._record_connection_metadata(
        SimpleNamespace(subprotocol="ocpp1.6", ocpp_offered_subprotocols=["ocpp1.6j"])
    )
    assert _attrs(cp, cdet.ocpp_version)["offered_subprotocols"] == ["ocpp1.6j"]


# --- Configuration.Keys ----------------------------------------------------


async def test_post_connect_records_the_sigenergy_configuration(hass):
    """post_connect snapshots every key, readonly keys and accumulated unknowns."""
    cp = _mk_cp16(hass)
    fake = _scripted_call(SIGEN_CONFIG, SIGEN_READONLY)
    cp.call = fake

    await cp.post_connect()

    assert cp.post_connect_success is True
    # Exactly one GetConfiguration without a key list, issued last.
    full = [r for r in fake.calls if isinstance(r, call.GetConfiguration) and not r.key]
    assert len(full) == 1
    assert fake.calls[-1] is full[0]

    metric = cp._metrics[(0, cdet.config_keys)]
    assert metric.value == 6
    attrs = metric.extra_attr
    for key, value in SIGEN_CONFIG.items():
        assert attrs[key] == value
    assert attrs["readonly_keys"] == ["SupportedFeatureProfiles"]
    # Collected from the earlier keyed requests (connector count, measurands,
    # clock-aligned interval), not from the snapshot itself.
    assert attrs["unknown_keys"] == [
        "ClockAlignedDataInterval",
        "MeterValuesSampledData",
        "NumberOfConnectors",
    ]
    assert attrs["redacted_keys"] == []
    assert attrs["keys_truncated"] is False
    assert attrs["truncated_values"] == []
    # Nothing was requested (manual mode, empty selection): unknown, omitted.
    assert "measurands_configurable" not in attrs


async def test_measurands_configurable_is_exposed_when_known(hass):
    """An accepted measurand selection is recorded in the snapshot."""
    cp = _mk_cp16(hass)
    cp.settings.monitored_variables = "Energy.Active.Import.Register"
    config = {
        **SIGEN_CONFIG,
        "MeterValuesSampledData": "Energy.Active.Import.Register",
    }
    cp.call = _scripted_call(config, SIGEN_READONLY)
    await cp.post_connect()
    assert _attrs(cp, cdet.config_keys)["measurands_configurable"] is True

    cp2 = _mk_cp16(hass)
    cp2.settings.monitored_variables = "Energy.Active.Import.Register"
    cp2.call = _scripted_call(SIGEN_CONFIG, SIGEN_READONLY)
    await cp2.post_connect()
    assert _attrs(cp2, cdet.config_keys)["measurands_configurable"] is False


async def test_measurands_not_configurable_when_the_change_fails(hass):
    """A ChangeConfiguration that raises marks measurands as not configurable."""
    cp = _mk_cp16(hass)
    cp.settings.monitored_variables = "Energy.Active.Import.Register"
    config = {
        **SIGEN_CONFIG,
        "MeterValuesSampledData": "Energy.Active.Import.Register",
    }
    scripted = _scripted_call(config, SIGEN_READONLY)

    async def failing_change(req, *args, **kwargs):
        if isinstance(req, call.ChangeConfiguration):
            raise TimeoutError("no reply")
        return await scripted(req, *args, **kwargs)

    cp.call = failing_change
    await cp.post_connect()
    assert _attrs(cp, cdet.config_keys)["measurands_configurable"] is False


async def test_later_unknown_keys_update_an_existing_snapshot(hass):
    """An unknown key found after the snapshot is added to it."""
    cp = _mk_cp16(hass)
    cp.call = _scripted_call(SIGEN_CONFIG, SIGEN_READONLY)
    await cp.post_connect()
    assert await cp.get_configuration("WebSocketPingInterval") == "Unknown"
    assert "WebSocketPingInterval" in _attrs(cp, cdet.config_keys)["unknown_keys"]


async def test_unknown_keys_accept_a_string_and_ignore_repeats(hass):
    """A lone key string is accepted and a repeated key changes nothing."""
    cp = _mk_cp16(hass)
    cp.call = _scripted_call(SIGEN_CONFIG, SIGEN_READONLY)
    await cp.post_connect()
    cp._record_unknown_config_keys("WebSocketPingInterval")
    attrs = _attrs(cp, cdet.config_keys)
    assert "WebSocketPingInterval" in attrs["unknown_keys"]

    cp._record_unknown_config_keys(["WebSocketPingInterval"])
    assert _attrs(cp, cdet.config_keys) is attrs


async def test_secrets_are_redacted_by_key_name(hass):
    """Credential-like keys never reach the attributes; benign keys do."""
    cp = _mk_cp16(hass)
    cp._record_configuration_snapshot(
        [
            {"key": "AuthorizationKey", "readonly": False, "value": "0011223344"},
            {"key": "WifiPassword", "readonly": False, "value": "hunter2"},
            {"key": "BackendAuthToken", "readonly": True, "value": "tok"},
            {"key": "CentralSystemCertificate", "readonly": True, "value": "PEM"},
            {"key": "VendorSecret", "readonly": False, "value": "s"},
            {"key": "LocalAuthorizeOffline", "readonly": False, "value": "true"},
            {"key": "AuthorizeRemoteTxRequests", "readonly": False, "value": "0"},
        ]
    )
    attrs = _attrs(cp, cdet.config_keys)
    for key in (
        "AuthorizationKey",
        "WifiPassword",
        "BackendAuthToken",
        "CentralSystemCertificate",
        "VendorSecret",
    ):
        assert attrs[key] == "redacted"
    assert attrs["LocalAuthorizeOffline"] == "true"
    assert attrs["AuthorizeRemoteTxRequests"] == "0"
    assert attrs["redacted_keys"] == sorted(
        [
            "AuthorizationKey",
            "BackendAuthToken",
            "CentralSystemCertificate",
            "VendorSecret",
            "WifiPassword",
        ]
    )
    assert attrs["readonly_keys"] == ["BackendAuthToken", "CentralSystemCertificate"]
    assert "0011223344" not in repr(attrs)


async def test_snapshot_is_bounded(hass):
    """At most 200 keys, values cut to 255 chars, and truncation is reported."""
    cp = _mk_cp16(hass)
    entries = [
        {"key": f"Key{i:03d}", "readonly": False, "value": str(i)}
        for i in range(CONFIG_SNAPSHOT_MAX_KEYS + 25)
    ]
    entries[0]["value"] = "x" * 1000
    entries[1].pop("value")  # value is optional in 1.6
    cp._record_configuration_snapshot(entries)

    metric = cp._metrics[(0, cdet.config_keys)]
    # The state still counts everything the charger returned.
    assert metric.value == CONFIG_SNAPSHOT_MAX_KEYS + 25
    attrs = metric.extra_attr
    assert attrs["keys_truncated"] is True
    assert sum(1 for k in attrs if k.startswith("Key")) == CONFIG_SNAPSHOT_MAX_KEYS
    assert f"Key{CONFIG_SNAPSHOT_MAX_KEYS:03d}" not in attrs
    assert attrs["Key000"] == "x" * CONFIG_SNAPSHOT_MAX_VALUE_LEN
    assert attrs["truncated_values"] == ["Key000"]
    assert attrs["Key001"] == ""


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), RuntimeError("boom"), call_result.GetConfiguration()],
    ids=["timeout", "error", "empty"],
)
async def test_failing_snapshot_does_not_break_post_connect(hass, failure):
    """A failed or empty full GetConfiguration leaves post_connect intact."""
    cp = _mk_cp16(hass)
    cp.call = _scripted_call(SIGEN_CONFIG, SIGEN_READONLY, snapshot=failure)
    await cp.post_connect()
    assert cp.post_connect_success is True
    assert cp._metrics[(0, cdet.features)].value is not None
    metric = cp._metrics[(0, cdet.config_keys)]
    if isinstance(failure, BaseException):
        assert metric.value is None
    else:
        assert metric.value == 0


async def test_snapshot_cancellation_propagates(hass):
    """Cancellation during the snapshot still cancels post_connect."""
    cp = _mk_cp16(hass)
    cp.call = _scripted_call(
        SIGEN_CONFIG, SIGEN_READONLY, snapshot=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await cp.post_connect()


async def test_unanswered_snapshot_is_bounded(hass, monkeypatch):
    """A snapshot reply that never comes cannot hold post_connect open."""
    monkeypatch.setattr(
        "custom_components.ocpp.chargepoint.CONFIG_SNAPSHOT_TIMEOUT", 0.05
    )
    cp = _mk_cp16(hass)
    scripted = _scripted_call(SIGEN_CONFIG, SIGEN_READONLY)

    async def silent_snapshot(req, *args, **kwargs):
        if isinstance(req, call.GetConfiguration) and req.key is None:
            await asyncio.Event().wait()
        return await scripted(req, *args, **kwargs)

    cp.call = silent_snapshot
    await asyncio.wait_for(cp.post_connect(), timeout=2)
    assert cp.post_connect_success is True
    assert cp._metrics[(0, cdet.config_keys)].value is None


# --- Boot.Notification -----------------------------------------------------


async def test_v16_boot_notification_keeps_every_field(hass):
    """All 1.6 BootNotification fields, optional ones included, are kept."""
    cp = _mk_cp16(hass)
    cp.post_connect_success = True  # don't start post_connect from the boot
    fields = {
        "charge_point_vendor": "Sigenergy",
        "charge_point_model": "EVDC",
        "charge_point_serial_number": "CPSN1",
        "charge_box_serial_number": "CBSN1",
        "firmware_version": "1.2.3",
        "iccid": "8961000000000000000",
        "imsi": "505010000000000",
        "meter_type": "DC-Meter",
        "meter_serial_number": "MSN1",
    }
    cp.on_boot_notification(**fields)
    await hass.async_block_till_done()

    metric = cp._metrics[(0, cdet.boot_notification)]
    assert metric.value is not None and metric.value.tzinfo is not None
    assert metric.extra_attr == fields


async def test_failing_boot_record_is_only_logged(hass, caplog):
    """A failure while recording the boot never escapes the boot handler."""
    cp = _mk_cp16(hass)
    cp.post_connect_success = True
    caplog.set_level("DEBUG", logger="custom_components.ocpp")

    def broken_refresh(metrics):
        raise RuntimeError("refresh failed")

    cp._async_refresh_metric_entities = broken_refresh
    cp._record_boot_notification({"charge_point_vendor": "Sigenergy"})
    assert "could not record boot notification: refresh failed" in caplog.text


async def test_v201_boot_notification_flattens_charging_station(hass):
    """2.x chargingStation fields (modem nested) and reason are kept."""
    cp = _mk_cp201(hass)
    cp.post_connect_success = True
    cp.on_boot_notification(
        charging_station={
            "model": "M1",
            "vendor_name": "V",
            "serial_number": "SN",
            "firmware_version": "2.0",
            "custom_data": None,
            "modem": {"iccid": "ICC", "imsi": "IMS"},
        },
        reason="PowerUp",
    )
    await hass.async_block_till_done()

    metric = cp._metrics[(0, cdet.boot_notification)]
    assert metric.value is not None
    assert metric.extra_attr == {
        "model": "M1",
        "vendor_name": "V",
        "serial_number": "SN",
        "firmware_version": "2.0",
        "modem_iccid": "ICC",
        "modem_imsi": "IMS",
        "reason": "PowerUp",
    }
    # 2.x has no 1.6 configuration listing; the sensor stays unset.
    assert cp._metrics[(0, cdet.config_keys)].value is None


# --- End to end ------------------------------------------------------------


class MetadataCharger(cpclass):
    """1.6 charger answering only what post_connect needs, like the Sigenergy."""

    def __init__(self, *args, **kwargs):
        """Start from the Sigenergy listing plus a measurand and a secret key."""
        super().__init__(*args, **kwargs)
        self.config = {
            **SIGEN_CONFIG,
            "MeterValuesSampledData": DEFAULT_MONITORED_VARIABLES,
            "AuthorizationKey": "deadbeef",
        }

    def _kv(self, key):
        return {
            "key": key,
            "readonly": key in SIGEN_READONLY,
            "value": self.config[key],
        }

    @on(Action.get_configuration)
    def on_get_configuration(self, key=None, **kwargs):
        """Answer keyed and full (no key list) GetConfiguration requests."""
        if not key:
            return call_result.GetConfiguration(
                configuration_key=[self._kv(k) for k in self.config]
            )
        known = [k for k in key if k in self.config]
        unknown = [k for k in key if k not in self.config]
        return call_result.GetConfiguration(
            configuration_key=[self._kv(k) for k in known] or None,
            unknown_key=unknown or None,
        )

    @on(Action.change_configuration)
    def on_change_configuration(self, key, value, **kwargs):
        """Accept changes to known keys only."""
        if key not in self.config:
            return call_result.ChangeConfiguration(
                status=ConfigurationStatus.not_supported
            )
        self.config[key] = value
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)


async def _wait_for(predicate, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


async def test_sensors_end_to_end(hass, socket_enabled):
    """A connecting 1.6 charger populates all three diagnostic sensors."""
    cp_id = "CP_meta"
    cpid = "test_cpid_meta"
    data = copy.deepcopy(MOCK_CONFIG_DATA)
    cp_data = copy.deepcopy(MOCK_CONFIG_CP_APPEND)
    cp_data[CONF_CPID] = cpid
    # Match what post_connect will detect so it does not reload the entry
    # (and drop the connection) mid-test.
    cp_data[CONF_NUM_CONNECTORS] = 1
    data[CONF_CPIDS].append({cp_id: cp_data})
    data[CONF_PORT] = 9433
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        entry_id="test_cms_meta",
        title="test_cms_meta",
        version=2,
        minor_version=0,
    )
    await create_configuration(hass, entry)

    registry = er.async_get(hass)
    for metric in (cdet.ocpp_version, cdet.config_keys, cdet.boot_notification):
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, sensor_unique_id(cpid, metric)
        ), metric

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{data[CONF_PORT]}/{cp_id}", subprotocols=["ocpp1.6"]
        ) as ws:
            charger = MetadataCharger(f"{cp_id}_client", ws)
            task = asyncio.create_task(charger.start())
            try:
                await charger.call(
                    call.BootNotification(
                        charge_point_model="EVDC",
                        charge_point_vendor="Sigenergy",
                        charge_point_serial_number="CPSN1",
                        meter_type="DC-Meter",
                        meter_serial_number="MSN1",
                        iccid="8961000000000000000",
                        imsi="505010000000000",
                    )
                )
                assert await _wait_for(
                    lambda: (
                        (s := hass.states.get(f"sensor.{cpid}_configuration_keys"))
                        is not None
                        and s.state == "8"
                    )
                )
                await hass.async_block_till_done()

                version = hass.states.get(f"sensor.{cpid}_version_ocpp")
                assert version.state == "1.6"
                assert version.attributes["subprotocol"] == "ocpp1.6"
                assert version.attributes["offered_subprotocols"] == ["ocpp1.6"]
                assert version.attributes["transport"] == "ws"

                keys = hass.states.get(f"sensor.{cpid}_configuration_keys")
                assert keys.attributes["HeartbeatInterval"] == "3600"
                assert keys.attributes["AuthorizationKey"] == "redacted"
                assert keys.attributes["readonly_keys"] == ["SupportedFeatureProfiles"]
                assert keys.attributes["unknown_keys"] == [
                    "ClockAlignedDataInterval",
                    "NumberOfConnectors",
                ]
                assert keys.attributes["measurands_configurable"] is True

                boot = hass.states.get(f"sensor.{cpid}_boot_notification")
                assert boot.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP
                assert boot.attributes["meter_type"] == "DC-Meter"
                assert boot.attributes["meter_serial_number"] == "MSN1"
                assert boot.attributes["charge_point_vendor"] == "Sigenergy"
                # SIM identifiers are shown but kept out of recorder history.
                assert boot.attributes["iccid"] == "8961000000000000000"
                assert boot.attributes["imsi"] == "505010000000000"
                assert {"iccid", "imsi", "modem_iccid", "modem_imsi"} <= (
                    boot.state_info["unrecorded_attributes"]
                )
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    finally:
        await remove_configuration(hass, entry)
