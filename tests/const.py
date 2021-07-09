"""Constants for ocpp tests."""
from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_METER_INTERVAL,
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
    str(Measurand.current_export): True,
    str(Measurand.current_import): True,
    str(Measurand.current_offered): True,
    str(Measurand.energy_active_export_register): True,
    str(Measurand.energy_active_import_register): True,
    str(Measurand.energy_reactive_export_register): True,
    str(Measurand.energy_reactive_import_register): True,
    str(Measurand.energy_active_export_interval): True,
    str(Measurand.energy_active_import_interval): True,
    str(Measurand.energy_reactive_export_interval): True,
    str(Measurand.energy_reactive_import_interval): True,
    str(Measurand.frequency): True,
    str(Measurand.power_active_export): True,
    str(Measurand.power_active_import): True,
    str(Measurand.power_factor): True,
    str(Measurand.power_offered): True,
    str(Measurand.power_reactive_export): True,
    str(Measurand.power_reactive_import): True,
    str(Measurand.rpm): True,
    str(Measurand.soc): True,
    str(Measurand.temperature): True,
    str(Measurand.voltage): True,
}
MOCK_CONFIG_DATA = {
    "host": "0.0.0.0",
    "port": 9000,
    "cpid": "test_cpid",
    "csid": "test_csid",
    "meter_interval": 60,
    "monitored_variables": "Measurand.current_export,Measurand.current_import,Measurand.current_offered,Measurand.energy_active_export_register,Measurand.energy_active_import_register,Measurand.energy_reactive_export_register,Measurand.energy_reactive_import_register,Measurand.energy_active_export_interval,Measurand.energy_active_import_interval,Measurand.energy_reactive_export_interval,Measurand.energy_reactive_import_interval,Measurand.frequency,Measurand.power_active_export,Measurand.power_active_import,Measurand.power_factor,Measurand.power_offered,Measurand.power_reactive_export,Measurand.power_reactive_import,Measurand.rpm,Measurand.soc,Measurand.temperature,Measurand.voltage",
}
DEFAULT_NAME = "test"
