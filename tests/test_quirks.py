"""Tests for vendor-specific overrides (custom_components/ocpp/quirks.py)."""

import pytest
from ocpp.v16.enums import (
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingRateUnitType,
)

from custom_components.ocpp.enums import OcppMisc as om
from custom_components.ocpp.ocppv16 import ChargePoint as ChargePointv16
from custom_components.ocpp.quirks import AutelMixin, apply_vendor_quirks


def _cp():
    cp = object.__new__(ChargePointv16)  # type: ignore[misc]
    cp.id = "test_cp"
    return cp


def _profile(req):
    return req.cs_charging_profiles


def test_default_station_request_is_relative():
    """Unrelated vendors keep the relative profile."""
    cp = _cp()
    apply_vendor_quirks(cp, "ABB")
    profile = _profile(
        cp._station_charge_rate_request(ChargingRateUnitType.amps.value, 16, 1)
    )
    assert profile[om.charging_profile_kind] == ChargingProfileKindType.relative.value
    assert om.start_schedule not in profile[om.charging_schedule]
    assert type(cp) is ChargePointv16


@pytest.mark.parametrize("vendor", ["Autel", "AUTEL", "  autel ", "Autel Energy"])
def test_autel_station_request_is_absolute(vendor):
    """Autel gets an absolute ChargePointMaxProfile anchored at a fixed time."""
    cp = _cp()
    apply_vendor_quirks(cp, vendor)
    assert isinstance(cp, AutelMixin)
    assert isinstance(cp, ChargePointv16)

    profile = _profile(
        cp._station_charge_rate_request(ChargingRateUnitType.amps.value, 16, 3)
    )
    assert profile[om.charging_profile_kind] == ChargingProfileKindType.absolute.value
    assert (
        profile[om.charging_profile_purpose]
        == ChargingProfilePurposeType.charge_point_max_profile.value
    )
    assert profile[om.stack_level] == 3
    assert profile[om.charging_schedule] == {
        om.charging_rate_unit: ChargingRateUnitType.amps.value,
        om.start_schedule: "2020-01-01T00:00:00Z",
        om.charging_schedule_period: [{om.start_period: 0, om.limit: 16}],
    }


def test_autel_request_targets_station():
    """The override keeps the base request's connector and profile id."""
    cp = _cp()
    apply_vendor_quirks(cp, "Autel")
    req = cp._station_charge_rate_request(ChargingRateUnitType.watts.value, 7000, 1)
    assert req.connector_id == 0
    assert _profile(req)[om.charging_profile_id] == 1000


def test_reapplying_same_vendor_is_idempotent():
    """A repeat BootNotification does not re-swap or stack classes."""
    cp = _cp()
    apply_vendor_quirks(cp, "Autel")
    cls = type(cp)
    apply_vendor_quirks(cp, "Autel")
    assert type(cp) is cls


def test_vendor_change_swaps_back_without_stacking():
    """A later BootNotification with another vendor drops the earlier mixin."""
    cp = _cp()
    apply_vendor_quirks(cp, "Autel")
    apply_vendor_quirks(cp, "ABB")
    assert type(cp) is ChargePointv16
    assert not isinstance(cp, AutelMixin)


@pytest.mark.parametrize("vendor", [None, "", 123, object()])
def test_unusable_vendor_leaves_class_unchanged(vendor):
    """Missing or non-string vendors are ignored."""
    cp = _cp()
    apply_vendor_quirks(cp, vendor)
    assert type(cp) is ChargePointv16


def test_swapped_class_is_cached():
    """Chargers of the same vendor and base share one generated class."""
    a, b = _cp(), _cp()
    apply_vendor_quirks(a, "Autel")
    apply_vendor_quirks(b, "Autel")
    assert type(a) is type(b)
