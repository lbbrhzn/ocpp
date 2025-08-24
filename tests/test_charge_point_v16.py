"""Implement a test by a simulating an OCPP 1.6 chargepoint."""

import asyncio
import contextlib
from datetime import datetime, UTC  # timedelta,
import logging
import re

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.exceptions import HomeAssistantError
import websockets

from custom_components.ocpp.api import CentralSystem
from custom_components.ocpp.button import BUTTONS
from custom_components.ocpp.chargepoint import Metric as M
from custom_components.ocpp.const import (
    DOMAIN as OCPP_DOMAIN,
    CONF_CPIDS,
    CONF_CPID,
    CONF_PORT,
)
from custom_components.ocpp.enums import (
    ConfigurationKey,
    HAChargerServices as csvcs,
    Profiles as prof,
)
from custom_components.ocpp.number import NUMBERS
from custom_components.ocpp.switch import SWITCHES
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cpclass, call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfileStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    DiagnosticsStatus,
    FirmwareStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    TriggerMessageStatus,
    UnlockStatus,
)

from .const import (
    MOCK_CONFIG_DATA,
    MOCK_CONFIG_CP_APPEND,
)
from .charge_point_test import (
    set_switch,
    press_button,
    set_number,
    create_configuration,
    remove_configuration,
    wait_ready,
)

SERVICES = [
    csvcs.service_update_firmware,
    csvcs.service_configure,
    csvcs.service_get_configuration,
    csvcs.service_get_diagnostics,
    csvcs.service_trigger_custom_message,
    csvcs.service_clear_profile,
    csvcs.service_data_transfer,
    csvcs.service_set_charge_rate,
]


SERVICES_ERROR = [
    csvcs.service_configure,
    csvcs.service_get_configuration,
    csvcs.service_trigger_custom_message,
    csvcs.service_clear_profile,
    csvcs.service_data_transfer,
    csvcs.service_set_charge_rate,
]


async def test_switches(hass, cpid, socket_enabled):
    """Test switch operations."""
    for switch in SWITCHES:
        await set_switch(hass, cpid, switch.key, True)
        await asyncio.sleep(1)
        await set_switch(hass, cpid, switch.key, False)


test_switches.__test__ = False


async def test_buttons(hass, cpid, socket_enabled):
    """Test button operations."""
    for button in BUTTONS:
        await press_button(hass, cpid, button.key)


test_buttons.__test__ = False


async def test_services(hass, cpid, serv_list, socket_enabled):
    """Test service operations."""

    for service in serv_list:
        data = {"devid": cpid}
        if service == csvcs.service_update_firmware:
            data.update({"firmware_url": "http://www.charger.com/firmware.bin"})
        if service == csvcs.service_configure:
            data.update({"ocpp_key": "WebSocketPingInterval", "value": "60"})
            await hass.services.async_call(
                OCPP_DOMAIN,
                service.value,
                service_data=data,
                blocking=True,
                return_response=True,
            )
            break
        if service == csvcs.service_get_configuration:
            data.update({"ocpp_key": "UnknownKeyTest"})
            await hass.services.async_call(
                OCPP_DOMAIN,
                service.value,
                service_data=data,
                blocking=True,
                return_response=True,
            )
            break
        if service == csvcs.service_get_diagnostics:
            data.update({"upload_url": "https://webhook.site/abc"})
        if service == csvcs.service_data_transfer:
            data.update({"vendor_id": "ABC"})
        if service == csvcs.service_set_charge_rate:
            data.update({"limit_amps": 30})
        if service == csvcs.service_trigger_custom_message:
            data.update({"requested_message:": "StatusNotification"})

        await hass.services.async_call(
            OCPP_DOMAIN,
            service.value,
            service_data=data,
            blocking=True,
        )
    # test additional set charge rate options
    await hass.services.async_call(
        OCPP_DOMAIN,
        csvcs.service_set_charge_rate,
        service_data={"devid": cpid, "limit_watts": 3000},
        blocking=True,
    )
    # test custom charge profile for advanced use
    prof = {
        "chargingProfileId": 8,
        "stackLevel": 6,
        "chargingProfileKind": "Relative",
        "chargingProfilePurpose": "ChargePointMaxProfile",
        "chargingSchedule": {
            "chargingRateUnit": "A",
            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16.0}],
        },
    }
    data = {"devid": cpid, "custom_profile": str(prof)}
    await hass.services.async_call(
        OCPP_DOMAIN,
        csvcs.service_set_charge_rate,
        service_data=data,
        blocking=True,
    )
    # test custom message request for MeterValues
    await hass.services.async_call(
        OCPP_DOMAIN,
        csvcs.service_trigger_custom_message,
        service_data={"devid": cpid, "requested_message": "MeterValues"},
        blocking=True,
    )

    for number in NUMBERS:
        # test setting value of number slider
        await set_number(hass, cpid, number.key, 10)


test_services.__test__ = False


