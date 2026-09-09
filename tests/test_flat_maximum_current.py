"""A Maximum Current entity controls one station ceiling, regardless of connectors."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.number import NumberExtraStoredData
from homeassistant.const import STATE_OK, UnitOfElectricCurrent
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from ocpp.messages import CallError
from ocpp.v16.enums import Measurand
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.ocpp.const import (
    CONF_CPIDS,
    CONF_MAX_CURRENT,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    DOMAIN,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import ConfigurationKey, Profiles
from custom_components.ocpp.ocppv16 import ChargePoint as ChargePoint16
from custom_components.ocpp.ocppv201 import ChargePoint as ChargePoint201

from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA
from .lifecycle_asserts import live_entity


@pytest.fixture
def flat_entry(hass):
    """Register a charger before platform setup so tests can seed its registry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CONFIG_DATA,
            CONF_PORT: 0,
            CONF_CPIDS: [
                {
                    "CP_flat": {
                        **MOCK_CONFIG_CP_APPEND,
                        CONF_MAX_CURRENT: 48,
                        CONF_NUM_CONNECTORS: 2,
                    }
                }
            ],
        },
        version=2,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_flat(hass, bypass_get_data, flat_entry):
    """Start the real integration and clean up any attached protocol object."""

    async def setup():
        assert await hass.config_entries.async_setup(flat_entry.entry_id)
        await hass.async_block_till_done()
        return hass.data[DOMAIN][flat_entry.entry_id]

    yield setup
    central = hass.data.get(DOMAIN, {}).get(flat_entry.entry_id)
    if central is not None:
        central.charge_points.clear()
        assert await hass.config_entries.async_unload(flat_entry.entry_id)


def _settings(entry):
    return entry.data[CONF_CPIDS][0]["CP_flat"]


def _configure(hass, entry, **changes):
    data = {**entry.data, CONF_CPIDS: [{"CP_flat": {**_settings(entry), **changes}}]}
    hass.config_entries.async_update_entry(entry, data=data)


def _register(hass, entry, uid, **kwargs):
    return er.async_get(hass).async_get_or_create(
        "number", DOMAIN, uid, config_entry=entry, **kwargs
    )


def _restore(hass, entity_id, value):
    data = NumberExtraStoredData(48, 0, 1, UnitOfElectricCurrent.AMPERE, value)
    mock_restore_cache_with_extra_data(
        hass, [(State(entity_id, str(value)), data.as_dict())]
    )


def _attach_protocol(hass, entry, central, protocol="1.6"):
    connection = SimpleNamespace(subprotocol=f"ocpp{protocol}")
    cls = ChargePoint16 if protocol == "1.6" else ChargePoint201
    cp = cls(
        "CP_flat",
        connection,
        hass,
        entry,
        central.settings,
        ChargerSystemSettings(**_settings(entry)),
    )
    cp.status = STATE_OK
    cp.num_connectors = 2
    cp._attr_supported_features = Profiles.SMART
    central.cpids["test_cpid"] = "CP_flat"
    central.charge_points["CP_flat"] = cp
    return cp


def _number(hass):
    return live_entity(hass, "number.test_cpid_maximum_current", "number")


@pytest.mark.parametrize("connectors", [1, 2])
async def test_one_master_on_station_device(hass, flat_entry, setup_flat, connectors):
    """Single and multi-connector chargers expose the same configured master."""
    _configure(hass, flat_entry, num_connectors=connectors)
    central = await setup_flat()
    registry = er.async_get(hass)
    numbers = [
        e
        for e in er.async_entries_for_config_entry(registry, flat_entry.entry_id)
        if e.domain == "number"
    ]
    assert len(numbers) == 1
    assert numbers[0].unique_id == "number.ocpp.test_cpid.maximum_current"
    entity = _number(hass)
    device = dr.async_get(hass).async_get(numbers[0].device_id)
    assert (DOMAIN, "test_cpid") in device.identifiers
    assert entity.connector_id is None
    assert entity._op_connector_id == 0
    assert entity.native_value == entity.native_max_value == 48
    assert entity.native_min_value == 0
    assert entity.native_step == 1
    assert entity._confirmed_value is None
    assert not entity.available
    cp = _attach_protocol(hass, flat_entry, central)
    assert entity.available
    cp._attr_supported_features = Profiles.NONE
    assert not entity.available
    cp._attr_supported_features = Profiles.SMART
    cp.status = "init"
    assert not entity.available


