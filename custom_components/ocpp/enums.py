"""Additional enumerated values to use in home assistant."""

from enum import Enum, IntFlag, auto


class HAChargerServices(str, Enum):
    """Charger status conditions to report in home assistant."""

    # For HA service reference and for function to call use .value
    service_charge_start = "start_transaction"
    service_charge_stop = "stop_transaction"
    service_availability = "availability"
    service_set_charge_rate = "set_charge_rate"
    service_reset = "reset"
    service_unlock = "unlock"
    service_update_firmware = "update_firmware"
    service_configure = "configure"
    service_get_configuration = "get_configuration"
    service_get_diagnostics = "get_diagnostics"
    service_trigger_custom_message = "trigger_custom_message"
    service_clear_profile = "clear_profile"
    service_data_transfer = "data_transfer"


class HAChargerStatuses(str, Enum):
    """Charger status conditions to report in home assistant."""

    status = "Status"
    status_connector = "Status.Connector"
    heartbeat = "Heartbeat"
    latency_ping = "Latency.Ping"
    latency_pong = "Latency.Pong"
    error_code = "Error.Code"
    error_code_connector = "Error.Code.Connector"
    stop_reason = "Stop.Reason"
    firmware_status = "Status.Firmware"
    reconnects = "Reconnects"
    id_tag = "Id.Tag"


class HAChargerDetails(str, Enum):
    """Charger nameplate information to report in home assistant."""

    identifier = "ID"
    model = "Model"
    vendor = "Vendor"
    serial = "Serial"
    firmware_version = "Version.Firmware"
    features = "Features"
    connectors = "Connectors"
    data_response = "Timestamp.Data.Response"
    data_transfer = "Timestamp.Data.Transfer"
    config_response = "Timestamp.Config.Response"


class HAChargerSession(str, Enum):
    """Charger session information to report in home assistant."""

    transaction_id = "Transaction.Id"
    session_time = "Time.Session"  # in min
    session_energy = "Energy.Session"  # in kWh
    meter_start = "Energy.Meter.Start"  # in kWh


class Profiles(IntFlag):
    """Flags to indicate supported feature profiles."""

    NONE = 0
    CORE = auto()  # Core
    FW = auto()  # FirmwareManagement
    SMART = auto()  # SmartCharging
    RES = auto()  # Reservation
    REM = auto()  # RemoteTrigger
    AUTH = auto()  # LocalAuthListManagement

    def labels(self):
        """Get labels for profiles."""
        if self == Profiles.NONE:
            return "NONE"
        return "|".join([p.name for p in Profiles if p & self])


class OcppMisc(str, Enum):
    """Miscellaneous strings used in ocpp v1.6 responses."""

    # For pythonic version use .name (eg with kwargs) for ocpp json use .value
    context = "context"
    key = "key"
    limit = "limit"
    location = "location"
    measurand = "measurand"
    phase = "phase"
    reason = "reason"
    readonly = "readonly"
    status = "status"
    unit = "unit"
    value = "value"
    sampled_value = "sampledValue"
    transaction_id = "transactionId"
    charge_point_serial_number = "chargePointSerialNumber"
    charge_point_vendor = "chargePointVendor"
    charge_point_model = "chargePointModel"
    firmware_version = "firmwareVersion"
    charging_profile_id = "chargingProfileId"
    stack_level = "stackLevel"
    charging_profile_kind = "chargingProfileKind"
    charging_profile_purpose = "chargingProfilePurpose"
    charging_schedule = "chargingSchedule"
    charging_rate_unit = "chargingRateUnit"
    charging_schedule_period = "chargingSchedulePeriod"
    start_period = "startPeriod"
    feature_profile_core = "Core"
    feature_profile_firmware = "FirmwareManagement"
    feature_profile_smart = "SmartCharging"
    feature_profile_reservation = "Reservation"
    feature_profile_remote = "RemoteTrigger"
    feature_profile_auth = "LocalAuthListManagement"
    tech_info = "techInfo"

    # for use with Smart Charging
    current = "Current"
    power = "Power"


class ConfigurationKey(str, Enum):
    """Configuration Key Names."""

    # 9.1 Core Profile
    allow_offline_tx_for_unknown_id = "AllowOfflineTxForUnknownId"
    authorization_cache_enabled = "AuthorizationCacheEnabled"
    authorize_remote_tx_requests = "AuthorizeRemoteTxRequests"
    blink_repeat = "BlinkRepeat"
    clock_aligned_data_interval = "ClockAlignedDataInterval"
    connection_time_out = "ConnectionTimeOut"
    connector_phase_rotation = "ConnectorPhaseRotation"
    connector_phase_rotation_max_length = "ConnectorPhaseRotationMaxLength"
    get_configuration_max_keys = "GetConfigurationMaxKeys"
    heartbeat_interval = "HeartbeatInterval"
    light_intensity = "LightIntensity"
    local_authorize_offline = "LocalAuthorizeOffline"
    local_pre_authorize = "LocalPreAuthorize"
    max_energy_on_invalid_id = "MaxEnergyOnInvalidId"
    meter_values_aligned_data = "MeterValuesAlignedData"
    meter_values_aligned_data_max_length = "MeterValuesAlignedDataMaxLength"
    meter_values_sampled_data = "MeterValuesSampledData"
    meter_values_sampled_data_max_length = "MeterValuesSampledDataMaxLength"
    meter_value_sample_interval = "MeterValueSampleInterval"
    minimum_status_duration = "MinimumStatusDuration"
    number_of_connectors = "NumberOfConnectors"
    reset_retries = "ResetRetries"
    stop_transaction_on_ev_side_disconnect = "StopTransactionOnEVSideDisconnect"
    stop_transaction_on_invalid_id = "StopTransactionOnInvalidId"
    stop_txn_aligned_data = "StopTxnAlignedData"
    stop_txn_aligned_data_max_length = "StopTxnAlignedDataMaxLength"
    stop_txn_sampled_data = "StopTxnSampledData"
    stop_txn_sampled_data_max_length = "StopTxnSampledDataMaxLength"
    supported_feature_profiles = "SupportedFeatureProfiles"
    supported_feature_profiles_max_length = "SupportedFeatureProfilesMaxLength"
    transaction_message_attempts = "TransactionMessageAttempts"
    transaction_message_retry_interval = "TransactionMessageRetryInterval"
    unlock_connector_on_ev_side_disconnect = "UnlockConnectorOnEVSideDisconnect"
    web_socket_ping_interval = "WebSocketPingInterval"

    # 9.2 Local Auth List Management Profile
    local_auth_list_enabled = "LocalAuthListEnabled"
    local_auth_list_max_length = "LocalAuthListMaxLength"
    send_local_list_max_length = "SendLocalListMaxLength"

    # 9.3 Reservation Profile
    reserve_connector_zero_supported = "ReserveConnectorZeroSupported"

    # 9.4 Smart Charging Profile
    charge_profile_max_stack_level = "ChargeProfileMaxStackLevel"
    charging_schedule_allowed_charging_rate_unit = (
        "ChargingScheduleAllowedChargingRateUnit"
    )
    charging_schedule_max_periods = "ChargingScheduleMaxPeriods"
    connector_switch_3to1_phase_supported = "ConnectorSwitch3to1PhaseSupported"
    max_charging_profiles_installed = "MaxChargingProfilesInstalled"