@pytest.fixture
async def setup_config_entry(hass, request) -> CentralSystem:
    """Setup/teardown mock config entry and central system."""
    # Create a mock entry so we don't have to go through config flow
    # Both version and minor need to match config flow so as not to trigger migration flow
    config_data = MOCK_CONFIG_DATA.copy()
    config_data[CONF_CPIDS].append(
        {request.param["cp_id"]: MOCK_CONFIG_CP_APPEND.copy()}
    )
    config_data[CONF_PORT] = request.param["port"]
    config_entry = MockConfigEntry(
        domain=OCPP_DOMAIN,
        data=config_data,
        entry_id=request.param["cms"],
        title=request.param["cms"],
        version=2,
        minor_version=0,
    )
    yield await create_configuration(hass, config_entry)
    # tear down
    await remove_configuration(hass, config_entry)


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)  # Set timeout for this test
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9001, "cp_id": "CP_1_nosub", "cms": "cms_nosub"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_nosub"])
@pytest.mark.parametrize("port", [9001])
async def test_cms_responses_nosub_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test central system responses to a charger with no subprotocol."""

    # no subprotocol central system assumes ocpp1.6 charge point
    # NB each new config entry will trigger async_update_entry
    # if the charger measurands differ from the config entry
    # which causes the websocket server to close/restart with a
    # ConnectionClosedOK exception, hence it needs to be passed/suppressed

    async with (
        websockets.connect(
            f"ws://127.0.0.1:{port}/{cp_id}",  # this is the charger cp_id ie CP_1_nosub in the cs
        ) as ws2
    ):
        assert ws2.subprotocol is None
        # Note this mocks a real charger and is not the charger representation in the cs, which is accessed by cp_id
        cp2 = ChargePoint(
            f"{cp_id}_client", ws2
        )  # uses a different id for debugging, would normally be cp_id
        with contextlib.suppress(
            asyncio.TimeoutError, websockets.exceptions.ConnectionClosedOK
        ):
            await asyncio.wait_for(
                asyncio.gather(
                    cp2.start(),
                    cp2.send_boot_notification(),
                    cp2.send_authorize(),
                    cp2.send_heartbeat(),
                    cp2.send_status_notification(),
                    cp2.send_firmware_status(),
                    cp2.send_data_transfer(),
                    cp2.send_start_transaction(),
                    cp2.send_stop_transaction(),
                    cp2.send_meter_periodic_data(),
                ),
                timeout=10,
            )
        await ws2.close()


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)  # Set timeout for this test
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9002, "cp_id": "CP_1_unsup", "cms": "cms_unsup"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_unsup"])
@pytest.mark.parametrize("port", [9002])
async def test_cms_responses_unsupp_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test central system unsupported protocol."""

    # unsupported subprotocol raises websockets exception
    with pytest.raises(websockets.exceptions.InvalidStatus):
        await websockets.connect(
            f"ws://127.0.0.1:{port}/{cp_id}",
            subprotocols=["ocpp0.0"],
        )


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)  # Set timeout for this test
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9003, "cp_id": "CP_1_restore_values", "cms": "cms_restore_values"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_restore_values"])
@pytest.mark.parametrize("port", [9003])
async def test_cms_responses_restore_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test central system restoring values for a charger."""

    cs = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp.active_transactionId = None
        # send None values
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_meter_periodic_data(),
                ),
                timeout=3,
            )
        # cpid set in cs after websocket connection
        cpid = cs.charge_points[cp_id].settings.cpid

        # check if None
        assert cs.get_metric(cpid, "Energy.Meter.Start") is None
        assert cs.get_metric(cpid, "Transaction.Id") is None

        # send new data
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_boot_notification(),
                    cp.send_start_transaction(12344),
                    cp.send_meter_periodic_data(),
                ),
                timeout=3,
            )

        # save for reference the values for meter_start and transaction_id
        saved_meter_start = int(cs.get_metric(cpid, "Energy.Meter.Start"))
        saved_transactionId = int(cs.get_metric(cpid, "Transaction.Id"))

        # delete current values from api memory
        cs.del_metric(cpid, "Energy.Meter.Start")
        cs.del_metric(cpid, "Transaction.Id")
        # send new data
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_boot_notification(),
                    cp.send_meter_periodic_data(),
                ),
                timeout=3,
            )
        await ws.close()

    # check if restored old values from HA when api have lost the values, i.e. simulated reboot of HA
    assert int(cs.get_metric(cpid, "Energy.Meter.Start")) == saved_meter_start
    assert int(cs.get_metric(cpid, "Transaction.Id")) == saved_transactionId


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)  # Set timeout for this test
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9004, "cp_id": "CP_1_norm", "cms": "cms_norm"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_norm"])
@pytest.mark.parametrize("port", [9004])
async def test_cms_responses_normal_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test central system responses to a charger under normal operation."""

    cs = setup_config_entry

    # test ocpp messages sent from charger to cms
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.5", "ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])
        await cp.send_boot_notification()
        await cp.send_authorize()
        await cp.send_heartbeat()
        await cp.send_status_notification()
        await cp.send_security_event()
        await cp.send_firmware_status()
        await cp.send_data_transfer()
        await cp.send_start_transaction(12345)
        await cp.send_meter_err_phases()
        await cp.send_meter_line_voltage()
        await cp.send_meter_periodic_data()
        # add delay to allow meter data to be processed
        await cp.send_stop_transaction(1)

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()

    cpid = cs.charge_points[cp_id].settings.cpid
    assert int(cs.get_metric(cpid, "Energy.Active.Import.Register")) == int(
        1305570 / 1000
    )
    assert int(cs.get_metric(cpid, "Energy.Session")) == int((54321 - 12345) / 1000)
    assert int(cs.get_metric(cpid, "Current.Import")) == 0
    # assert int(cs.get_metric(cpid, "Voltage")) == 228
    assert cs.get_unit(cpid, "Energy.Active.Import.Register") == "kWh"
    assert cs.get_ha_unit(cpid, "Power.Reactive.Import") == "var"
    assert cs.get_unit(cpid, "Power.Reactive.Import") == "var"
    assert cs.get_metric("unknown_cpid", "Energy.Active.Import.Register") is None
    assert cs.get_unit("unknown_cpid", "Energy.Active.Import.Register") is None
    assert cs.get_extra_attr("unknown_cpid", "Energy.Active.Import.Register") is None
    assert int(cs.get_supported_features("unknown_cpid")) == 0
    assert (
        await asyncio.wait_for(
            cs.set_max_charge_rate_amps("unknown_cpid", 0), timeout=1
        )
        is False
    )
    assert (
        await asyncio.wait_for(
            cs.set_charger_state("unknown_cpid", csvcs.service_clear_profile, False),
            timeout=1,
        )
        is False
    )


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)  # Set timeout for this test
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9005, "cp_id": "CP_1_services", "cms": "cms_services"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_services"])
@pytest.mark.parametrize("port", [9005])
async def test_cms_responses_actions_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test central system responses to actions and multi charger under normal operation."""
    # start clean entry for services
    cs = setup_config_entry

    # test ocpp messages sent from cms to charger, through HA switches/services
    # should reconnect as already started above
    # test processing of clock aligned meter data
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            cp_task = asyncio.create_task(cp.start())
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            # Confirm charger completed post_connect before running services
            await asyncio.wait_for(
                asyncio.gather(
                    cp.send_meter_clock_data(),
                    # cs.charge_points[cp_id].trigger_boot_notification(),
                    # cs.charge_points[cp_id].trigger_status_notification(),
                    test_switches(
                        hass,
                        cs.charge_points[cp_id].settings.cpid,
                        socket_enabled,
                    ),
                    test_services(
                        hass,
                        cs.charge_points[cp_id].settings.cpid,
                        SERVICES,
                        socket_enabled,
                    ),
                    test_buttons(
                        hass,
                        cs.charge_points[cp_id].settings.cpid,
                        socket_enabled,
                    ),
                ),
                timeout=10,
            )
            cp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cp_task
        await ws.close()

    # cpid set in cs after websocket connection
    cpid = cs.charge_points[cp_id].settings.cpid

    assert int(cs.get_metric(cpid, "Frequency")) == 50
    assert float(cs.get_metric(cpid, "Energy.Active.Import.Register")) == 1101.452

    # add new charger to config entry
    cp_id = "CP_1_non_er_3.9"
    entry = hass.config_entries._entries.get_entries_for_domain(OCPP_DOMAIN)[0]
    entry.data[CONF_CPIDS].append({cp_id: MOCK_CONFIG_CP_APPEND.copy()})
    entry.data[CONF_CPIDS][-1][cp_id][CONF_CPID] = "cpid2"
    # reload required to setup new charger in HA, normally happens with discovery flow
    assert await hass.config_entries.async_reload(entry.entry_id)
    cs = hass.data[OCPP_DOMAIN][entry.entry_id]

    # test ocpp messages sent from charger that don't support errata 3.9
    # i.e. "Energy.Meter.Start" starts from 0 for each session and "Energy.Active.Import.Register"
    # reports starting from 0 Wh for every new transaction id. Total main meter values are without transaction id.

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint(f"{cp_id}_client", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_boot_notification(),
                    cp.send_start_transaction(0),
                    cp.send_meter_periodic_data(),
                    cp.send_main_meter_clock_data(),
                    # add delay to allow meter data to be processed
                    cp.send_stop_transaction(2),
                ),
                timeout=5,
            )
        await ws.close()

    cpid = cs.charge_points[cp_id].settings.cpid
    # Last sent "Energy.Active.Import.Register" value without transaction id should be here.
    assert int(cs.get_metric(cpid, "Energy.Active.Import.Register")) == int(
        67230012 / 1000
    )
    assert cs.get_unit(cpid, "Energy.Active.Import.Register") == "kWh"

    # Last sent "Energy.Active.Import.Register" value with transaction id should be here.
    # Meter value sent with stop transaction should not be used to calculate session energy
    assert int(cs.get_metric(cpid, "Energy.Session")) == int(1305570 / 1000)
    assert cs.get_unit(cpid, "Energy.Session") == "kWh"

    # test ocpp messages sent from charger that don't support errata 3.9 with meter values with kWh as energy unit
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint(f"{cp_id}_client", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_boot_notification(),
                    cp.send_start_transaction(0),
                    cp.send_meter_energy_kwh(),
                    cp.send_meter_clock_data(),
                    # add delay to allow meter data to be processed
                    cp.send_stop_transaction(2),
                ),
                timeout=5,
            )
        await ws.close()

    assert int(cs.get_metric(cpid, "Energy.Active.Import.Register")) == 1101
    assert int(cs.get_metric(cpid, "Energy.Session")) == 11
    assert cs.get_unit(cpid, "Energy.Active.Import.Register") == "kWh"


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)  # Set timeout for this test
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9006, "cp_id": "CP_1_error", "cms": "cms_error"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_error"])
@pytest.mark.parametrize("port", [9006])
async def test_cms_responses_errors_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test central system responses to actions and multi charger under error operation."""
    # start clean entry for services
    cs = setup_config_entry

    # test ocpp rejection messages sent from charger to cms
    # use SERVICES_ERROR as only Core and Smart profiles enabled
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        with contextlib.suppress(
            asyncio.TimeoutError, websockets.exceptions.ConnectionClosedOK
        ):
            cp = ChargePoint(f"{cp_id}_client", ws)
            cp.accept = False
            # Allow charger time to connect before running services
            await asyncio.wait_for(
                cp.start(),
                timeout=5,
            )
        await ws.close()
    # if monitored variables differ cs will restart and charger needs to reconnect
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        with contextlib.suppress(
            asyncio.TimeoutError, websockets.exceptions.ConnectionClosedOK
        ):
            cp = ChargePoint(f"{cp_id}_client", ws)
            cp.accept = False
            cp_task = asyncio.create_task(cp.start())
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])
            # Confirm charger completed post_connect before running services
            await asyncio.wait_for(
                asyncio.gather(
                    cs.charge_points[cp_id].trigger_boot_notification(),
                    cs.charge_points[cp_id].trigger_status_notification(),
                    test_switches(
                        hass,
                        cs.charge_points[cp_id].settings.cpid,
                        socket_enabled,
                    ),
                    test_services(
                        hass,
                        "xxx",  # Test with incorrect devid supplied
                        SERVICES_ERROR,
                        socket_enabled,
                    ),
                    test_buttons(
                        hass,
                        cs.charge_points[cp_id].settings.cpid,
                        socket_enabled,
                    ),
                ),
                timeout=10,
            )
            await cs.charge_points[cp_id].stop()
            cp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cp_task
        await ws.close()

    # test services when charger is unavailable
    with pytest.raises(HomeAssistantError):
        await test_services(
            hass, cs.charge_points[cp_id].settings.cpid, SERVICES_ERROR, socket_enabled
        )


