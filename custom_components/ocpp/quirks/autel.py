"""Autel MaxiCharger overrides."""

from ocpp.v16.enums import ChargingProfileKindType

from ..enums import OcppMisc as om


class AutelMixin:
    """Autel MaxiCharger overrides."""

    # Any fixed past instant works; the charger only needs an absolute anchor.
    ABSOLUTE_START_SCHEDULE = "2020-01-01T00:00:00Z"

    @staticmethod
    def _absolute_schedule(units_value: str, limit_value: float) -> dict:
        """Build a single-period schedule anchored at an absolute start time."""
        return {
            om.charging_rate_unit: units_value,
            om.start_schedule: AutelMixin.ABSOLUTE_START_SCHEDULE,
            om.charging_schedule_period: [{om.start_period: 0, om.limit: limit_value}],
        }

    def _station_charge_rate_request(
        self, units_value: str, limit_value: float, stack_level: int
    ):
        """Autel rejects a relative ChargePointMaxProfile, so make it absolute."""
        req = super()._station_charge_rate_request(
            units_value, limit_value, stack_level
        )
        profile = req.cs_charging_profiles
        profile[om.charging_profile_kind] = ChargingProfileKindType.absolute.value
        profile[om.charging_schedule] = self._absolute_schedule(
            units_value, limit_value
        )
        return req