@pytest.mark.parametrize("connectors", [1, 2])
async def test_cleanup_only_legacy_numbers_for_this_charger(
    hass, flat_entry, setup_flat, caplog, connectors
):
    """Clean all stale indices, names and disabled entries without overmatching."""
    _configure(hass, flat_entry, cpid="test.cpid", num_connectors=connectors)
    caplog.set_level(logging.INFO, logger="custom_components.ocpp")
    registry = er.async_get(hass)
    removed = [
        _register(
            hass,
            flat_entry,
            f"number.ocpp.test.cpid.conn{n}.maximum_current",
            suggested_object_id=f"custom_slider_{n}",
            disabled_by=er.RegistryEntryDisabler.USER if n == 2 else None,
        )
        for n in (1, 2, 3, 40)
    ]
    foreign_entry = MockConfigEntry(domain=DOMAIN)
    foreign_entry.add_to_hass(hass)
    kept = [
        _register(hass, flat_entry, "number.ocpp.testXcpid.conn1.maximum_current"),
        _register(
            hass, flat_entry, "number.ocpp.test.cpid_other.conn1.maximum_current"
        ),
        _register(hass, flat_entry, "number.ocpp.test.cpid.conn1.another_number"),
        _register(
            hass, flat_entry, "number.ocpp.test.cpid.conn1.maximum_current.extra"
        ),
        _register(hass, foreign_entry, "number.ocpp.test.cpid.conn5.maximum_current"),
        registry.async_get_or_create(
            "number",
            "other",
            "number.ocpp.test.cpid.conn6.maximum_current",
            config_entry=flat_entry,
        ),
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "number.ocpp.test.cpid.conn7.maximum_current",
            config_entry=flat_entry,
        ),
    ]
    await setup_flat()
    for entry in removed:
        assert registry.async_get(entry.entity_id) is None
        assert (
            sum(
                f"Removing stale connector-level entity {entry.entity_id};" in r.message
                for r in caplog.records
            )
            == 1
        )
    for entry in kept:
        assert registry.async_get(entry.entity_id) == entry


async def test_existing_master_survives_connector_reconfiguration(
    hass, flat_entry, setup_flat
):
    """Keep a renamed flat registry entry, its state and settings through 1 -> 2."""
    _configure(hass, flat_entry, num_connectors=1)
    existing = _register(
        hass,
        flat_entry,
        "number.ocpp.test_cpid.maximum_current",
        suggested_object_id="custom_station_limit",
    )
    existing = er.async_get(hass).async_update_entity(
        existing.entity_id, name="Site ceiling"
    )
    _restore(hass, existing.entity_id, 17)
    await setup_flat()
    entity = live_entity(hass, existing.entity_id, "number")
    assert entity.native_value == entity._confirmed_value == 17
    assert await hass.config_entries.async_unload(flat_entry.entry_id)
    _configure(hass, flat_entry, num_connectors=2)
    await setup_flat()
    reloaded = live_entity(hass, existing.entity_id, "number")
    assert reloaded is not entity
    assert reloaded.native_value == reloaded._confirmed_value == 17
    registry_entry = er.async_get(hass).async_get(existing.entity_id)
    assert registry_entry.id == existing.id
    assert registry_entry.name == "Site ceiling"