@pytest.mark.timeout(20)  # Set timeout for this test
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9007, "cp_id": "CP_1_norm_mc", "cms": "cms_norm"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_norm_mc"])
@pytest.mark.parametrize("port", [9007])
async def test_cms_responses_normal_multiple_connectors_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Test central system responses to a charger.

    Normal operation with multiple connectors.
    """

    cs = setup_config_entry
    num_connectors = 2

    # test ocpp messages sent from charger to cms
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.5", "ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint(f"{cp_id}_client", ws, no_connectors=num_connectors)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])
        await cp.send_boot_notification()
        await cp.send_authorize()
        await cp.send_heartbeat()
        await cp.send_status_notification()
        await cp.send_security_event()
        await cp.send_firmware_status()
        await cp.send_data_transfer()
        await cp.send_start_transaction(12345)
        await cp.send_meter_err_phases()
        await cp.send_meter_line_voltage()
        await cp.send_meter_periodic_data()
        # add delay to allow meter data to be processed
        await cp.send_stop_transaction(1)

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()

    cpid = cs.charge_points[cp_id].settings.cpid

    assert int(
        cs.get_metric(cpid, "Energy.Active.Import.Register", connector_id=1)
    ) == int(1305570 / 1000)
    assert int(cs.get_metric(cpid, "Energy.Session", connector_id=1)) == int(
        (54321 - 12345) / 1000
    )
    assert int(cs.get_metric(cpid, "Current.Import", connector_id=1)) == 0
    # assert int(cs.get_metric(cpid, "Voltage")) == 228
    assert cs.get_unit(cpid, "Energy.Active.Import.Register", connector_id=1) == "kWh"
    assert cs.get_ha_unit(cpid, "Power.Reactive.Import", connector_id=1) == "var"
    assert cs.get_unit(cpid, "Power.Reactive.Import", connector_id=1) == "var"
    assert cs.get_metric("unknown_cpid", "Energy.Active.Import.Register") is None
    assert cs.get_unit("unknown_cpid", "Energy.Active.Import.Register") is None
    assert cs.get_extra_attr("unknown_cpid", "Energy.Active.Import.Register") is None
    assert int(cs.get_supported_features("unknown_cpid")) == 0
    assert (
        await asyncio.wait_for(
            cs.set_max_charge_rate_amps("unknown_cpid", 0), timeout=1
        )
        is False
    )
    assert (
        await asyncio.wait_for(
            cs.set_charger_state("unknown_cpid", csvcs.service_clear_profile, False),
            timeout=1,
        )
        is False
    )


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9009, "cp_id": "CP_1_clear", "cms": "cms_clear"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_clear"])
@pytest.mark.parametrize("port", [9009])
async def test_clear_profile_v16(hass, socket_enabled, cp_id, port, setup_config_entry):
    """Verify that HA's clear_profile service triggers OCPP 1.6 ClearChargingProfile."""

    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        # Make CP ready so HA can run services
        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        cpid = cs.charge_points[cp_id].settings.cpid

        # Minimal clear: no filters -> clears any CS/CP max profiles
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_clear_profile.value,
            service_data={"devid": cpid},
            blocking=True,
        )

        # Let the request propagate
        await asyncio.sleep(0.05)

        # Assert the CP handler was called
        assert cp.last_clear_profile_kwargs is not None
        # Common default: empty dict (no id/purpose/stack/connector filters)
        assert isinstance(cp.last_clear_profile_kwargs, dict)

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


async def set_report_session_energyreport(
    cs: CentralSystem, cp_id: str, should_report: bool
):
    """Set report session energy report True/False."""
    cs.charge_points[cp_id]._charger_reports_session_energy = should_report


