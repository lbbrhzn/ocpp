"""Define constants for OCPP integration."""
import homeassistant.const as ha

from ocpp.v16.enums import Measurand, UnitOfMeasure

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
PLATFORMS = [SENSOR, SWITCH]

# Ocpp SupportedFeatureProfiles
FEATURE_PROFILE_CORE = "Core"
FEATURE_PROFILE_FW = "FirmwareManagement"
FEATURE_PROFILE_SMART = "SmartCharging"
FEATURE_PROFILE_RESERV = "Reservation"
FEATURE_PROFILE_REMOTE = "RemoteTrigger"
FEATURE_PROFILE_AUTH = "LocalAuthListManagement"

# Services to register for use in HA
SERVICE_CHARGE_START = "start_transaction"
SERVICE_CHARGE_STOP = "stop_transaction"
SERVICE_AVAILABILITY = "availability"
SERVICE_SET_CHARGE_RATE = "set_charge_rate"
SERVICE_RESET = "reset"
SERVICE_UNLOCK = "unlock"

# Ocpp supported measurands
MEASURANDS = [
    Measurand.current_export.value,
    Measurand.current_import.value,
    Measurand.current_offered.value,
    Measurand.energy_active_export_register.value,
    Measurand.energy_active_import_register.value,
    Measurand.energy_reactive_export_register.value,
    Measurand.energy_reactive_import_register.value,
    Measurand.energy_active_export_interval.value,
    Measurand.energy_active_import_interval.value,
    Measurand.energy_reactive_export_interval.value,
    Measurand.energy_reactive_import_interval.value,
    Measurand.frequency.value,
    Measurand.power_active_export.value,
    Measurand.power_active_import.value,
    Measurand.power_factor.value,
    Measurand.power_offered.value,
    Measurand.power_reactive_export.value,
    Measurand.power_reactive_import.value,
    Measurand.rpm.value,
    Measurand.soc.value,
    Measurand.temperature.value,
    Measurand.voltage.value,
]
DEFAULT_MEASURAND = Measurand.energy_active_import_register.value
DEFAULT_MONITORED_VARIABLES = ",".join(MEASURANDS)
DEFAULT_ENERGY_UNIT = UnitOfMeasure.wh.value
DEFAULT_POWER_UNIT = UnitOfMeasure.w.value
HA_ENERGY_UNIT = UnitOfMeasure.kwh.value
HA_POWER_UNIT = UnitOfMeasure.kw.value

# Additional conditions/states to monitor
CONDITIONS = [
    "Status",
    "Heartbeat",
    "Error.Code",
    "Stop.Reason",
    "FW.Status",
    "Session.Time",  # in min
    "Session.Energy",  # in kWh
    "Meter.Start",  # in kWh
]

# Additional general information to report
GENERAL = [
    "ID",
    "Model",
    "Vendor",
    "Serial",
    "FW.Version",
    "Features",
    "Connectors",
    "Transaction.Id",
]
