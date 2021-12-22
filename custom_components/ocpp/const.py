"""Define constants for OCPP integration."""
import homeassistant.components.input_number as input_number
import homeassistant.const as ha

from ocpp.v16.enums import ChargePointStatus, Measurand, UnitOfMeasure

from .enums import HAChargerServices, HAChargerStatuses

CONF_CPI = "charge_point_identity"
CONF_CPID = "cpid"
CONF_CSID = "csid"
CONF_HOST = ha.CONF_HOST
CONF_ICON = ha.CONF_ICON
CONF_INITIAL = input_number.CONF_INITIAL
CONF_MAX = input_number.CONF_MAX
CONF_MIN = input_number.CONF_MIN
CONF_METER_INTERVAL = "meter_interval"
CONF_MODE = ha.CONF_MODE
CONF_MONITORED_VARIABLES = ha.CONF_MONITORED_VARIABLES
CONF_NAME = ha.CONF_NAME
CONF_PASSWORD = ha.CONF_PASSWORD
CONF_PORT = ha.CONF_PORT
CONF_STEP = input_number.CONF_STEP
CONF_SUBPROTOCOL = "subprotocol"
CONF_UNIT_OF_MEASUREMENT = ha.CONF_UNIT_OF_MEASUREMENT
CONF_USERNAME = ha.CONF_USERNAME
DEFAULT_CSID = "central"
DEFAULT_CPID = "charger"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9000
DEFAULT_SUBPROTOCOL = "ocpp1.6"
DEFAULT_METER_INTERVAL = 60
DOMAIN = "ocpp"
ICON = "mdi:ev-station"
MODE_SLIDER = input_number.MODE_SLIDER
MODE_BOX = input_number.MODE_BOX
SLEEP_TIME = 60

# Platforms
NUMBER = "number"
SENSOR = "sensor"
SWITCH = "switch"
BUTTON = "button"

PLATFORMS = [SENSOR, SWITCH, NUMBER, BUTTON]

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
# At a minimum define switch name and on service call,
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

SWITCHES = [SWITCH_CHARGE, SWITCH_AVAILABILITY]

# Input number definitions
NUMBER_MAX_CURRENT = {
    CONF_NAME: "Maximum_Current",
    CONF_ICON: ICON,
    CONF_MIN: 0,
    CONF_MAX: 32,
    CONF_STEP: 1,
    CONF_INITIAL: 32,
    CONF_MODE: MODE_SLIDER,
    CONF_UNIT_OF_MEASUREMENT: "A",
}
NUMBERS = [NUMBER_MAX_CURRENT]

# Where a HA unit does not exist use Ocpp unit
UNITS_OCCP_TO_HA = {
    UnitOfMeasure.wh: ha.ENERGY_WATT_HOUR,
    UnitOfMeasure.kwh: ha.ENERGY_KILO_WATT_HOUR,
    UnitOfMeasure.varh: UnitOfMeasure.varh,
    UnitOfMeasure.kvarh: UnitOfMeasure.kvarh,
    UnitOfMeasure.w: ha.POWER_WATT,
    UnitOfMeasure.kw: ha.POWER_KILO_WATT,
    UnitOfMeasure.va: ha.POWER_VOLT_AMPERE,
    UnitOfMeasure.kva: UnitOfMeasure.kva,
    UnitOfMeasure.var: UnitOfMeasure.var,
    UnitOfMeasure.kvar: UnitOfMeasure.kvar,
    UnitOfMeasure.a: ha.ELECTRIC_CURRENT_AMPERE,
    UnitOfMeasure.v: ha.ELECTRIC_POTENTIAL_VOLT,
    UnitOfMeasure.celsius: ha.TEMP_CELSIUS,
    UnitOfMeasure.fahrenheit: ha.TEMP_FAHRENHEIT,
    UnitOfMeasure.k: ha.TEMP_KELVIN,
    UnitOfMeasure.percent: ha.PERCENTAGE,
    UnitOfMeasure.hertz: ha.FREQUENCY_HERTZ,
}