set_report_session_energyreport.__test__ = False


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9010, "cp_id": "CP_1_stop_paths", "cms": "cms_stop_paths"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_stop_paths"])
@pytest.mark.parametrize("port", [9010])
async def test_stop_transaction_paths_v16_a(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Exercise all branches of ocppv16.on_stop_transaction."""
    cs: CentralSystem = setup_config_entry

    #
    # SCENARIO A: _charger_reports_session_energy = True and SessionEnergy is None
    #             Use last Energy.Active.Import.Register to populate SessionEnergy.
    #
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        cs.charge_points[cp_id]._charger_reports_session_energy = True

        # Ensure there is an active tx so stop is accepted
        await cp.send_start_transaction(meter_start=0)

        # Force SessionEnergy to be None before stop
        m = cs.charge_points[cp_id]._metrics
        m[(1, "Energy.Session")].value = None  # connector 1

        # Case A1: last EAIR in Wh → should convert to kWh
        m[(1, "Energy.Active.Import.Register")].value = 1300000  # Wh
        m[(1, "Energy.Active.Import.Register")].unit = "Wh"

        await cp.send_stop_transaction(delay=0)

        cpid = cs.charge_points[cp_id].settings.cpid
        sess = float(cs.get_metric(cpid, "Energy.Session", connector_id=1))
        assert round(sess, 3) == 1300000 / 1000.0
        assert cs.get_unit(cpid, "Energy.Session", connector_id=1) == "kWh"

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9021, "cp_id": "CP_1_stop_paths_a1", "cms": "cms_stop_paths_a1"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_stop_paths_a1"])
@pytest.mark.parametrize("port", [9021])
async def test_stop_transaction_paths_v16_a1(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Exercise all branches of ocppv16.on_stop_transaction."""
    cs: CentralSystem = setup_config_entry

    #
    # SCENARIO A (variant): charger reports session energy AND last EAIR already kWh.
    #
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        cs.charge_points[cp_id]._charger_reports_session_energy = True
        await cp.send_start_transaction(meter_start=0)

        m = cs.charge_points[cp_id]._metrics
        m[(1, "Energy.Session")].value = None
        m[(1, "Energy.Active.Import.Register")].value = 42.5  # already kWh
        m[(1, "Energy.Active.Import.Register")].unit = "kWh"

        await cp.send_stop_transaction(delay=0)

        cpid = cs.charge_points[cp_id].settings.cpid
        sess = float(cs.get_metric(cpid, "Energy.Session", connector_id=1))
        assert round(sess, 3) == 42.5
        assert cs.get_unit(cpid, "Energy.Session", connector_id=1) == "kWh"

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9022, "cp_id": "CP_1_stop_paths_b", "cms": "cms_stop_paths_b"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_stop_paths_b"])
@pytest.mark.parametrize("port", [9022])
async def test_stop_transaction_paths_v16_b(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Exercise all branches of ocppv16.on_stop_transaction."""
    cs: CentralSystem = setup_config_entry

    #
    # SCENARIO B: charger reports session energy BUT SessionEnergy already set → do not overwrite.
    #
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        cs.charge_points[cp_id]._charger_reports_session_energy = True
        await cp.send_start_transaction(meter_start=0)

        m = cs.charge_points[cp_id]._metrics
        # Pre-set SessionEnergy (should remain unchanged)
        m[(1, "Energy.Session")].value = 7.777
        m[(1, "Energy.Session")].unit = "kWh"

        # Set EAIR to a different value to ensure we would notice an overwrite
        m[(1, "Energy.Active.Import.Register")].value = 999999
        m[(1, "Energy.Active.Import.Register")].unit = "Wh"

        await cp.send_stop_transaction(delay=0)

        cpid = cs.charge_points[cp_id].settings.cpid
        sess = float(cs.get_metric(cpid, "Energy.Session", connector_id=1))
        assert round(sess, 3) == 7.777  # unchanged

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9023, "cp_id": "CP_1_stop_paths_c", "cms": "cms_stop_paths_c"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_stop_paths_c"])
@pytest.mark.parametrize("port", [9023])
async def test_stop_transaction_paths_v16_c(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Exercise all branches of ocppv16.on_stop_transaction."""
    cs: CentralSystem = setup_config_entry

    #
    # SCENARIO C: _charger_reports_session_energy = False -> compute from meter_stop - meter_start
    #
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_boot_notification(),
                    cp.send_start_transaction(12345),
                    set_report_session_energyreport(cs, cp_id, False),
                    cp.send_stop_transaction(1),
                ),
                timeout=8,
            )
        await ws.close()

        cpid = cs.charge_points[cp_id].settings.cpid

        # Expect session = 54.321 - 12.345 = 41.976 kWh
        sess = float(cs.get_metric(cpid, "Energy.Session"))
        assert round(sess, 3) == round(54.321 - 12.345, 3)
        assert cs.get_unit(cpid, "Energy.Session") == "kWh"

        # After stop, these measurands must be zeroed
        for meas in [
            "Current.Import",
            "Power.Active.Import",
            "Power.Reactive.Import",
            "Current.Export",
            "Power.Active.Export",
            "Power.Reactive.Export",
        ]:
            assert float(cs.get_metric(cpid, meas)) == 0.0

        # Optional: stop reason captured
        assert cs.get_metric(cpid, "Stop.Reason") is not None

        await ws.close()


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9011, "cp_id": "CP_1_meter_paths", "cms": "cms_meter_paths"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_meter_paths"])
@pytest.mark.parametrize("port", [9011])
async def test_on_meter_values_paths_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Exercise important branches of ocppv16.on_meter_values.

    - Main meter (EAIR) without transaction_id -> connector 0 (kWh)
    - Restore meter_start/transaction_id when missing
    - With transaction_id and match -> update Energy.Session
    - Empty strings for other measurands -> coerced to 0.0
    """
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)

        # Keep the OCPP task running in the background.
        cp_task = asyncio.create_task(cp.start())
        try:
            # Boot (enough for the CS to register the CPID).
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            cpid = cs.charge_points[cp_id].settings.cpid

            # 1) Start a transaction so the helper for "main meter" won't block.
            await cp.send_start_transaction(meter_start=10000)  # Wh (10 kWh)
            # Give CS a tick to persist state.
            await asyncio.sleep(0.1)
            active_tx = cs.charge_points[cp_id].active_transaction_id
            assert active_tx != 0

            # 2) MAIN METER (no transaction_id): updates aggregate connector (0) in kWh
            #    Note: helper waits for active tx, but still omits transaction_id in the message.
            await cp.send_main_meter_clock_data()
            agg_eair = float(
                cs.get_metric(cpid, "Energy.Active.Import.Register", connector_id=0)
            )
            assert agg_eair == pytest.approx(67230012 / 1000.0, rel=1e-6)
            assert (
                cs.get_unit(cpid, "Energy.Active.Import.Register", connector_id=0)
                == "kWh"
            )

            # 3) Force-loss: clear meter_start and transaction_id; provide last EAIR to restore from.
            m = cs.charge_points[cp_id]._metrics
            m[(1, "Energy.Meter.Start")].value = None
            m[(1, "Transaction.Id")].value = None
            m[(1, "Energy.Active.Import.Register")].value = 12.5
            m[(1, "Energy.Active.Import.Register")].unit = "kWh"

            # 4) Send MeterValues WITH transaction_id and include:
            #    - EAIR = 15000 Wh (-> 15.0 kWh)
            #    - Power.Active.Import = "" (should coerce to 0.0)
            mv = call.MeterValues(
                connector_id=1,
                transaction_id=active_tx,
                meter_value=[
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "sampledValue": [
                            {
                                "value": "15000",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "location": "Outlet",
                                "context": "Sample.Periodic",
                            },
                            {
                                "value": "",
                                "measurand": "Power.Active.Import",
                                "unit": "W",
                                "context": "Sample.Periodic",
                            },
                        ],
                    }
                ],
            )
            resp = await cp.call(mv)
            assert resp is not None

            # meter_start restored from last EAIR on connector 1 -> 12.5 kWh; session = 15.0 - 12.5 = 2.5 kWh
            sess = float(cs.get_metric(cpid, "Energy.Session", connector_id=1))
            assert sess == pytest.approx(2.5, rel=1e-6)
            assert cs.get_unit(cpid, "Energy.Session", connector_id=1) == "kWh"

            # Empty-string coerced to 0.0
            pai = float(cs.get_metric(cpid, "Power.Active.Import", connector_id=1))
            assert pai == 0.0

            # Transaction id restored/kept
            tx_restored = int(cs.get_metric(cpid, "Transaction.Id", connector_id=1))
            assert tx_restored == active_tx

        finally:
            cp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cp_task
            await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9018, "cp_id": "CP_1_mv_restore", "cms": "cms_mv_restore"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_mv_restore"])
@pytest.mark.parametrize("port", [9018])
async def test_on_meter_values_restore_paths_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry, monkeypatch
):
    """Cover both restore branches in on_meter_values.

    - restored (meter_start) is not None
    - restored_tx (transaction_id) is not None
    Then verify SessionEnergy behavior.
    """
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        srv = cs.charge_points[cp_id]
        cpid = srv.settings.cpid

        # Ensure the metric slots look "missing" so both restore branches run.
        srv._metrics[(1, "Energy.Meter.Start")].value = None
        srv._metrics[(1, "Transaction.Id")].value = None

        # Patch get_ha_metric so both restores succeed.
        def fake_get_ha_metric(name: str, connector_id: int | None = None):
            if name == "Energy.Meter.Start" and connector_id == 1:
                return "12.5"  # kWh
            if name == "Transaction.Id" and connector_id == 1:
                return "123456"
            return None

        monkeypatch.setattr(srv, "get_ha_metric", fake_get_ha_metric, raising=True)

        # (1) Send a MeterValues WITHOUT transaction_id -> updates aggregate EAIR (conn 0)
        mv_no_tx = call.MeterValues(
            connector_id=1,
            meter_value=[
                {
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "sampledValue": [
                        {
                            "value": "15000",  # Wh -> 15.0 kWh
                            "measurand": "Energy.Active.Import.Register",
                            "unit": "Wh",
                            "location": "Inlet",
                            "context": "Sample.Clock",
                        }
                    ],
                }
            ],
        )
        resp = await cp.call(mv_no_tx)
        assert resp is not None

        # Verify both restore branches happened.
        assert srv._metrics[(1, "Energy.Meter.Start")].value == 12.5
        assert srv._metrics[(1, "Transaction.Id")].value == 123456
        assert srv._active_tx.get(1, 0) == 123456

        # Aggregate EAIR (connector 0) updated to 15.0 kWh with attrs.
        assert srv._metrics[(0, "Energy.Active.Import.Register")].value == 15.0
        assert srv._metrics[(0, "Energy.Active.Import.Register")].unit == "kWh"
        assert (
            srv._metrics[(0, "Energy.Active.Import.Register")].extra_attr.get(
                "location"
            )
            == "Inlet"
        )
        assert (
            srv._metrics[(0, "Energy.Active.Import.Register")].extra_attr.get("context")
            == "Sample.Clock"
        )

        # (2) Send a MeterValues WITH matching transaction_id and EAIR=16.0 kWh
        mv_with_tx = call.MeterValues(
            connector_id=1,
            transaction_id=123456,
            meter_value=[
                {
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "sampledValue": [
                        {
                            "value": "16.0",
                            "measurand": "Energy.Active.Import.Register",
                            "unit": "kWh",
                            "context": "Sample.Periodic",
                        }
                    ],
                }
            ],
        )
        resp2 = await cp.call(mv_with_tx)
        assert resp2 is not None

        # SessionEnergy = 16.0 − 12.5 = 3.5 kWh
        sess = float(cs.get_metric(cpid, "Energy.Session", connector_id=1))
        assert pytest.approx(sess, rel=1e-6) == 3.5
        assert cs.get_unit(cpid, "Energy.Session", connector_id=1) == "kWh"

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9013, "cp_id": "CP_1_extra", "cms": "cms_extra"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_extra"])
@pytest.mark.parametrize("port", [9013])
async def test_api_get_extra_attr_paths(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Exercise CentralSystem.get_extra_attr() without driving full post-connect.

    We connect briefly to ensure the CS has a server-side CP object, then we
    seed _metrics extra_attr directly and verify lookup order:
    - explicit connector_id returns that connector's attrs,
    - no connector_id prefers aggregate (conn 0),
    - if conn 0 is missing, fallback to conn 1 succeeds.
    """
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        # Start a minimal CP so CS creates/keeps the server-side object
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())
        # One Boot is enough to associate the CP id in CS
        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        # Grab server-side CP and seed metrics directly
        cp_srv = cs.charge_points[cp_id]
        cpid = cp_srv.settings.cpid

        meas = "Energy.Active.Import.Register"

        # Seed aggregate (connector 0) extra_attr
        cp_srv._metrics[(0, meas)].extra_attr = {
            "location": "Inlet",
            "context": "Sample.Clock",
        }

        # (A) No connector_id -> prefers aggregate (0)
        attrs = cs.get_extra_attr(cpid, measurand=meas)
        assert attrs == {"location": "Inlet", "context": "Sample.Clock"}

        # (B) Explicit connector 1 -> returns that connector's attrs
        cp_srv._metrics[(1, meas)].extra_attr = {"custom": "c1", "context": "Override"}
        attrs_c1 = cs.get_extra_attr(cpid, measurand=meas, connector_id=1)
        assert attrs_c1 == {"custom": "c1", "context": "Override"}

        # (C) Fallback order when aggregate is missing -> falls back to connector 1
        cp_srv._metrics[(0, meas)].extra_attr = None
        attrs_fallback = cs.get_extra_attr(cpid, measurand=meas)
        assert attrs_fallback == {"custom": "c1", "context": "Override"}

        # Clean up
        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9014, "cp_id": "CP_1_fw_ok", "cms": "cms_fw_ok"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_fw_ok"])