@pytest.mark.parametrize("renamed_connector", [False, True])
async def test_new_master_ignores_old_restore_data(
    hass, flat_entry, setup_flat, renamed_connector
):
    """Historical flat state and a renamed connector must never seed confirmation."""
    eid = "number.test_cpid_maximum_current"
    if renamed_connector:
        old = _register(
            hass,
            flat_entry,
            "number.ocpp.test_cpid.conn1.maximum_current",
            suggested_object_id="test_cpid_maximum_current",
        )
        assert old.entity_id == eid
    _restore(hass, eid, 9)
    central = await setup_flat()
    entity = _number(hass)
    assert entity.native_value == 48
    assert entity._confirmed_value is None
    cp = _attach_protocol(hass, flat_entry, central)
    cp.call = AsyncMock(return_value=SimpleNamespace(status="Accepted"))
    cp.get_configuration = AsyncMock(return_value="Current")
    await entity.async_set_native_value(18)
    central.charge_points.clear()
    assert await hass.config_entries.async_reload(flat_entry.entry_id)
    await hass.async_block_till_done()
    assert _number(hass).native_value == _number(hass)._confirmed_value == 18


@pytest.fixture
async def slider16(hass, flat_entry, setup_flat):
    """Attach a real 1.6 protocol object behind the real number and API."""
    central = await setup_flat()
    cp = _attach_protocol(hass, flat_entry, central)
    cp._active_tx = {1: 111, 2: 222}

    async def configuration(key):
        return (
            "Current"
            if key == ConfigurationKey.charging_schedule_allowed_charging_rate_unit
            else "3"
        )

    cp.get_configuration = AsyncMock(side_effect=configuration)
    cp.call = AsyncMock(return_value=SimpleNamespace(status="Accepted"))
    return _number(hass), cp, central


async def test_slider16_sends_only_station_ceiling(slider16):
    """Acceptance sends one station profile, including when set to the maximum."""
    entity, cp, _ = slider16
    for limit in (16, 48):
        cp.call.reset_mock()
        await entity.async_set_native_value(limit)
        cp.call.assert_awaited_once()
        req = cp.call.call_args.args[0]
        assert cp.call.call_args.kwargs == {"suppress": False}
        assert req.connector_id == 0
        assert req.cs_charging_profiles == {
            "chargingProfileId": 1000,
            "stackLevel": 3,
            "chargingProfilePurpose": "ChargePointMaxProfile",
            "chargingProfileKind": "Relative",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": float(limit)}],
            },
        }
        assert entity.native_value == entity._confirmed_value == limit


@pytest.mark.parametrize(
    "failure", ["Rejected", "NotSupported", TimeoutError("no reply")]
)
@pytest.mark.parametrize("confirmed", [None, 24])
async def test_slider16_refusal_reverts_without_fallback(slider16, failure, confirmed):
    """Refusals preserve the last confirmed value and reason with active transactions."""
    entity, cp, _ = slider16
    entity._confirmed_value = confirmed
    if isinstance(failure, Exception):
        cp.call.side_effect = failure
    else:
        cp.call.return_value = SimpleNamespace(status=failure)
    with pytest.raises(HomeAssistantError) as exc:
        await entity.async_set_native_value(16)
    assert str(failure) in str(exc.value.translation_placeholders["message"])
    assert entity.native_value == entity._confirmed_value == confirmed
    cp.call.assert_awaited_once()
    assert (
        cp.call.call_args.args[0].cs_charging_profiles["chargingProfilePurpose"]
        == "ChargePointMaxProfile"
    )


async def test_slider16_callerror_through_library(slider16):
    """Route a real CALLERROR through the OCPP queue and preserve the charger's reason."""
    entity, cp, _ = slider16
    sent = []

    async def send(frame):
        message = json.loads(frame)
        sent.append(message)
        response = CallError(
            message[1], "NotSupported", "station profile unsupported", {}
        )
        await cp.route_message(response.to_json())

    cp._connection.send = send
    cp.call = AsyncMock(wraps=ChargePoint16.call.__get__(cp))
    with pytest.raises(HomeAssistantError) as exc:
        await asyncio.wait_for(entity.async_set_native_value(16), timeout=2)
    assert "NotSupported" in exc.value.translation_placeholders["message"]
    assert (
        "station profile unsupported" in exc.value.translation_placeholders["message"]
    )
    assert len(sent) == 1
    assert sent[0][2] == "SetChargingProfile"
    assert cp.call.call_args.kwargs == {"suppress": False}
    assert entity.native_value is None


