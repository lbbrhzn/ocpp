"""Test that MeterValues with blank values or units pass schema validation."""

import copy

import pytest
from ocpp.messages import Call, validate_payload

from custom_components.ocpp.chargepoint import ChargePoint


def _sungrow_meter_values() -> Call:
    """MeterValues as sent by a Sungrow AC22E (fw LE-01.1E1.001.101.50)."""
    return Call(
        unique_id="1",
        action="MeterValues",
        payload={
            "connectorId": 1,
            "transactionId": 1792629960,
            "meterValue": [
                {
                    "timestamp": "2026-10-22T00:59:02Z",
                    "sampledValue": [
                        {"measurand": "Current.Export", "value": "", "unit": "A"},
                        {
                            "measurand": "Current.Import",
                            "phase": "L1",
                            "value": "13.2",
                            "unit": "A",
                        },
                        {"measurand": "Frequency", "value": "50.0", "unit": ""},
                        {
                            "measurand": "Power.Active.Import",
                            "value": "3190",
                            "unit": "W",
                        },
                        {"measurand": "SoC", "value": "", "unit": "Percent"},
                    ],
                }
            ],
        },
    )


async def test_blank_meter_samples_are_stripped_and_validate():
    """Blank values are dropped, blank units removed, payload validates."""
    msg = _sungrow_meter_values()

    ChargePoint._strip_blank_meter_samples(msg)

    samples = msg.payload["meterValue"][0]["sampledValue"]
    assert [s["measurand"] for s in samples] == [
        "Current.Import",
        "Frequency",
        "Power.Active.Import",
    ]
    assert "unit" not in samples[1]
    await validate_payload(msg, "1.6")


def test_other_actions_are_untouched():
    """Only MeterValues payloads are rewritten."""
    payload = {"meterValue": [{"sampledValue": [{"value": "", "unit": ""}]}]}
    msg = Call(unique_id="2", action="DataTransfer", payload=payload)

    ChargePoint._strip_blank_meter_samples(msg)

    assert msg.payload["meterValue"][0]["sampledValue"] == [{"value": "", "unit": ""}]


def _meter_values(*buckets) -> Call:
    return Call(
        unique_id="3",
        action="MeterValues",
        payload={
            "connectorId": 1,
            "meterValue": [
                {"timestamp": "2026-10-22T00:59:02Z", "sampledValue": b}
                for b in buckets
            ],
        },
    )


async def test_bucket_left_empty_is_dropped():
    """A timestamp whose samples were all blank is removed, others kept."""
    msg = _meter_values(
        [{"measurand": "SoC", "value": "", "unit": "Percent"}],
        [{"measurand": "Voltage", "value": "244", "unit": "V"}],
    )

    ChargePoint._strip_blank_meter_samples(msg)

    assert [mv["sampledValue"] for mv in msg.payload["meterValue"]] == [
        [{"measurand": "Voltage", "value": "244", "unit": "V"}]
    ]
    await validate_payload(msg, "1.6")


def test_all_blank_payload_is_left_for_validation():
    """With no reading left, the original payload is kept untouched."""
    msg = _meter_values([{"measurand": "SoC", "value": "", "unit": ""}])
    original = copy.deepcopy(msg.payload)

    ChargePoint._strip_blank_meter_samples(msg)

    assert msg.payload == original


def test_numeric_zero_is_kept():
    """OCPP 2.0.1 sends numeric values; 0 is a reading, not a blank."""
    msg = _meter_values([{"measurand": "Current.Import", "value": 0}])

    ChargePoint._strip_blank_meter_samples(msg)

    assert msg.payload["meterValue"][0]["sampledValue"] == [
        {"measurand": "Current.Import", "value": 0}
    ]


@pytest.mark.parametrize(
    "meter_value",
    [None, [None], [{"sampledValue": None}], [{"sampledValue": [None]}]],
)
def test_malformed_payload_is_left_for_validation(meter_value):
    """Malformed structures must not raise; validation rejects them later."""
    msg = Call(unique_id="4", action="MeterValues", payload={"meterValue": meter_value})

    ChargePoint._strip_blank_meter_samples(msg)

    assert msg.payload == {"meterValue": meter_value}
