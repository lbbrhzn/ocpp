"""Implement a test by a simulating a chargepoint."""
import asyncio
from datetime import datetime, timezone  # timedelta,

from pytest_homeassistant_custom_component.common import MockConfigEntry
import websockets

from custom_components.ocpp import async_setup_entry, async_unload_entry
from custom_components.ocpp.const import DOMAIN
from custom_components.ocpp.enums import ConfigurationKey
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
    FirmwareStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    TriggerMessageStatus,
    UnlockStatus,
)

from .const import MOCK_CONFIG_DATA


async def test_cms_responses(hass):
    """Test central system responses to a charger."""
    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG_DATA, entry_id="test_cms"
    )
    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()

    cs = hass.data[DOMAIN][config_entry.entry_id]

    async with websockets.connect(
        "ws://localhost:9000/CP_1",
        subprotocols=["ocpp1.6"],
    ) as ws:
        # use a different id for debugging
        cp = ChargePoint("CP_1_test", ws)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    cp.start(),
                    cp.send_boot_notification(),
                    cp.send_authorize(),
                    cp.send_heartbeat(),
                    cp.send_status_notification(),
                    cp.send_firmware_status(),
                    cp.send_data_transfer(),
                    cp.send_start_transaction(),
                    cp.send_meter_data(),
                    cp.send_stop_transaction(),
                    cs.charge_points["test_cpid"].start_transaction(),
                    cs.charge_points["test_cpid"].reset(),
                    cs.charge_points["test_cpid"].set_charge_rate(),
                    cs.charge_points["test_cpid"].clear_profile(),
                    cs.charge_points["test_cpid"].update_firmware(
                        "http://www.charger.com/file.bin"
                    ),
                    cs.charge_points["test_cpid"].unlock(),
                ),
                timeout=7,
            )
        except asyncio.TimeoutError:
            pass
        assert int(cs.get_metric("test_cpid", "Energy.Active.Import.Register")) == int(
            1305570 / 1000
        )
        assert cs.get_unit("test_cpid", "Energy.Active.Import.Register") == "kWh"
    await async_unload_entry(hass, config_entry)
    await hass.async_block_till_done()


