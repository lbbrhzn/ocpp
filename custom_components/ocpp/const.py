"""Define constants for OCPP integration."""

import pathlib

from dataclasses import dataclass, field
import homeassistant.components.input_number as input_number
from homeassistant.components.sensor import SensorDeviceClass
import homeassistant.const as ha
from ocpp.v16.enums import Measurand, UnitOfMeasure

CONF_AUTH_LIST = "authorization_list"
CONF_AUTH_STATUS = "authorization_status"
CONF_CPI = "charge_point_identity"
CONF_CPID = "cpid"
CONF_CPIDS = "cpids"
CONF_CSID = "csid"
CONF_DEFAULT_AUTH_STATUS = "default_authorization_status"
CONF_HOST = ha.CONF_HOST
CONF_ID_TAG = "id_tag"
CONF_ICON = ha.CONF_ICON
CONF_IDLE_INTERVAL = "idle_interval"
CONF_MAX_CURRENT = "max_current"
CONF_METER_INTERVAL = "meter_interval"
CONF_MODE = ha.CONF_MODE
CONF_MONITORED_VARIABLES = ha.CONF_MONITORED_VARIABLES
CONF_MONITORED_VARIABLES_AUTOCONFIG = "monitored_variables_autoconfig"
CONF_NAME = ha.CONF_NAME
CONF_NUM_CONNECTORS = "num_connectors"
CONF_PASSWORD = ha.CONF_PASSWORD
CONF_PORT = ha.CONF_PORT
CONF_SKIP_SCHEMA_VALIDATION = "skip_schema_validation"
CONF_FORCE_SMART_CHARGING = "force_smart_charging"
CONF_SSL = "ssl"
CONF_SSL_CERTFILE_PATH = "ssl_certfile_path"
CONF_SSL_KEYFILE_PATH = "ssl_keyfile_path"
CONF_STEP = input_number.CONF_STEP
CONF_SUBPROTOCOL = "subprotocol"
CONF_UNIT_OF_MEASUREMENT = ha.CONF_UNIT_OF_MEASUREMENT
CONF_USERNAME = ha.CONF_USERNAME
CONF_WEBSOCKET_CLOSE_TIMEOUT = "websocket_close_timeout"
CONF_WEBSOCKET_PING_TRIES = "websocket_ping_tries"
CONF_WEBSOCKET_PING_INTERVAL = "websocket_ping_interval"
CONF_WEBSOCKET_PING_TIMEOUT = "websocket_ping_timeout"
DATA_UPDATED = "ocpp_data_updated"
DEFAULT_CSID = "central"
DEFAULT_CPID = "charger"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_MAX_CURRENT = 32
DEFAULT_NUM_CONNECTORS = 1
DEFAULT_PORT = 9000
DEFAULT_SKIP_SCHEMA_VALIDATION = False
DEFAULT_FORCE_SMART_CHARGING = False
DEFAULT_SSL = False
DEFAULT_SSL_CERTFILE_PATH = pathlib.Path.cwd().joinpath("fullchain.pem")
DEFAULT_SSL_KEYFILE_PATH = pathlib.Path.cwd().joinpath("privkey.pem")
DEFAULT_SUBPROTOCOLS = ["ocpp1.6", "ocpp2.0.1", "ocpp2.1"]
OCPP_2_0 = "ocpp2"
DEFAULT_METER_INTERVAL = 60
DEFAULT_IDLE_INTERVAL = 900
DEFAULT_WEBSOCKET_CLOSE_TIMEOUT = 10
DEFAULT_WEBSOCKET_PING_TRIES = 2
DEFAULT_WEBSOCKET_PING_INTERVAL = 20
DEFAULT_WEBSOCKET_PING_TIMEOUT = 20
DOMAIN = "ocpp"
CONFIG = "config"
ICON = "mdi:ev-station"
SLEEP_TIME = 60

# Platforms
NUMBER = "number"
SENSOR = "sensor"
SWITCH = "switch"
BUTTON = "button"

PLATFORMS = [SENSOR, SWITCH, NUMBER, BUTTON]