@pytest.mark.parametrize("port", [9014])
async def test_update_firmware_supported_valid_url_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry, caplog
):
    """FW supported + valid URL -> returns True and RPC is sent with correct payload."""
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        server_cp = cs.charge_points[cp_id]
        # Enable FW bit
        server_cp._attr_supported_features = (
            int(server_cp._attr_supported_features or 0) | prof.FW
        )

        url = "https://example.com/fw.bin"
        caplog.set_level(logging.INFO)

        ok = await server_cp.update_firmware(url, wait_time=0)
        assert ok is True

        # Assert the client actually received an UpdateFirmware call with expected data
        # retrieveDate format: YYYY-mm-ddTHH:MM:SSZ
        assert cp.last_update_firmware is not None
        assert cp.last_update_firmware.get("location") == url
        rd = cp.last_update_firmware.get("retrieve_date")
        assert isinstance(rd, str) and re.match(
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", rd
        )

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9015, "cp_id": "CP_1_fw_badurl", "cms": "cms_fw_badurl"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_fw_badurl"])
@pytest.mark.parametrize("port", [9015])
async def test_update_firmware_supported_invalid_url_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry, caplog
):
    """FW supported + invalid URL -> returns False and no RPC is sent."""
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        server_cp = cs.charge_points[cp_id]
        server_cp._attr_supported_features = (
            int(server_cp._attr_supported_features or 0) | prof.FW
        )

        bad_url = "not-a-valid-url"
        caplog.set_level(logging.WARNING)

        ok = await server_cp.update_firmware(bad_url, wait_time=1)
        assert ok is False
        # Should warn about invalid URL
        assert any("Failed to parse url" in rec.message for rec in caplog.records)
        # Client must not have received any UpdateFirmware
        assert getattr(cp, "last_update_firmware", None) is None

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9016, "cp_id": "CP_1_fw_nosupport", "cms": "cms_fw_nosupport"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_fw_nosupport"])
@pytest.mark.parametrize("port", [9016])
async def test_update_firmware_not_supported_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry, caplog
):
    """FW not supported -> returns False; no RPC."""
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        server_cp = cs.charge_points[cp_id]
        # Ensure FW bit is NOT set
        server_cp._attr_supported_features = (
            int(server_cp._attr_supported_features or 0) & ~prof.FW
        )

        caplog.set_level(logging.WARNING)
        ok = await server_cp.update_firmware("https://example.com/fw.bin", wait_time=0)
        assert ok is False
        assert any(
            "does not support OCPP firmware updating" in rec.message
            for rec in caplog.records
        )
        assert getattr(cp, "last_update_firmware", None) is None

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9017, "cp_id": "CP_1_fw_rpcfail", "cms": "cms_fw_rpcfail"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_fw_rpcfail"])
@pytest.mark.parametrize("port", [9017])
async def test_update_firmware_rpc_failure_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry, caplog, monkeypatch
):
    """FW supported but self.call raises -> returns False and logs error."""
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        server_cp = cs.charge_points[cp_id]
        server_cp._attr_supported_features = (
            int(server_cp._attr_supported_features or 0) | prof.FW
        )

        # Make the server-side call() fail
        async def boom(_req):
            raise RuntimeError("boom")

        monkeypatch.setattr(server_cp, "call", boom, raising=True)

        caplog.set_level(logging.ERROR)
        ok = await server_cp.update_firmware("https://example.com/fw.bin", wait_time=0)
        assert ok is False
        assert any("UpdateFirmware failed" in rec.message for rec in caplog.records)
        # No successful RPC reached the client
        assert getattr(cp, "last_update_firmware", None) is None

        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9020, "cp_id": "CP_1_unit_fallback", "cms": "cms_unit_fallback"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_unit_fallback"])