class ChargePoint(cpclass):
    """Representation of real client Charge Point."""

    def __init__(self, id, connection, response_timeout=30):
        """Init extra variables for testing."""
        super().__init__(id, connection)
        self._transactionId = 0

    @on(Action.GetConfiguration)
    def on_get_configuration(self, key, **kwargs):
        """Handle a get configuration requests."""
        if key[0] == ConfigurationKey.supported_feature_profiles.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[
                    {
                        "key": key[0],
                        "readonly": False,
                        "value": "Core,FirmwareManagement,RemoteTrigger,SmartCharging",
                    }
                ]
            )
        if key[0] == ConfigurationKey.heartbeat_interval.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[{"key": key[0], "readonly": False, "value": "300"}]
            )
        if key[0] == ConfigurationKey.number_of_connectors.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[{"key": key[0], "readonly": False, "value": "1"}]
            )
        if key[0] == ConfigurationKey.web_socket_ping_interval.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[{"key": key[0], "readonly": False, "value": "60"}]
            )
        if key[0] == ConfigurationKey.meter_values_sampled_data.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[
                    {
                        "key": key[0],
                        "readonly": False,
                        "value": "Energy.Active.Import.Register",
                    }
                ]
            )
        if key[0] == ConfigurationKey.meter_value_sample_interval.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[{"key": key[0], "readonly": False, "value": "60"}]
            )
        if (
            key[0]
            == ConfigurationKey.charging_schedule_allowed_charging_rate_unit.value
        ):
            return call_result.GetConfigurationPayload(
                configuration_key=[
                    {"key": key[0], "readonly": False, "value": "Current"}
                ]
            )
        if key[0] == ConfigurationKey.authorize_remote_tx_requests.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[{"key": key[0], "readonly": False, "value": "false"}]
            )
        if key[0] == ConfigurationKey.charge_profile_max_stack_level.value:
            return call_result.GetConfigurationPayload(
                configuration_key=[{"key": key[0], "readonly": False, "value": "3"}]
            )
        return call_result.GetConfigurationPayload(
            configuration_key=[{"key": key[0], "readonly": False, "value": ""}]
        )

    @on(Action.ChangeConfiguration)
    def on_change_configuration(self, **kwargs):
        """Handle a get configuration request."""
        return call_result.ChangeConfigurationPayload(ConfigurationStatus.accepted)

    @on(Action.ChangeAvailability)
    def on_change_availability(self, **kwargs):
        """Handle change availability request."""
        return call_result.ChangeAvailabilityPayload(AvailabilityStatus.accepted)

    @on(Action.UnlockConnector)
    def on_unlock_connector(self, **kwargs):
        """Handle unlock request."""
        return call_result.UnlockConnectorPayload(UnlockStatus.unlocked)

    @on(Action.Reset)
    def on_reset(self, **kwargs):
        """Handle change availability request."""
        return call_result.ResetPayload(ResetStatus.accepted)

    @on(Action.RemoteStartTransaction)
    def on_remote_start_transaction(self, **kwargs):
        """Handle remote start request."""
        return call_result.RemoteStartTransactionPayload(RemoteStartStopStatus.accepted)

    @on(Action.SetChargingProfile)
    def on_set_charging_profile(self, **kwargs):
        """Handle set charging profile request."""
        return call_result.SetChargingProfilePayload(ChargingProfileStatus.accepted)

    @on(Action.ClearChargingProfile)
    def on_clear_charging_profile(self, **kwargs):
        """Handle clear charging profile request."""
        return call_result.ClearChargingProfilePayload(
            ClearChargingProfileStatus.accepted
        )

    @on(Action.TriggerMessage)
    def on_trigger_message(self, **kwargs):
        """Handle trigger message request."""
        return call_result.TriggerMessagePayload(TriggerMessageStatus.accepted)

    @on(Action.UpdateFirmware)
    def on_update_firmware(self, **kwargs):
        """Handle update firmware request."""
        return call_result.UpdateFirmwarePayload()

    async def send_boot_notification(self):
        """Send a boot notification."""
        request = call.BootNotificationPayload(
            charge_point_model="Optimus", charge_point_vendor="The Mobility House"
        )
        resp = await self.call(request)
        assert resp.status == RegistrationStatus.accepted

    async def send_heartbeat(self):
        """Send a heartbeat."""
        request = call.HeartbeatPayload()
        resp = await self.call(request)
        assert len(resp.current_time) > 0

    async def send_authorize(self):
        """Send an authorize request."""
        request = call.AuthorizePayload(id_tag="test_cp")
        resp = await self.call(request)
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted

    async def send_firmware_status(self):
        """Send a firmware status notification."""
        request = call.FirmwareStatusNotificationPayload(
            status=FirmwareStatus.downloaded
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_data_transfer(self):
        """Send a data transfer."""
        request = call.DataTransferPayload(
            vendor_id="The Mobility House",
            message_id="Test123",
            data="Test data transfer",
        )
        resp = await self.call(request)
        assert resp.status == DataTransferStatus.accepted

    async def send_start_transaction(self):
        """Send a start transaction notification."""
        request = call.StartTransactionPayload(
            connector_id=1,
            id_tag="test_cp",
            meter_start=12345,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
        )
        resp = await self.call(request)
        self._transactionId = resp.transaction_id
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted.value

    async def send_status_notification(self):
        """Send a status notification."""
        request = call.StatusNotificationPayload(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.charging,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            info="Test info",
            vendor_id="The Mobility House",
            vendor_error_code="Test error",
        )
        resp = await self.call(request)
        assert resp is not None

    async def send_meter_data(self):
        """Send meter data notification."""
        request = call.MeterValuesPayload(
            connector_id=1,
            transaction_id=self._transactionId,
            meter_value=[
                {
                    "timestamp": "2021-06-21T16:15:09Z",
                    "sampledValue": [
                        {
                            "value": "1305570.000",
                            "context": "Sample.Periodic",
                            "measurand": "Energy.Active.Import.Register",
                            "location": "Outlet",
                            "unit": "Wh",
                        },
                        {
                            "value": "0.000",
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
                            "value": "0.000",
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "location": "Outlet",
                            "unit": "W",
                        },
                        {
                            "value": "0.000",
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
                            "value": "227.000",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L2-N",
                        },
                        {
                            "value": "229.300",
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "location": "Outlet",
                            "unit": "V",
                            "phase": "L3-N",
                        },
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

    async def send_stop_transaction(self):
        """Send a stop transaction notification."""
        request = call.StopTransactionPayload(
            meter_stop=54321,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            transaction_id=self._transactionId,
            reason="EVDisconnected",
            id_tag="test_cp",
        )
        resp = await self.call(request)
        assert resp.id_tag_info["status"] == AuthorizationStatus.accepted.value