async def test_slider16_watt_only_conversion(slider16):
    """The station route retains the action's measured three-phase watt conversion."""
    entity, cp, _ = slider16
    cp.get_configuration = AsyncMock(side_effect=["Power", "3"])
    metric = cp._metrics[(1, Measurand.voltage.value)]
    metric.value = 240
    metric.extra_attr = {"L1": 240, "L2": 240, "L3": 240}
    await entity.async_set_native_value(16)
    schedule = cp.call.call_args.args[0].cs_charging_profiles["chargingSchedule"]
    assert schedule["chargingRateUnit"] == "W"
    assert schedule["chargingSchedulePeriod"] == [{"startPeriod": 0, "limit": 11520}]


async def test_station_without_smart_charging_sends_nothing(slider16):
    """The backend also guards unsupported chargers when called directly."""
    entity, cp, _ = slider16
    cp._attr_supported_features = Profiles.NONE
    with pytest.raises(HomeAssistantError, match="Smart charging"):
        await entity.async_set_native_value(16)
    cp.call.assert_not_awaited()
    cp.get_configuration.assert_not_awaited()
    assert entity.native_value is None


@pytest.mark.parametrize("connector", [None, 0, 2])
async def test_service_keeps_connector_fallbacks(slider16, connector):
    """Omitted, zero and explicit action connectors still use the full legacy chain."""
    _, cp, central = slider16
    cp.call.side_effect = [
        SimpleNamespace(status=s) for s in ("Rejected", "Accepted", "Accepted")
    ]
    data = {"devid": "test_cpid", "limit_amps": 16}
    if connector is not None:
        data["conn_id"] = connector
    await central.handle_set_charge_rate(SimpleNamespace(data=data))
    target = connector or 1
    requests = [c.args[0] for c in cp.call.call_args_list]
    assert [
        (r.connector_id, r.cs_charging_profiles["chargingProfilePurpose"])
        for r in requests
    ] == [
        (0, "ChargePointMaxProfile"),
        (target, "TxProfile"),
        (target, "TxDefaultProfile"),
    ]
    assert requests[1].cs_charging_profiles["transactionId"] == cp._active_tx[target]
    assert "transactionId" not in requests[2].cs_charging_profiles
    assert [r.cs_charging_profiles["chargingProfileId"] for r in requests] == [
        1000,
        3000 + target,
        2000 + target,
    ]


@pytest.mark.parametrize(
    "limit,status",
    [
        (16, "Accepted"),
        (16, "Rejected"),
        (48, "Accepted"),
        (48, "Rejected"),
        (48, "Unknown"),
    ],
)
async def test_slider201_uses_existing_station_path(
    hass, flat_entry, setup_flat, limit, status
):
    """The new hook retains EVSE 0, the configured clear threshold and refusals."""
    central = await setup_flat()
    cp = _attach_protocol(hass, flat_entry, central, "2.0.1")
    cp.call = AsyncMock(
        return_value=SimpleNamespace(status=status, status_info="charger reason")
    )
    entity = _number(hass)
    entity._confirmed_value = 24
    if status == "Rejected":
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(limit)
        assert entity.native_value == 24
    else:
        await entity.async_set_native_value(limit)
        assert entity.native_value == limit
    cp.call.assert_awaited_once()
    req = cp.call.call_args.args[0]
    if limit < 48:
        assert req.evse_id == 0
        assert (
            req.charging_profile["charging_profile_purpose"]
            == "ChargingStationMaxProfile"
        )
    else:
        assert type(req).__name__ == "ClearChargingProfile"
        assert req.charging_profile_criteria == {
            "charging_profile_purpose": "ChargingStationMaxProfile"
        }
