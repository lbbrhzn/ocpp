"""Charger is fed three-phase, from the stored option to the limit sent (#2224)."""

import asyncio
import contextlib
import copy
from datetime import UTC, datetime

import pytest
import websockets
from ocpp.routing import on
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, ChargingProfileStatus, Phase
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import (
    CONF_CPIDS,
    CONF_PORT,
    CONF_THREE_PHASE_SUPPLY,
    DOMAIN,
)

from .charge_point_test import create_configuration, remove_configuration, wait_ready
from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA
from .test_charge_point_v16 import ChargePoint

CP_ID = "CP_three_phase"


class PowerOnlyChargePoint(ChargePoint):
    """A 1.6 charger that takes limits in watts only and keeps what it is sent."""

    def __init__(self, *args, **kwargs):
        """Start with no profiles received."""
        super().__init__(*args, **kwargs)
        self.profiles = []

    @on(Action.get_configuration)
    def on_get_configuration(self, key, **kwargs):
        """Report Power as the only rate unit."""
        if key[0] == "ChargingScheduleAllowedChargingRateUnit":
            return call_result.GetConfiguration(
                configuration_key=[{"key": key[0], "readonly": True, "value": "Power"}]
            )
        return super().on_get_configuration(key, **kwargs)

    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, connector_id, cs_charging_profiles, **kwargs):
        """Accept every profile."""
        self.profiles.append(cs_charging_profiles["charging_schedule"])
        return call_result.SetChargingProfile(ChargingProfileStatus.accepted)


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    ("stored", "port", "watts"),
    [({CONF_THREE_PHASE_SUPPLY: True}, 9534, 11040), ({}, 9535, 3680)],
    ids=["option_on", "option_absent"],
)
async def test_maximum_current_goes_out_for_three_phases_when_set(
    hass, socket_enabled, stored, port, watts
):
    """The stored option sets the watts sent, before meter values and after."""
    config = copy.deepcopy(MOCK_CONFIG_DATA)
    config[CONF_CPIDS] = [{CP_ID: {**copy.deepcopy(MOCK_CONFIG_CP_APPEND), **stored}}]
    config[CONF_PORT] = port
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        entry_id=f"cms_three_phase_{port}",
        title=f"cms_three_phase_{port}",
        version=2,
        minor_version=3,
    )
    central = await create_configuration(hass, entry)
    slider = f"number.{MOCK_CONFIG_CP_APPEND['cpid']}_maximum_current"

    async def set_slider_and_read_limit(charger) -> tuple[str, float]:
        sent = len(charger.profiles)
        await hass.services.async_call(
            "number", "set_value", {"entity_id": slider, "value": 16}, blocking=True
        )
        assert len(charger.profiles) == sent + 1
        schedule = charger.profiles[-1]
        return (
            schedule["charging_rate_unit"],
            schedule["charging_schedule_period"][0]["limit"],
        )

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/{CP_ID}", subprotocols=["ocpp1.6"]
        ) as ws:
            charger = PowerOnlyChargePoint(f"{CP_ID}_client", ws)
            task = asyncio.create_task(charger.start())
            try:
                await charger.send_boot_notification()
                await wait_ready(central.charge_points[CP_ID])
                await hass.async_block_till_done()

                # As after a restart: no meter values yet.
                assert await set_slider_and_read_limit(charger) == ("W", watts)

                # A one-phase car draws on L1 only.
                await charger.send_start_transaction(0)
                await charger.call(
                    call.MeterValues(
                        connector_id=1,
                        transaction_id=charger.active_transactionId,
                        meter_value=[
                            {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "sampledValue": [
                                    {
                                        "measurand": "Current.Import",
                                        "phase": phase,
                                        "unit": "A",
                                        "value": value,
                                    }
                                    for phase, value in (
                                        (Phase.l1.value, "16"),
                                        (Phase.l2.value, "0"),
                                        (Phase.l3.value, "0"),
                                    )
                                ],
                            }
                        ],
                    )
                )
                # Meter values from it must not undo the option.
                assert await set_slider_and_read_limit(charger) == ("W", watts)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    finally:
        await remove_configuration(hass, entry)
