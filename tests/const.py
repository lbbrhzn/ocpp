"""Constants for ocpp tests."""
from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_PORT,
)
from ocpp.v16.enums import Measurand

MOCK_CONFIG = {
    CONF_HOST: "0.0.0.0",
    CONF_PORT: 9000,
    CONF_CPID: "test_cpid",
    CONF_CSID: "test_csid",
    CONF_METER_INTERVAL: 60,
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
    CONF_HOST: "0.0.0.0",
    CONF_PORT: 9000,
    CONF_CPID: "test_cpid",
    CONF_CSID: "test_csid",
    CONF_METER_INTERVAL: 60,
    CONF_MONITORED_VARIABLES: "Current.Export,Current.Import,Current.Offered,Energy.Active.Export.Register,Energy.Active.Import.Register,Energy.Reactive.Export.Register,Energy.Reactive.Import.Register,Energy.Active.Export.Interval,Energy.Active.Import.Interval,Energy.Reactive.Export.Interval,Energy.Reactive.Import.Interval,Frequency,Power.Active.Export,Power.Active.Import,Power.Factor,Power.Offered,Power.Reactive.Export,Power.Reactive.Import,RPM,SoC,Temperature,Voltage",
}
DEFAULT_NAME = "test"
