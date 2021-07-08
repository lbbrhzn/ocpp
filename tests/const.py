"""Constants for ocpp tests."""
from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_METER_INTERVAL,
    CONF_PORT,
    MEASURANDS,
)

MOCK_CONFIG = {
    CONF_HOST: "0.0.0.0",
    CONF_PORT: 9000,
    CONF_CPID: "test_cpid",
    CONF_CSID: "test_csid",
    CONF_METER_INTERVAL: 60,
}
MOCK_CONFIG_2 = {MEASURANDS[1]: True}
DEFAULT_NAME = "test"
