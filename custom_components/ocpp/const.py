"""Define constants for OCPP integration."""
import homeassistant.const as ha

from ocpp.v16.enums import UnitOfMeasure

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
    "Current.Export",
    "Current.Import",
    "Current.Offered",
    "Energy.Active.Export.Register",
    "Energy.Active.Import.Register",
    "Energy.Reactive.Export.Register",
    "Energy.Reactive.Import.Register",
    "Energy.Active.Export.Interval",
    "Energy.Active.Import.Interval",
    "Energy.Reactive.Export.Interval",
    "Energy.Reactive.Import.Interval",
    "Frequency",
    "Power.Active.Export",
    "Power.Active.Import",
    "Power.Factor",
    "Power.Offered",
    "Power.Reactive.Export",
    "Power.Reactive.Import",
    "RPM",
    "SoC",
    "Temperature",
    "Voltage",
]
DEFAULT_MEASURAND = "Energy.Active.Import.Register"
DEFAULT_MONITORED_VARIABLES = ",".join(MEASURANDS)
DEFAULT_ENERGY_UNIT = UnitOfMeasure.wh
DEFAULT_POWER_UNIT = UnitOfMeasure.w
HA_ENERGY_UNIT = UnitOfMeasure.kwh
HA_POWER_UNIT = UnitOfMeasure.kw

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