@pytest.mark.parametrize("port", [9020])
async def test_api_get_unit_fallback_to_later_connectors(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """get_unit() should fall back to connectors >=2 when (0) and (1) have no unit."""
    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        # IMPORTANT: advertise 3 connectors so the CS learns n_connectors >= 3
        cp = ChargePoint(f"{cp_id}_client", ws, no_connectors=3)
        cp_task = asyncio.create_task(cp.start())

        # Boot + wait for server-side post_connect to complete (fetches number_of_connectors)
        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        srv = cs.charge_points[cp_id]
        cpid = srv.settings.cpid

        meas = "Power.Active.Import"

        # Ensure no flat-key unit short-circuits the fallback
        if meas in srv._metrics:
            srv._metrics[meas].unit = None

        # Seed (0) and (1) with metrics but no unit…
        srv._metrics[(0, meas)] = srv._metrics.get((0, meas), M(0.0, None))
        srv._metrics[(0, meas)].unit = None
        srv._metrics[(1, meas)] = srv._metrics.get((1, meas), M(0.0, None))
        srv._metrics[(1, meas)].unit = None

        # …and (2) with a concrete unit the fallback should discover.
        srv._metrics[(2, meas)] = M(0.0, "kW")

        unit = cs.get_unit(cpid, measurand=meas)
        assert unit == "kW"

        # Cleanup
        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [
        {
            "port": 9019,
            "cp_id": "CP_1_extra_fallback",
            "cms": "cms_extra_fallback",
        }
    ],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_extra_fallback"])
@pytest.mark.parametrize("port", [9019])
async def test_api_get_extra_attr_fallback_to_later_connectors(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Ensure get_extra_attr() falls back.

    To connectors >=2 when (0), flat-key, (1) and (2) have no attrs (extra_attr=None), so connector 3 is returned.
    """

    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws, no_connectors=3)
        cp_task = asyncio.create_task(cp.start())

        # Boot + wait for server-side post_connect to complete (fetches number_of_connectors)
        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        srv = cs.charge_points[cp_id]
        cpid = srv.settings.cpid

        from custom_components.ocpp.chargepoint import Metric as M

        meas = "Energy.Active.Import.Register"

        # (1) Force early checks to return None (NOT {}):
        #     - Access the flat key via __getitem__ to create the exact object the API will read,
        #       then set its extra_attr to None.
        srv._metrics[(0, meas)] = M(0.0, None)
        srv._metrics[(0, meas)].extra_attr = None

        _flat = srv._metrics[meas]  # <-- pre-touch the flat key
        _flat.extra_attr = None  # <-- ensure it returns None, not {}

        srv._metrics[(1, meas)] = M(0.0, None)
        srv._metrics[(1, meas)].extra_attr = None

        srv._metrics[(2, meas)] = M(0.0, None)
        srv._metrics[(2, meas)].extra_attr = None

        # (2) Seed connector 3 with the only non-empty attrs.
        expected = {"source": "conn3", "context": "Sample.Clock"}
        srv._metrics[(3, meas)] = M(0.0, None)
        srv._metrics[(3, meas)].extra_attr = expected

        # (3) Now the API should fall through to connector 3.
        got = cs.get_extra_attr(cpid, measurand=meas)
        assert got == expected

        # Cleanup
        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


# @pytest.mark.skip(reason="skip")
@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9008, "cp_id": "CP_1_diag_dt", "cms": "cms_diag_dt"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_1_diag_dt"])
@pytest.mark.parametrize("port", [9008])
async def test_get_diagnostics_and_data_transfer_v16(
    hass, socket_enabled, cp_id, port, setup_config_entry, caplog
):
    """Ensure HA services trigger correct OCPP 1.6 calls with expected payload.

    including DataTransfer rejected path and get_diagnostics error/feature branches.
    """

    cs: CentralSystem = setup_config_entry

    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        cp_task = asyncio.create_task(cp.start())

        # Bring charger to ready state (boot + post_connect)
        await cp.send_boot_notification()
        await wait_ready(cs.charge_points[cp_id])

        # Resolve HA device id (cpid)
        cpid = cs.charge_points[cp_id].settings.cpid

        # --- get_diagnostics: happy path with valid URL ---
        upload_url = "https://example.test/diag"
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_get_diagnostics.value,
            service_data={"devid": cpid, "upload_url": upload_url},
            blocking=True,
        )

        # --- data_transfer: Accepted path ---
        vendor_id = "VendorX"
        message_id = "Msg42"
        payload = '{"hello":"world"}'
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_data_transfer.value,
            service_data={
                "devid": cpid,
                "vendor_id": vendor_id,
                "message_id": message_id,
                "data": payload,
            },
            blocking=True,
        )

        # Give event loop a tick to flush ws calls
        await asyncio.sleep(0.05)

        # Assert CP handlers received expected fields (as captured by the fake CP)
        assert cp.last_diag_location == upload_url
        assert cp.last_data_transfer == (vendor_id, message_id, payload)

        # --- data_transfer: Rejected path (flip cp.accept -> False) ---
        cp.accept = False
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_data_transfer.value,
            service_data={
                "devid": cpid,
                "vendor_id": "VendorX",
                "message_id": "MsgRejected",
                "data": "nope",
            },
            blocking=True,
        )
        await asyncio.sleep(0.05)

        # --- get_diagnostics: invalid URL triggers vol.MultipleInvalid warning ---
        caplog.clear()
        caplog.set_level(logging.WARNING)
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_get_diagnostics.value,
            service_data={"devid": cpid, "upload_url": "not-a-valid-url"},
            blocking=True,
        )
        assert any(
            "Failed to parse url" in rec.message for rec in caplog.records
        ), "Expected warning for invalid diagnostics upload_url not found"

        # --- get_diagnostics: FW profile NOT supported branch ---
        # Simulate that FirmwareManagement profile is not supported by the CP
        cpobj = cs.charge_points[cp_id]
        original_features = getattr(cpobj, "_attr_supported_features", None)

        # Try to blank out features regardless of type (set/list/tuple/int)
        try:
            tp = type(original_features)
            if isinstance(original_features, set | list | tuple):
                new_val = tp()  # empty same container type
            else:
                new_val = 0  # fall back to "no features"
            setattr(cpobj, "_attr_supported_features", new_val)
        except Exception:
            setattr(cpobj, "_attr_supported_features", 0)

        # Valid URL, but without FW support the handler should skip/return gracefully
        await hass.services.async_call(
            OCPP_DOMAIN,
            csvcs.service_get_diagnostics.value,
            service_data={"devid": cpid, "upload_url": "https://example.com/diag2"},
            blocking=True,
        )

        # Restore original features to avoid impacting other tests
        if original_features is not None:
            setattr(cpobj, "_attr_supported_features", original_features)

        # Cleanup
        cp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cp_task
        await ws.close()


class ChargePoint(cpclass):
    """Representation of real client Charge Point."""

    def __init__(self, id, connection, response_timeout=30, no_connectors=1):
        """Init extra variables for testing."""
        super().__init__(id, connection)
        self.no_connectors = int(no_connectors)
        self.active_transactionId: int = 0
        self.accept: bool = True
        self.task = None  # reused for background triggers
        self._tasks: set[asyncio.Task] = set()
        self.last_diag_location: str | None = None
        self.last_data_transfer: tuple[str | None, str | None, str | None] | None = None
        self.last_clear_profile_kwargs: dict | None = None
        self.last_update_firmware: dict | None = None

    @on(Action.get_configuration)
    def on_get_configuration(self, key, **kwargs):
        """Handle a get configuration requests."""
        if key[0] == ConfigurationKey.supported_feature_profiles.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {
                            "key": key[0],
                            "readonly": False,
                            "value": "Core,FirmwareManagement,LocalAuthListManagement,Reservation,SmartCharging,RemoteTrigger,Dummy",
                        }
                    ]
                )
            else:
                # use to test TypeError handling
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.heartbeat_interval.value:
            return call_result.GetConfiguration(
                configuration_key=[{"key": key[0], "readonly": False, "value": "300"}]
            )
        if key[0] == ConfigurationKey.number_of_connectors.value:
            return call_result.GetConfiguration(
                configuration_key=[
                    {"key": key[0], "readonly": False, "value": f"{self.no_connectors}"}
                ]
            )
        if key[0] == ConfigurationKey.web_socket_ping_interval.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "60"}
                    ]
                )
            else:
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.meter_values_sampled_data.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {
                            "key": key[0],
                            "readonly": False,
                            "value": "Energy.Active.Import.Register",
                        }
                    ]
                )
            else:
                pass
        if key[0] == ConfigurationKey.meter_value_sample_interval.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "60"}
                    ]
                )
            else:
                return call_result.GetConfiguration(
                    configuration_key=[{"key": key[0], "readonly": True, "value": "60"}]
                )
        if (
            key[0]
            == ConfigurationKey.charging_schedule_allowed_charging_rate_unit.value
        ):
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "Current"}
                    ]
                )
            else:
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.authorize_remote_tx_requests.value:
            if self.accept is True:
                return call_result.GetConfiguration(
                    configuration_key=[
                        {"key": key[0], "readonly": False, "value": "false"}
                    ]
                )
            else:
                return call_result.GetConfiguration(unknown_key=[key[0]])
        if key[0] == ConfigurationKey.charge_profile_max_stack_level.value:
            return call_result.GetConfiguration(
                configuration_key=[{"key": key[0], "readonly": False, "value": "3"}]
            )
        return call_result.GetConfiguration(
            configuration_key=[{"key": key[0], "readonly": False, "value": ""}]
        )

    @on(Action.change_configuration)
    def on_change_configuration(self, key, **kwargs):
        """Handle a get configuration request."""
        if self.accept is True:
            if key == ConfigurationKey.meter_values_sampled_data.value:
                return call_result.ChangeConfiguration(
                    ConfigurationStatus.reboot_required
                )
            else:
                return call_result.ChangeConfiguration(ConfigurationStatus.accepted)
        else:
            return call_result.ChangeConfiguration(ConfigurationStatus.rejected)

    @on(Action.change_availability)
    def on_change_availability(self, **kwargs):
        """Handle change availability request."""
        if self.accept is True:
            return call_result.ChangeAvailability(AvailabilityStatus.accepted)
        else:
            return call_result.ChangeAvailability(AvailabilityStatus.rejected)

    @on(Action.unlock_connector)
    def on_unlock_connector(self, **kwargs):
        """Handle unlock request."""
        if self.accept is True:
            return call_result.UnlockConnector(UnlockStatus.unlocked)
        else:
            return call_result.UnlockConnector(UnlockStatus.unlock_failed)

    @on(Action.reset)
    def on_reset(self, **kwargs):
        """Handle reset request."""
        if self.accept is True:
            return call_result.Reset(ResetStatus.accepted)
        else:
            return call_result.Reset(ResetStatus.rejected)

    @on(Action.remote_start_transaction)
    def on_remote_start_transaction(self, **kwargs):
        """Handle remote start request."""
        if self.accept is True:
            self.task = asyncio.create_task(self.send_start_transaction())
            return call_result.RemoteStartTransaction(RemoteStartStopStatus.accepted)
        else:
            return call_result.RemoteStopTransaction(RemoteStartStopStatus.rejected)

    @on(Action.remote_stop_transaction)
    def on_remote_stop_transaction(self, **kwargs):
        """Handle remote stop request."""
        if self.accept is True:
            return call_result.RemoteStopTransaction(RemoteStartStopStatus.accepted)
        else:
            return call_result.RemoteStopTransaction(RemoteStartStopStatus.rejected)

    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, **kwargs):
        """Handle set charging profile request."""
        if self.accept is True:
            return call_result.SetChargingProfile(ChargingProfileStatus.accepted)
        else:
            return call_result.SetChargingProfile(ChargingProfileStatus.rejected)

    @on(Action.clear_charging_profile)
    def on_clear_charging_profile(self, **kwargs):
        """Handle clear charging profile request."""
        # keep what was requested so the test can assert
        self.last_clear_profile_kwargs = dict(kwargs) if kwargs else {}
        if self.accept is True:
            return call_result.ClearChargingProfile(ClearChargingProfileStatus.accepted)
        else:
            return call_result.ClearChargingProfile(ClearChargingProfileStatus.unknown)

    @on(Action.trigger_message)
    def on_trigger_message(self, **kwargs):
        """Handle trigger message request."""
        if self.accept is True:
            return call_result.TriggerMessage(TriggerMessageStatus.accepted)
        else:
            return call_result.TriggerMessage(TriggerMessageStatus.rejected)

    @on(Action.update_firmware)
    def on_update_firmware(self, **kwargs):
        """Handle update firmware request."""
        self.last_update_firmware = dict(kwargs)
        return call_result.UpdateFirmware()

    @on(Action.get_diagnostics)
    def on_get_diagnostics(self, **kwargs):
        """Handle get diagnostics request."""
        # OCPP 1.6 GetDiagnostics request uses 'location'
        self.last_diag_location = kwargs.get("location")
        return call_result.GetDiagnostics()

    @on(Action.data_transfer)
    def on_data_transfer(self, **kwargs):
        """Handle get data transfer request."""
        # OCPP 1.6 DataTransfer request uses 'vendor_id', 'message_id', 'data'
        self.last_data_transfer = (
            kwargs.get("vendor_id"),
            kwargs.get("message_id"),
            kwargs.get("data"),
        )
        if self.accept is True:
            return call_result.DataTransfer(DataTransferStatus.accepted)
        else:
            return call_result.DataTransfer(DataTransferStatus.rejected)

    async def send_boot_notification(self):
        """Send a boot notification."""
        request = call.BootNotification(
            charge_point_model="Optimus", charge_point_vendor="The Mobility House"
        )
        resp = await self.call(request)
        assert resp.status == RegistrationStatus.accepted

    async def send_heartbeat(self):
        """Send a heartbeat."""
        request = call.Heartbeat()
        resp = await self.call(request)
        assert len(resp.current_time) > 0

    async def send_authorize(self):
        """Send an authorize request."""
        request = call.Authorize(id_tag="test_cp")
        resp = await self.call(request)
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted

    async def send_firmware_status(self):
        """Send a firmware status notification."""
        request = call.FirmwareStatusNotification(status=FirmwareStatus.downloaded)
        resp = await self.call(request)
        assert resp is not None

    async def send_diagnostics_status(self):
        """Send a diagnostics status notification."""
        request = call.DiagnosticsStatusNotification(status=DiagnosticsStatus.uploaded)
        resp = await self.call(request)
        assert resp is not None

    async def send_data_transfer(self):
        """Send a data transfer."""
        request = call.DataTransfer(
            vendor_id="The Mobility House",
            message_id="Test123",
            data="Test data transfer",
        )
        resp = await self.call(request)
        assert resp.status == DataTransferStatus.accepted

    async def send_start_transaction(self, meter_start: int = 12345):
        """Send a start transaction notification."""
        request = call.StartTransaction(
            connector_id=1,
            id_tag="test_cp",
            meter_start=meter_start,
            timestamp=datetime.now(tz=UTC).isoformat(),
        )
        resp = await self.call(request)
        self.active_transactionId = resp.transaction_id
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted.value

    async def send_status_notification(self):
        """Send a status notification."""
        request = call.StatusNotification(
            connector_id=0,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.suspended_ev,
            timestamp=datetime.now(tz=UTC).isoformat(),
            info="Test info",
            vendor_id="The Mobility House",
            vendor_error_code="Test error",
        )
        resp = await self.call(request)
        request = call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.charging,
            timestamp=datetime.now(tz=UTC).isoformat(),
            info="Test info",
            vendor_id="The Mobility House",
            vendor_error_code="Test error",
        )
        resp = await self.call(request)
        request = call.StatusNotification(
            connector_id=2,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.available,
            timestamp=datetime.now(tz=UTC).isoformat(),
            info="Test info",
            vendor_id="The Mobility House",
            vendor_error_code="Available",
        )
        resp = await self.call(request)

        assert resp is not None

    async def send_status_for_all_connectors(self):
        """Send StatusNotification for all connectors."""
        await self.send_status_notification()

    async def send_meter_periodic_data(self, connector_id: int = 1):
        """Send periodic meter data notification for a given connector."""
        n = 0
        while self.active_transactionId == 0 and n < 2:
            await asyncio.sleep(1)
            n += 1
        request = call.MeterValues(
            connector_id=connector_id,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "1305590.000",
                            "context": "Sample.Periodic",
                            "measurand": "Energy.Active.Import.Register",
                            "location": "Outlet",
                            "unit": "Wh",
                        },
                        {
                            "value": "20.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L1",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L2",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L3",
                        },
                        {
                            "value": "16.000",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Offered",
                            "location": "Outlet",
                            "unit": "A",
                        },
                        {
                            "value": "50.010",
                            "context": "Sample.Periodic",
                            "measurand": "Frequency",
                            "location": "Outlet",
                        },
                        {
                            "value": "",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "kW",
                        },
                        {
                            "value": "",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "W",
                            "phase": "L1",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "W",
                            "phase": "L2",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "W",
                            "phase": "L3",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Factor",
                            "location": "Outlet",
                        },
                        {
                            "value": "38.500",
                            "context": "Sample.Periodic",
                            "measurand": "Temperature",
                            "location": "Body",
                            "unit": "Celsius",
                        },
                        {
                            "value": "228.000",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L1-N",
                        },
                        {
                            "value": "228.000",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L2-N",
                        },
                        {
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L3-N",
                        },
                        {
                            "value": "89.00",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Reactive.Import",
                            "unit": "var",
                        },
                        {
                            "value": "0.010",
                            "context": "Transaction.Begin",
                            "unit": "kWh",
                        },
                        {
                            "value": "1305570.000",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_line_voltage(self, connector_id: int = 1):
        """Send line voltages for a given connector."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=connector_id,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "395.900",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L1-L2",
                        },
                        {
                            "value": "396.300",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L2-L3",
                        },
                        {
                            "value": "398.900",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L3-L1",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_err_phases(self, connector_id: int = 1):
        """Send erroneous voltage phase for a given connector."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=connector_id,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "230",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L1",
                        },
                        {
                            "value": "23",
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "location": "Outlet",
                            "unit": "A",
                            "phase": "L1-N",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_energy_kwh(self, connector_id: int = 1):
        """Send periodic energy meter value with kWh unit for a given connector."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=connector_id,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "unit": "kWh",
                            "value": "11",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_main_meter_clock_data(self, connector_id: int = 1):
        """Send periodic main meter value (no transaction_id) for a given connector."""
        while self.active_transactionId == 0:
            await asyncio.sleep(1)
        request = call.MeterValues(
            connector_id=connector_id,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "67230012",
                            "context": "Sample.Clock",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "location": "Inlet",
                        },
                    ],
                }
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_clock_data(self, connector_id: int = 1):
        """Send periodic meter data (clock) for a given connector."""
        self.active_transactionId = 0
        request = call.MeterValues(
            connector_id=connector_id,
            transaction_id=self.active_transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "measurand": "Voltage",
                            "context": "Sample.Clock",
                            "unit": "V",
                            "value": "228.490",
                        },
                        {
                            "measurand": "Power.Active.Import",
                            "context": "Sample.Clock",
                            "unit": "W",
                            "value": "0.000",
                        },
                        {
                            "measurand": "Energy.Active.Import.Register",
                            "context": "Sample.Clock",
                            "unit": "kWh",
                            "value": "1101.452",
                        },
                        {
                            "measurand": "Current.Import",
                            "context": "Sample.Clock",
                            "unit": "A",
                            "value": "0.054",
                        },
                        {
                            "measurand": "Frequency",
                            "context": "Sample.Clock",
                            "value": "50.000",
                        },
                    ],
                },
            ],
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_stop_transaction(self, delay: int = 0):
        """Send a stop transaction notification."""
        # add delay to allow meter data to be processed
        await asyncio.sleep(delay)
        n = 0
        while self.active_transactionId == 0 and n < 2:
            await asyncio.sleep(1)
            n += 1
        request = call.StopTransaction(
            meter_stop=54321,
            timestamp=datetime.now(tz=UTC).isoformat(),
            transaction_id=self.active_transactionId,
            reason="EVDisconnected",
            id_tag="test_cp",
        )
        resp = await self.call(request)
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted.value

    async def send_security_event(self):
        """Send a security event notification."""
        request = call.SecurityEventNotification(
            type="SettingSystemTime",
            timestamp="2022-09-29T20:58:29Z",
            tech_info="BootNotification",
        )
        await self.call(request)
