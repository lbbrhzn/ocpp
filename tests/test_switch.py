"""Test switch entities for single and multi-connector chargers."""

import asyncio
import copy
import pytest
import websockets
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    DOMAIN as OCPP_DOMAIN,
)
from custom_components.ocpp.switch import SWITCHES
from tests.charge_point_test import create_configuration, remove_configuration
from tests.const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA


@pytest.mark.asyncio
async def test_switch_entities_single_connector(hass, socket_enabled):
    """Test switch entities are created correctly for single-connector charger."""
    cp_id = "CP_1_switch_sc"
    cpid = "test_cpid_switch_sc"

    data = copy.deepcopy(MOCK_CONFIG_DATA)
    cp_data = copy.deepcopy(MOCK_CONFIG_CP_APPEND)
    cp_data[CONF_CPID] = cpid
    # Single connector (default, no CONF_NUM_CONNECTORS set)
    data[CONF_CPIDS].append({cp_id: cp_data})
    data[CONF_PORT] = 9051

    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=data,
        entry_id="test_cms_switch_sc",
        title="test_cms_switch_sc",
        version=2,
        minor_version=0,
    )

    await create_configuration(hass, config_entry)

    # Open a ws once to trigger platform setup
    async with websockets.connect(
        f"ws://127.0.0.1:{data[CONF_PORT]}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # Give HA a tick to register entities
        for _ in range(5):
            entity = hass.states.get("switch.test_cms_switch_sc")
            if entity:
                break
            await asyncio.sleep(0.5)

        # For single-connector charger, check expected switches exist
        for switch_desc in SWITCHES:
            if switch_desc.per_connector:
                # Per-connector switches without connector suffix for single-connector
                if switch_desc.key == "connector_availability":
                    # This switch should NOT exist for single-connector charger
                    entity_id = f"switch.{cpid}_{switch_desc.key}"
                    entity = hass.states.get(entity_id)
                    assert (
                        entity is None
                    ), f"connector_availability switch should not exist for single-connector: {entity_id}"
                else:
                    # charge_control should exist without connector suffix
                    entity_id = f"switch.{cpid}_{switch_desc.key}"
                    entity = hass.states.get(entity_id)
                    assert (
                        entity is not None
                    ), f"missing per-connector switch for single-connector: {entity_id}"
            else:
                # Non-per-connector switches (availability)
                entity_id = f"switch.{cpid}_{switch_desc.key}"
                entity = hass.states.get(entity_id)
                assert entity is not None, f"missing switch: {entity_id}"

        await ws.close()

    await remove_configuration(hass, config_entry)


@pytest.mark.asyncio
async def test_switch_entities_multi_connector(hass, socket_enabled):
    """Test switch entities are created correctly for multi-connector charger."""
    cp_id = "CP_1_switch_mc"
    cpid = "test_cpid_switch_mc"

    data = copy.deepcopy(MOCK_CONFIG_DATA)
    cp_data = copy.deepcopy(MOCK_CONFIG_CP_APPEND)
    cp_data[CONF_CPID] = cpid
    cp_data[CONF_NUM_CONNECTORS] = 2  # Multi-connector
    data[CONF_CPIDS].append({cp_id: cp_data})
    data[CONF_PORT] = 9052

    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=data,
        entry_id="test_cms_switch_mc",
        title="test_cms_switch_mc",
        version=2,
        minor_version=0,
    )

    await create_configuration(hass, config_entry)

    # Open a ws once to trigger platform setup
    async with websockets.connect(
        f"ws://127.0.0.1:{data[CONF_PORT]}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # Give HA a tick to register entities
        for _ in range(5):
            entity = hass.states.get("switch.test_cms_switch_mc")
            if entity:
                break
            await asyncio.sleep(0.5)

        # For multi-connector charger, check expected switches exist
        for switch_desc in SWITCHES:
            if switch_desc.per_connector:
                # Per-connector switches should have connector suffix
                for conn_id in range(1, 3):  # 2 connectors
                    if switch_desc.key == "connector_availability":
                        # This switch SHOULD exist for multi-connector charger
                        entity_id = (
                            f"switch.{cpid}_connector_{conn_id}_{switch_desc.key}"
                        )
                        entity = hass.states.get(entity_id)
                        assert (
                            entity is not None
                        ), f"missing connector_availability switch for connector {conn_id}: {entity_id}"
                    else:
                        # charge_control per connector
                        entity_id = (
                            f"switch.{cpid}_connector_{conn_id}_{switch_desc.key}"
                        )
                        entity = hass.states.get(entity_id)
                        assert (
                            entity is not None
                        ), f"missing per-connector switch for connector {conn_id}: {entity_id}"
            else:
                # Non-per-connector switches (availability) - charger-level only
                entity_id = f"switch.{cpid}_{switch_desc.key}"
                entity = hass.states.get(entity_id)
                assert entity is not None, f"missing charger-level switch: {entity_id}"

        # Verify no switch exists for non-existent connector 3
        entity_id = f"switch.{cpid}_connector_3_charge_control"
        entity = hass.states.get(entity_id)
        assert entity is None, f"unexpected switch for connector 3: {entity_id}"

        await ws.close()

    await remove_configuration(hass, config_entry)
