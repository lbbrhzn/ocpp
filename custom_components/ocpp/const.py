"""Define constants for OCPP integration."""
import homeassistant.const as ha

from ocpp.v16.enums import ChargePointStatus, Measurand, UnitOfMeasure

from .enums import HAChargerServices, HAChargerStatuses

DOMAIN = "ocpp"
CONF_METER_INTERVAL = "meter_interval"
CONF_USERNAME = ha.CONF_USERNAME
CONF_PASSWORD = ha.CONF_PASSWORD
CONF_HOST = ha.CONF_HOST
CONF_MONITORED_VARIABLES = ha.CONF_MONITORED_VARIABLES
CONF_NAME = ha.CONF_NAME
CONF_CPID = "cpid"
CONF_CSID = "csid"
CONF_PORT = ha.CONF_PORT
CONF_SUBPROTOCOL = "subprotocol"
CONF_CPI = "charge_point_identity"
DEFAULT_CSID = "central"
DEFAULT_CPID = "charger"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9000
DEFAULT_SUBPROTOCOL = "ocpp1.6"
DEFAULT_METER_INTERVAL = 60

ICON = "mdi:ev-station"
SLEEP_TIME = 60

# Platforms
BINARY_SENSOR = "binary_sensor"
SENSOR = "sensor"
SWITCH = "switch"
SLIDER = "slider"
PLATFORMS = [SENSOR, SWITCH, SLIDER]

# Ocpp supported measurands
MEASURANDS = [
    Measurand.energy_active_import_register.value,
    Measurand.energy_reactive_import_register.value,
    Measurand.energy_active_import_interval.value,
    Measurand.energy_reactive_import_interval.value,
    Measurand.power_active_import.value,
    Measurand.power_reactive_import.value,
    Measurand.power_offered.value,
    Measurand.power_factor.value,
    Measurand.current_import.value,
    Measurand.current_offered.value,
    Measurand.voltage.value,
    Measurand.frequency.value,
    Measurand.rpm.value,
    Measurand.soc.value,
    Measurand.temperature.value,
    Measurand.current_export.value,
    Measurand.energy_active_export_register.value,
    Measurand.energy_reactive_export_register.value,
    Measurand.energy_active_export_interval.value,
    Measurand.energy_reactive_export_interval.value,
    Measurand.power_active_export.value,
    Measurand.power_reactive_export.value,
]
DEFAULT_MEASURAND = Measurand.energy_active_import_register.value
DEFAULT_MONITORED_VARIABLES = ",".join(MEASURANDS)
DEFAULT_ENERGY_UNIT = UnitOfMeasure.wh.value
DEFAULT_POWER_UNIT = UnitOfMeasure.w.value
HA_ENERGY_UNIT = UnitOfMeasure.kwh.value
HA_POWER_UNIT = UnitOfMeasure.kw.value

# Switch configuration definitions
# At a minimum define switch name and on service call, pulse used to call a service once such as reset
# metric and condition combination can be used to drive switch state, use default to set initial state to True
SWITCH_CHARGE = {
    "name": "Charge_Control",
    "on": HAChargerServices.service_charge_start.name,
    "off": HAChargerServices.service_charge_stop.name,
    "metric": HAChargerStatuses.status.value,
    "condition": ChargePointStatus.charging.value,
}
SWITCH_AVAILABILITY = {
    "name": "Availability",
    "on": HAChargerServices.service_availability.name,
    "off": HAChargerServices.service_availability.name,
    "default": True,
    "metric": HAChargerStatuses.status.value,
    "condition": ChargePointStatus.available.value,
}
SWITCH_RESET = {
    "name": "Reset",
    "on": HAChargerServices.service_reset.name,
    "pulse": True,
}
SWITCH_UNLOCK = {
    "name": "Unlock",
    "on": HAChargerServices.service_unlock.name,
    "pulse": True,
}
SWITCHES = [SWITCH_CHARGE, SWITCH_RESET, SWITCH_UNLOCK, SWITCH_AVAILABILITY]