# Ocpp supported measurands
MEASURANDS = [
    Measurand.current_export.value,
    Measurand.current_import.value,
    Measurand.current_offered.value,
    Measurand.energy_active_export_interval.value,
    Measurand.energy_active_export_register.value,
    Measurand.energy_active_import_interval.value,
    Measurand.energy_active_import_register.value,
    Measurand.energy_reactive_export_interval.value,
    Measurand.energy_reactive_export_register.value,
    Measurand.energy_reactive_import_interval.value,
    Measurand.energy_reactive_import_register.value,
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
DEFAULT_MONITORED_VARIABLES_AUTOCONFIG = True
DEFAULT_ENERGY_UNIT = UnitOfMeasure.wh.value
DEFAULT_POWER_UNIT = UnitOfMeasure.w.value
HA_ENERGY_UNIT = UnitOfMeasure.kwh.value
HA_POWER_UNIT = UnitOfMeasure.kw.value

# Where a HA unit does not exist use Ocpp unit
UNITS_OCCP_TO_HA = {
    UnitOfMeasure.wh: ha.UnitOfEnergy.WATT_HOUR,
    UnitOfMeasure.kwh: ha.UnitOfEnergy.KILO_WATT_HOUR,
    UnitOfMeasure.varh: UnitOfMeasure.varh,
    UnitOfMeasure.kvarh: UnitOfMeasure.kvarh,
    UnitOfMeasure.w: ha.UnitOfPower.WATT,
    UnitOfMeasure.kw: ha.UnitOfPower.KILO_WATT,
    UnitOfMeasure.va: ha.UnitOfApparentPower.VOLT_AMPERE,
    UnitOfMeasure.kva: UnitOfMeasure.kva,
    UnitOfMeasure.var: ha.UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
    UnitOfMeasure.kvar: UnitOfMeasure.kvar,
    UnitOfMeasure.a: ha.UnitOfElectricCurrent.AMPERE,
    UnitOfMeasure.v: ha.UnitOfElectricPotential.VOLT,
    UnitOfMeasure.celsius: ha.UnitOfTemperature.CELSIUS,
    UnitOfMeasure.fahrenheit: ha.UnitOfTemperature.FAHRENHEIT,
    UnitOfMeasure.k: ha.UnitOfTemperature.KELVIN,
    UnitOfMeasure.percent: ha.PERCENTAGE,
}

# Where an occp unit is not reported and only one possibility assign HA unit on device class
DEFAULT_CLASS_UNITS_HA = {
    SensorDeviceClass.CURRENT: ha.UnitOfElectricCurrent.AMPERE,
    SensorDeviceClass.VOLTAGE: ha.UnitOfElectricPotential.VOLT,
    SensorDeviceClass.FREQUENCY: ha.UnitOfFrequency.HERTZ,
    SensorDeviceClass.BATTERY: ha.PERCENTAGE,
    SensorDeviceClass.POWER: ha.UnitOfPower.KILO_WATT,
    SensorDeviceClass.REACTIVE_POWER: ha.UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
    SensorDeviceClass.ENERGY: ha.UnitOfEnergy.KILO_WATT_HOUR,
    SensorDeviceClass.TEMPERATURE: ha.UnitOfTemperature.CELSIUS,
}


@dataclass
class ChargerSystemSettings:
    """CentralSystem configuration passed to a ChargePoint."""

    cpid: str
    max_current: int
    idle_interval: int
    meter_interval: int
    monitored_variables: str
    monitored_variables_autoconfig: bool
    skip_schema_validation: bool
    force_smart_charging: bool
    connection: int | None = None  # number of this connection in central server
    num_connectors: int = DEFAULT_NUM_CONNECTORS


@dataclass
class CentralSystemSettings:
    """CentralSystem configuration values."""

    csid: str
    host: str
    port: str
    ssl: bool
    ssl_certfile_path: str
    ssl_keyfile_path: str
    websocket_close_timeout: int
    websocket_ping_interval: int
    websocket_ping_timeout: int
    websocket_ping_tries: int
    cpids: list = field(default_factory=list)  # holds cpid config flow settings
    subprotocols: list = field(default_factory=lambda: DEFAULT_SUBPROTOCOLS)

    # def __post_init__(self):
    #     i = 0
    #     for id in self.cpids:
    #        self.cpids[i] = ChargerSystemSettings(**id)
    #        i =+ 1
