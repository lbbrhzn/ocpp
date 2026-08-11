"""Measurand resolution for OCPP 2.0.1 chargers that omit valuesList.

variableCharacteristics.valuesList is optional in OCPP 2.0.1. Reading only
that field left the measurand list empty for any charger that omits it, and
get_supported_measurands then wrote that empty list back with SetVariables -
clearing whatever the charger was configured to report, and disabling every
meter value inside TransactionEvent. The write persists on the charger, so
the damage outlives the session.

Only an advertised valuesList understood in full is written back. Anything
else describes the charger's own configuration rather than its capabilities,
so writing it back could only narrow it.
"""

import asyncio
from types import SimpleNamespace

import pytest
from ocpp.v201.enums import MeasurandEnumType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.ocppv201 import ChargePoint

from .const import CONF_SSL_CERTFILE_PATH, CONF_SSL_KEYFILE_PATH

ENERGY = MeasurandEnumType.energy_active_import_register.value
POWER = MeasurandEnumType.power_active_import.value
VENDOR = "Vendor.Custom.Thing"  # spec-legal, and not in MeasurandEnumType


def _mk_cp(hass, monitored_variables: str = ""):
    """Build a v201 ChargePoint that records the requests it sends."""
    data = {
        "host": "127.0.0.1",
        "port": 0,
        "csid": "cs",
        "cpids": [{"CP_A": {"cpid": "test_cpid"}}],
        "subprotocols": ["ocpp2.0.1"],
        "websocket_close_timeout": 5,
        "ssl": False,
        "websocket_ping_interval": 0.0,
        "websocket_ping_timeout": 0.01,
        "websocket_ping_tries": 0,
        "ssl_certfile_path": CONF_SSL_CERTFILE_PATH,
        "ssl_keyfile_path": CONF_SSL_KEYFILE_PATH,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    central = CentralSystemSettings(**data)
    charger = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32,
        idle_interval=60,
        meter_interval=60,
        monitored_variables=monitored_variables,
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    conn = SimpleNamespace(
        state=State.CLOSED,
        close=lambda: asyncio.sleep(0),
        subprotocol="ocpp2.0.1",
    )
    cp = ChargePoint("CP_A", conn, hass, entry, central, charger)
    cp.sent = []

    async def record(req):
        cp.sent.append(req)
        return SimpleNamespace(status="Accepted")

    cp.call = record
    return cp


def _entry(*, values_list=None, current_value=None, evse=None):
    """Build one SampledDataCtrlr/TxUpdatedMeasurands report entry."""
    characteristics = {"data_type": "string", "supports_monitoring": False}
    if values_list is not None:
        characteristics["values_list"] = values_list
    component = {"name": "SampledDataCtrlr"}
    if evse is not None:
        component["evse"] = evse
    out = {
        "component": component,
        "variable": {"name": "TxUpdatedMeasurands"},
        "variable_characteristics": characteristics,
    }
    if current_value is not None:
        out["variable_attribute"] = [{"value": current_value}]
    return out


def _report(cp, *entries):
    """Feed report entries through the real inventory parser."""
    cp._inventory = None
    cp._wait_inventory = asyncio.Event()
    cp.on_report(1, "2026-01-01T00:00:00Z", 0, report_data=list(entries))


def _measurand_writes(cp):
    """Return the TxUpdatedMeasurands values written back to the charger."""
    out = []
    for req in cp.sent:
        for item in getattr(req, "set_variable_data", None) or []:
            if item.get("variable", {}).get("name") == "TxUpdatedMeasurands":
                out.append(item.get("attribute_value"))
    return out


@pytest.mark.asyncio
async def test_current_value_used_when_valueslist_is_absent(hass):
    """A charger that omits valuesList keeps the measurands it already has."""
    cp = _mk_cp(hass)

    _report(cp, _entry(current_value=f"{ENERGY},{POWER}"))

    assert cp._inventory.tx_updated_measurands == [
        MeasurandEnumType(ENERGY),
        MeasurandEnumType(POWER),
    ]

    accepted = await cp.get_supported_measurands()

    # Reported to Home Assistant so the sensors exist...
    assert accepted == f"{ENERGY},{POWER}"
    # ...but this is the charger's own configuration, so nothing is written.
    assert cp.sent == []


@pytest.mark.asyncio
async def test_nothing_is_written_when_there_is_nothing_to_go_on(hass):
    """With no valuesList and no current value, nothing is touched.

    The empty list was previously written back, clearing the charger's
    configuration and silencing all meter values. The configured measurands
    are returned unchanged so post_connect does not overwrite them with ""
    and reload the config entry.
    """
    cp = _mk_cp(hass, monitored_variables=f"{ENERGY},{POWER}")

    _report(cp, _entry(current_value=""))

    assert cp._inventory.tx_updated_measurands == []

    accepted = await cp.get_supported_measurands()

    assert accepted == f"{ENERGY},{POWER}"
    assert cp.sent == []


@pytest.mark.asyncio
async def test_valueslist_still_wins_and_is_written_back(hass):
    """Unchanged behaviour: a fully understood valuesList is authoritative."""
    cp = _mk_cp(hass)

    _report(cp, _entry(values_list=f"{ENERGY},{POWER}", current_value=ENERGY))

    assert cp._inventory.tx_updated_measurands_rank == 2

    accepted = await cp.get_supported_measurands()

    assert accepted == f"{ENERGY},{POWER}"
    assert _measurand_writes(cp) == [f"{ENERGY},{POWER}"]


@pytest.mark.asyncio
async def test_unknown_measurand_is_skipped_not_raised(hass):
    """An unrecognised measurand must not abort the whole inventory report."""
    cp = _mk_cp(hass)

    _report(cp, _entry(current_value=f"{ENERGY},{VENDOR},{POWER}"))

    assert cp._inventory.tx_updated_measurands == [
        MeasurandEnumType(ENERGY),
        MeasurandEnumType(POWER),
    ]


@pytest.mark.asyncio
async def test_a_dropped_measurand_is_never_written_back(hass):
    """Losing an entry we cannot parse must not narrow the charger's config.

    Vendor.* measurands are permitted by OCPP 2.0.1 and are not in
    MeasurandEnumType. Writing the surviving subset back would delete the
    vendor measurand from the charger permanently - the same damage class
    this module exists to prevent, just partial rather than total.
    """
    cp = _mk_cp(hass)

    _report(cp, _entry(values_list=f"{ENERGY},{VENDOR}"))

    assert cp._inventory.tx_updated_measurands_rank == 0

    accepted = await cp.get_supported_measurands()

    assert accepted == ENERGY
    assert cp.sent == []


@pytest.mark.asyncio
async def test_a_weaker_entry_does_not_displace_an_advertised_list(hass):
    """SampledDataCtrlr is reportable per-EVSE, so entries can repeat.

    An EVSE-scoped entry carrying only the current value must not replace a
    station-level valuesList that arrived first, or message ordering alone
    would decide what gets written back to the charger.
    """
    cp = _mk_cp(hass)

    _report(
        cp,
        _entry(values_list=f"{ENERGY},{POWER}"),
        _entry(current_value=ENERGY, evse={"id": 1}),
    )

    assert cp._inventory.tx_updated_measurands == [
        MeasurandEnumType(ENERGY),
        MeasurandEnumType(POWER),
    ]
    assert cp._inventory.tx_updated_measurands_rank == 2

    accepted = await cp.get_supported_measurands()

    assert accepted == f"{ENERGY},{POWER}"
    assert _measurand_writes(cp) == [f"{ENERGY},{POWER}"]


@pytest.mark.asyncio
async def test_evse_scoped_list_does_not_replace_the_station_list(hass):
    """A per-EVSE valuesList may be narrower than the charging station's.

    SetVariables here targets the station-level SampledDataCtrlr, so adopting
    one EVSE's list and writing it back would narrow the station's settings.
    """
    cp = _mk_cp(hass)

    _report(
        cp,
        _entry(values_list=f"{ENERGY},{POWER}"),
        _entry(values_list=ENERGY, evse={"id": 1}),
    )

    assert cp._inventory.tx_updated_measurands_rank == 2

    accepted = await cp.get_supported_measurands()

    assert accepted == f"{ENERGY},{POWER}"
    assert _measurand_writes(cp) == [f"{ENERGY},{POWER}"]


@pytest.mark.asyncio
async def test_station_list_wins_even_when_it_arrives_last(hass):
    """Precedence must be by scope, not by arrival order."""
    cp = _mk_cp(hass)

    _report(
        cp,
        _entry(values_list=ENERGY, evse={"id": 1}),
        _entry(values_list=f"{ENERGY},{POWER}"),
    )

    assert cp._inventory.tx_updated_measurands_rank == 2

    accepted = await cp.get_supported_measurands()

    assert accepted == f"{ENERGY},{POWER}"
    assert _measurand_writes(cp) == [f"{ENERGY},{POWER}"]


@pytest.mark.asyncio
async def test_evse_only_list_is_reported_but_not_written(hass):
    """With no station-level entry, report the EVSE list but do not write it."""
    cp = _mk_cp(hass)

    _report(cp, _entry(values_list=f"{ENERGY},{POWER}", evse={"id": 1}))

    assert cp._inventory.tx_updated_measurands_rank == 1

    accepted = await cp.get_supported_measurands()

    assert accepted == f"{ENERGY},{POWER}"
    assert cp.sent == []
