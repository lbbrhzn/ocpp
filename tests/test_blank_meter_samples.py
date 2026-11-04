"""Test that MeterValues with blank values or units pass schema validation."""

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


def test_blank_meter_samples_are_stripped_and_validate():
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
    validate_payload(msg, "1.6")


def test_other_actions_are_untouched():
    """Only MeterValues payloads are rewritten."""
    payload = {"meterValue": [{"sampledValue": [{"value": "", "unit": ""}]}]}
    msg = Call(unique_id="2", action="DataTransfer", payload=payload)

    ChargePoint._strip_blank_meter_samples(msg)

    assert msg.payload["meterValue"][0]["sampledValue"] == [{"value": "", "unit": ""}]
