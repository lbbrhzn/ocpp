"""Constants for ocpp tests."""
from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_PORT,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_SSL,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
)
from ocpp.v16.enums import Measurand

MOCK_CONFIG = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9000,
    CONF_CPID: "test_cpid",
    CONF_CSID: "test_csid",
    CONF_IDLE_INTERVAL: 900,
    CONF_MAX_CURRENT: 32,
    CONF_METER_INTERVAL: 60,
    CONF_SKIP_SCHEMA_VALIDATION: False,
    CONF_WEBSOCKET_CLOSE_TIMEOUT: 1,
    CONF_WEBSOCKET_PING_TRIES: 0,
    CONF_WEBSOCKET_PING_INTERVAL: 1,
    CONF_WEBSOCKET_PING_TIMEOUT: 1,
}
MOCK_CONFIG_2 = {
    Measurand.current_export.value: True,
    Measurand.current_import.value: True,
    Measurand.current_offered.value: True,
    Measurand.energy_active_export_register.value: True,
    Measurand.energy_active_import_register.value: True,
    Measurand.energy_reactive_export_register.value: True,
    Measurand.energy_reactive_import_register.value: True,
    Measurand.energy_active_export_interval.value: True,
    Measurand.energy_active_import_interval.value: True,
    Measurand.energy_reactive_export_interval.value: True,
    Measurand.energy_reactive_import_interval.value: True,
    Measurand.frequency.value: True,
    Measurand.power_active_export.value: True,
    Measurand.power_active_import.value: True,
    Measurand.power_factor.value: True,
    Measurand.power_offered.value: True,
    Measurand.power_reactive_export.value: True,
    Measurand.power_reactive_import.value: True,
    Measurand.rpm.value: True,
    Measurand.soc.value: True,
    Measurand.temperature.value: True,
    Measurand.voltage.value: True,
}
MOCK_CONFIG_DATA = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9000,
    CONF_CPID: "test_cpid",
    CONF_CSID: "test_csid",
    CONF_IDLE_INTERVAL: 900,
    CONF_MAX_CURRENT: 32,
    CONF_METER_INTERVAL: 60,
    CONF_MONITORED_VARIABLES: "Current.Export,Current.Import,Current.Offered,Energy.Active.Export.Register,Energy.Active.Import.Register,Energy.Reactive.Export.Register,Energy.Reactive.Import.Register,Energy.Active.Export.Interval,Energy.Active.Import.Interval,Energy.Reactive.Export.Interval,Energy.Reactive.Import.Interval,Frequency,Power.Active.Export,Power.Active.Import,Power.Factor,Power.Offered,Power.Reactive.Export,Power.Reactive.Import,RPM,SoC,Temperature,Voltage",
    CONF_SKIP_SCHEMA_VALIDATION: False,
    CONF_SSL: False,
    CONF_WEBSOCKET_CLOSE_TIMEOUT: 1,
    CONF_WEBSOCKET_PING_TRIES: 0,
    CONF_WEBSOCKET_PING_INTERVAL: 1,
    CONF_WEBSOCKET_PING_TIMEOUT: 1,
}

# configuration with skip schema validation enabled
MOCK_CONFIG_DATA_2 = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9002,
    CONF_CPID: "test_cpid_2",
    CONF_CSID: "test_csid",
    CONF_IDLE_INTERVAL: 900,
    CONF_MAX_CURRENT: 32,
    CONF_METER_INTERVAL: 60,
    CONF_MONITORED_VARIABLES: "Current.Export,Current.Import,Current.Offered,Energy.Active.Export.Register,Energy.Active.Import.Register,Energy.Reactive.Export.Register,Energy.Reactive.Import.Register,Energy.Active.Export.Interval,Energy.Active.Import.Interval,Energy.Reactive.Export.Interval,Energy.Reactive.Import.Interval,Frequency,Power.Active.Export,Power.Active.Import,Power.Factor,Power.Offered,Power.Reactive.Export,Power.Reactive.Import,RPM,SoC,Temperature,Voltage",
    CONF_SKIP_SCHEMA_VALIDATION: True,
    CONF_SSL: False,
    CONF_WEBSOCKET_CLOSE_TIMEOUT: 1,
    CONF_WEBSOCKET_PING_TRIES: 0,
    CONF_WEBSOCKET_PING_INTERVAL: 1,
    CONF_WEBSOCKET_PING_TIMEOUT: 1,
}

# separate entry for switch so tests can run concurrently
MOCK_CONFIG_SWITCH = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9001,
    CONF_CPID: "test_cpid_2",
    CONF_CSID: "test_csid_2",
    CONF_MAX_CURRENT: 32,
    CONF_IDLE_INTERVAL: 900,
    CONF_METER_INTERVAL: 60,
    CONF_MONITORED_VARIABLES: "Current.Export,Current.Import,Current.Offered,Energy.Active.Export.Register,Energy.Active.Import.Register,Energy.Reactive.Export.Register,Energy.Reactive.Import.Register,Energy.Active.Export.Interval,Energy.Active.Import.Interval,Energy.Reactive.Export.Interval,Energy.Reactive.Import.Interval,Frequency,Power.Active.Export,Power.Active.Import,Power.Factor,Power.Offered,Power.Reactive.Export,Power.Reactive.Import,RPM,SoC,Temperature,Voltage",
}
DEFAULT_NAME = "test"
