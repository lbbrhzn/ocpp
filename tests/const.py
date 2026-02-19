"""Constants for ocpp tests."""

from custom_components.ocpp.const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_CSID,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    DEFAULT_MONITORED_VARIABLES,
)

MOCK_CONFIG_CS = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9005,
    CONF_SSL: False,
    CONF_SSL_CERTFILE_PATH: "/tests/fullchain.pem",
    CONF_SSL_KEYFILE_PATH: "/tests/privkey.pem",
    CONF_CSID: "test_csid_flow",
    CONF_WEBSOCKET_CLOSE_TIMEOUT: 1,
    CONF_WEBSOCKET_PING_TRIES: 0,
    CONF_WEBSOCKET_PING_INTERVAL: 1,
    CONF_WEBSOCKET_PING_TIMEOUT: 1,
    CONF_CPIDS: [],
}

MOCK_CONFIG_CP = {
    CONF_CPID: "test_cpid",
    CONF_IDLE_INTERVAL: 900,
    CONF_MAX_CURRENT: 32,
    CONF_METER_INTERVAL: 60,
    CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
    CONF_SKIP_SCHEMA_VALIDATION: False,
    CONF_FORCE_SMART_CHARGING: True,
}

MOCK_CONFIG_FLOW = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9005,
    CONF_CSID: "test_csid_flow",
    CONF_SSL: False,
    CONF_SSL_CERTFILE_PATH: "/tests/fullchain.pem",
    CONF_SSL_KEYFILE_PATH: "/tests/privkey.pem",
    CONF_WEBSOCKET_CLOSE_TIMEOUT: 1,
    CONF_WEBSOCKET_PING_TRIES: 0,
    CONF_WEBSOCKET_PING_INTERVAL: 1,
    CONF_WEBSOCKET_PING_TIMEOUT: 1,
    CONF_CPIDS: [
        {
            "test_cp_id": {
                CONF_CPID: "test_cpid",
                CONF_IDLE_INTERVAL: 900,
                CONF_MAX_CURRENT: 32,
                CONF_METER_INTERVAL: 60,
                CONF_MONITORED_VARIABLES: DEFAULT_MONITORED_VARIABLES,
                CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
                CONF_SKIP_SCHEMA_VALIDATION: False,
                CONF_FORCE_SMART_CHARGING: True,
            }
        },
    ],
}

# test_cpid configuration with skip schema validation enabled, and auto config false
MOCK_CONFIG_DATA = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9000,
    CONF_CSID: "test_csid",
    CONF_SSL: False,
    CONF_SSL_CERTFILE_PATH: "/tests/fullchain.pem",
    CONF_SSL_KEYFILE_PATH: "/tests/privkey.pem",
    CONF_WEBSOCKET_CLOSE_TIMEOUT: 1,
    CONF_WEBSOCKET_PING_TRIES: 0,
    CONF_WEBSOCKET_PING_INTERVAL: 1,
    CONF_WEBSOCKET_PING_TIMEOUT: 1,
    CONF_CPIDS: [],
}

# Mock a charger that can be appended to config data
MOCK_CONFIG_CP_APPEND = {
    CONF_CPID: "test_cpid",
    CONF_IDLE_INTERVAL: 900,
    CONF_MAX_CURRENT: 32,
    CONF_METER_INTERVAL: 60,
    CONF_MONITORED_VARIABLES: DEFAULT_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
    CONF_SKIP_SCHEMA_VALIDATION: False,
    CONF_FORCE_SMART_CHARGING: True,
}

# different port with skip schema validation enabled, and auto config false
MOCK_CONFIG_DATA_1 = {
    **MOCK_CONFIG_DATA,
    CONF_CSID: "test_csid_1",
    CONF_PORT: 9001,
    CONF_CPIDS: [
        {
            "CP_1_nosub": {
                CONF_CPID: "test_cpid_9001",
                CONF_IDLE_INTERVAL: 900,
                CONF_MAX_CURRENT: 32,
                CONF_METER_INTERVAL: 60,
                CONF_MONITORED_VARIABLES: DEFAULT_MONITORED_VARIABLES,
                CONF_MONITORED_VARIABLES_AUTOCONFIG: False,
                CONF_NUM_CONNECTORS: 2,
                CONF_SKIP_SCHEMA_VALIDATION: True,
                CONF_FORCE_SMART_CHARGING: True,
            }
        },
    ],
}

# different port with skip schema validation enabled, auto config false
# and multiple connector support
MOCK_CONFIG_DATA_1_MC = {
    **MOCK_CONFIG_DATA,
    CONF_CSID: "test_csid_1_mc",
    CONF_PORT: 9001,
    CONF_CPIDS: [
        {
            "CP_1_mc": {
                CONF_CPID: "test_cpid_9001",
                CONF_IDLE_INTERVAL: 900,
                CONF_MAX_CURRENT: 32,
                CONF_METER_INTERVAL: 60,
                CONF_MONITORED_VARIABLES: DEFAULT_MONITORED_VARIABLES,
                CONF_MONITORED_VARIABLES_AUTOCONFIG: False,
                CONF_NUM_CONNECTORS: 2,
                CONF_SKIP_SCHEMA_VALIDATION: True,
                CONF_FORCE_SMART_CHARGING: True,
            }
        },
    ],
}

# allow many chargers to connect
MOCK_CONFIG_DATA_2 = {
    **MOCK_CONFIG_DATA,
    CONF_CSID: "test_csid_2",
}

# empty monitored variables
MOCK_CONFIG_DATA_3 = {
    **MOCK_CONFIG_DATA,
    CONF_CSID: "test_csid_3",
    CONF_CPIDS: [
        {
            "test_cpid": {
                CONF_CPID: "test_cpid",
                CONF_IDLE_INTERVAL: 900,
                CONF_MAX_CURRENT: 32,
                CONF_METER_INTERVAL: 60,
                CONF_MONITORED_VARIABLES: "",
                CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
                CONF_SKIP_SCHEMA_VALIDATION: False,
                CONF_FORCE_SMART_CHARGING: True,
            }
        },
    ],
}


MOCK_CONFIG_MIGRATION_FLOW = {
    CONF_HOST: "127.0.0.1",
    CONF_PORT: 9005,
    CONF_CSID: "test_migration_flow",
    CONF_SSL: False,
    CONF_SSL_CERTFILE_PATH: "/tests/fullchain.pem",
    CONF_SSL_KEYFILE_PATH: "/tests/privkey.pem",
    CONF_WEBSOCKET_CLOSE_TIMEOUT: 1,
    CONF_WEBSOCKET_PING_TRIES: 0,
    CONF_WEBSOCKET_PING_INTERVAL: 1,
    CONF_WEBSOCKET_PING_TIMEOUT: 1,
    CONF_CPID: "test_cpid_migration_flow",
    CONF_IDLE_INTERVAL: 900,
    CONF_MAX_CURRENT: 32,
    CONF_METER_INTERVAL: 60,
    CONF_MONITORED_VARIABLES: DEFAULT_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG: True,
    CONF_SKIP_SCHEMA_VALIDATION: False,
    CONF_FORCE_SMART_CHARGING: True,
}
