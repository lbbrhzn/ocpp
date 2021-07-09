"""Constants for ocpp tests."""
import voluptuous as vol

from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_METER_INTERVAL,
    CONF_PORT,
    DEFAULT_MEASURAND,
    MEASURANDS,
)

MOCK_CONFIG = {
    CONF_HOST: "0.0.0.0",
    CONF_PORT: 9000,
    CONF_CPID: "test_cpid",
    CONF_CSID: "test_csid",
    CONF_METER_INTERVAL: 60,
}
MOCK_CONFIG_2 = vol.Schema(
    {
        vol.Required(m, default=(True if m == DEFAULT_MEASURAND else False)): bool
        for m in MEASURANDS
    }
)
DEFAULT_NAME = "test"
