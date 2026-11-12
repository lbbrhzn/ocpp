"""Test that MeterValues with blank values or units pass schema validation."""

import copy
import json
from unittest.mock import AsyncMock, patch

import pytest
from ocpp.charge_point import ChargePoint as OcppChargePoint
from ocpp.exceptions import OCPPError
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


@pytest.mark.parametrize("unit", ["", "Wh"])
def test_all_blank_payload_is_flagged_for_ack(unit):
    """With no reading left, the payload is untouched and flagged, valid unit or not."""
    msg = _meter_values(
        [{"measurand": "Energy.Active.Import.Register", "value": "", "unit": unit}]
    )
    original = copy.deepcopy(msg.payload)

    assert ChargePoint._strip_blank_meter_samples(msg) is True
    assert msg.payload == original


def _bare_charge_point() -> ChargePoint:
    """Return a real ChargePoint with only the transport mocked."""
    cp = object.__new__(ChargePoint)
    cp.id = "cp"
    cp._ocpp_version = "1.6"
    cp._send = AsyncMock()
    return cp


async def test_all_blank_meter_values_are_acked_not_applied():
    """_handle_call answers with an empty MeterValues.conf and skips the handler."""
    cp = _bare_charge_point()
    msg = _meter_values(
        [{"measurand": "Energy.Active.Import.Register", "value": "", "unit": "Wh"}]
    )

    with patch.object(OcppChargePoint, "_handle_call", new=AsyncMock()) as handler:
        await ChargePoint._handle_call(cp, msg)

    handler.assert_not_called()
    sent = json.loads(cp._send.call_args.args[0])
    assert sent == [3, msg.unique_id, {}]


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


async def test_all_blank_meter_values_with_bad_fields_still_fail_validation():
    """Skipping the blank samples must not skip validation of the rest."""
    cp = _bare_charge_point()
    msg = Call(
        unique_id="5",
        action="MeterValues",
        payload={
            "connectorId": "bad",
            "meterValue": [{"sampledValue": [{"value": "", "unit": ""}]}],
        },
    )

    with (
        patch.object(OcppChargePoint, "_handle_call", new=AsyncMock()) as handler,
        pytest.raises(OCPPError),
    ):
        await ChargePoint._handle_call(cp, msg)

    handler.assert_not_called()
    cp._send.assert_not_called()
