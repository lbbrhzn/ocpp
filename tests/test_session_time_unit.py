"""Session duration must keep its minutes unit without live charger metrics."""

from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorExtraStoredData, SensorStateClass
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OK,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTime,
)
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.ocpp.chargepoint import _ConnectorAwareMetrics
from custom_components.ocpp.const import (
    CONF_CPIDS,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    DOMAIN,
)
from custom_components.ocpp.enums import HAChargerSession

from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA
from .lifecycle_asserts import live_entity


@pytest.mark.parametrize("num_connectors", [1, 2])
@pytest.mark.parametrize(
    ("restored_value", "restored_unit"),
    [(None, None), (12, UnitOfTime.MINUTES), (12, None), (0, "")],
    ids=["fresh", "restored-minutes", "restored-no-unit", "restored-zero-empty-unit"],
)
async def test_session_time_unit_without_live_metrics(
    hass, socket_enabled, num_connectors, restored_value, restored_unit
):
    """Keep minutes through startup, missing units, disconnection and reload."""
    cpid = "test_cpid"
    connector_id = num_connectors if num_connectors > 1 else None
    suffix = f"_connector_{connector_id}" if connector_id is not None else ""
    entity_id = f"sensor.{cpid}{suffix}_time_session"
    if restored_value is not None:
        mock_restore_cache_with_extra_data(
            hass,
            [
                (
                    State(
                        entity_id,
                        str(restored_value),
                        {ATTR_UNIT_OF_MEASUREMENT: restored_unit},
                    ),
                    SensorExtraStoredData(restored_value, restored_unit).as_dict(),
                )
            ],
        )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CONFIG_DATA,
            CONF_PORT: 0,
            CONF_CPIDS: [
                {
                    "CP_session": {
                        **MOCK_CONFIG_CP_APPEND,
                        CONF_NUM_CONNECTORS: num_connectors,
                    }
                }
            ],
        },
        version=2,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    def assert_state(expected=None):
        """Assert statistics metadata and, when specified, the published value."""
        state = hass.states.get(entity_id)
        assert state is not None
        if expected is not None:
            assert state.state == expected
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.MINUTES
        assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT

    # Before any connection, the entity has no runtime metrics or units.
    assert_state(STATE_UNAVAILABLE)
    sensor = live_entity(hass, entity_id, "sensor")

    central = hass.data[DOMAIN][entry.entry_id]
    charge_point = SimpleNamespace(
        _metrics=_ConnectorAwareMetrics(),
        num_connectors=num_connectors,
        status=STATE_OK,
    )
    central.cpids[cpid] = "CP_session"
    central.charge_points["CP_session"] = charge_point

    # A connection can become available before session metrics are populated.
    sensor.async_write_ha_state()
    assert sensor.available
    assert_state(STATE_UNKNOWN if restored_value is None else None)

    metric = charge_point._metrics[(connector_id or 1, HAChargerSession.session_time)]
    metric.unit = UnitOfTime.MINUTES
    for value in (0, 12):
        metric.value = value
        sensor.async_write_ha_state()
        assert_state(str(value))

    # Empty units are normalised to None by the central system. Neither form
    # may strip the unit from a still-valid session value.
    for missing_unit in (None, ""):
        metric.unit = missing_unit
        sensor.async_write_ha_state()
        assert_state("12")

    # Losing the backend must publish unavailable, retaining the minutes unit.
    central.charge_points.clear()
    sensor.async_write_ha_state()
    assert_state(STATE_UNAVAILABLE)

    # Reload uses a fresh entity while the charger is offline. Restoring sensor
    # data must not restore the old unit-loss bug with it.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = live_entity(hass, entity_id, "sensor")
    assert reloaded is not sensor
    assert_state(STATE_UNAVAILABLE)

    # A reconnect gets fresh, uninitialised metrics. Assert unit and availability
    # without requiring a cached value to be republished before live data arrives.
    central = hass.data[DOMAIN][entry.entry_id]
    charge_point._metrics = _ConnectorAwareMetrics()
    charge_point.status = "init"
    central.cpids[cpid] = "CP_session"
    central.charge_points["CP_session"] = charge_point
    reloaded.async_write_ha_state()
    assert_state(STATE_UNAVAILABLE)
    charge_point.status = STATE_OK
    reloaded.async_write_ha_state()
    assert reloaded.available
    assert_state()
    charge_point._metrics[(connector_id or 1, HAChargerSession.session_time)].value = 0
    reloaded.async_write_ha_state()
    assert_state("0")

    central.charge_points.clear()
    assert await hass.config_entries.async_unload(entry.entry_id)
