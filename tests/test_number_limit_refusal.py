"""The current-limit slider must not keep a value the charger refused.

async_set_native_value moved the slider first and then swallowed every
failure into a log warning, so a refused limit left the UI reading as if it
had been applied. Caught live on a FoxESS A022KP1 whose OCPP 2.0.1 firmware
rejects every SetChargingProfile: the slider showed 16 A while a clamp
meter showed the car drawing 30.6 A.

That matters more here than for most entities. This number is the only
thing telling the user what the circuit is limited to, so someone capping
a shared feed can believe it is capped when it is not. Per #2049 the value
is now put back and the failure raised, so the frontend surfaces it and the
charger's own reason survives.
"""

from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ocpp.const import DOMAIN
from custom_components.ocpp.number import NUMBERS, ChargePointNumber


def _mk_number(hass, result=None, error=None, on_write=None):
    """Build the maximum-current entity with a scripted central system."""
    calls = []

    async def set_max_charge_rate_amps(cpid, value, connector_id=0):
        calls.append((cpid, value, connector_id))
        if error is not None:
            raise error
        return result

    central = SimpleNamespace(
        set_max_charge_rate_amps=set_max_charge_rate_amps,
        get_available=lambda *a, **k: True,
        get_supported_features=lambda *a, **k: 0xFF,
        get_metric=lambda *a, **k: None,
    )
    entity = ChargePointNumber(
        hass, central, "test_cpid", NUMBERS[0], connector_id=None, op_connector_id=0
    )
    entity.hass = hass
    entity.entity_id = "number.test_cpid_maximum_current"
    # The entity is not added to hass, so writing state would need the
    # registry; record the sequence instead - the order of writes is the
    # behaviour under test.
    written = []
    entity.async_write_ha_state = lambda: written.append(
        on_write() if on_write else entity._attr_native_value
    )
    entity.calls = calls
    entity.written = written
    return entity


@pytest.mark.asyncio
async def test_an_accepted_limit_is_kept(hass):
    """Control: the success path must be untouched by the revert logic."""
    entity = _mk_number(hass, result=True)
    entity._attr_native_value = 32.0

    await entity.async_set_native_value(16)

    assert entity._attr_native_value == 16.0
    assert entity.calls == [("test_cpid", 16.0, 0)]


@pytest.mark.asyncio
async def test_a_refused_limit_is_reverted_and_raised(hass):
    """A falsy result means the charger did not take it."""
    entity = _mk_number(hass, result=False)
    entity._attr_native_value = 32.0

    with pytest.raises(HomeAssistantError) as excinfo:
        await entity.async_set_native_value(16)

    assert entity._attr_native_value == 32.0
    assert "16.0 A" in str(excinfo.value.translation_placeholders["message"])


@pytest.mark.asyncio
async def test_the_charger_s_own_reason_is_preserved(hass):
    """set_charge_rate raises with status_info; that message must survive.

    It is the only explanation of why the charger said no, so it is
    re-raised untouched rather than wrapped in a generic message.
    """
    refusal = HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="set_variables_error",
        translation_placeholders={"message": "Rejected: over site limit"},
    )
    entity = _mk_number(hass, error=refusal)
    entity._attr_native_value = 32.0

    with pytest.raises(HomeAssistantError) as excinfo:
        await entity.async_set_native_value(16)

    assert excinfo.value is refusal
    assert entity._attr_native_value == 32.0


@pytest.mark.asyncio
async def test_an_unexpected_failure_is_also_reverted(hass):
    """A transport error must not leave a phantom limit on screen either.

    The live case arrived as a CallError raised out of the library rather
    than as a False return, so both shapes have to revert.
    """
    entity = _mk_number(hass, error=TimeoutError("no reply"))
    entity._attr_native_value = 32.0

    with pytest.raises(HomeAssistantError) as excinfo:
        await entity.async_set_native_value(16)

    assert entity._attr_native_value == 32.0
    assert "no reply" in str(excinfo.value.translation_placeholders["message"])


@pytest.mark.asyncio
async def test_the_revert_is_written_to_the_ui(hass):
    """Restoring the attribute is not enough; the state has to be pushed.

    Without the second write the frontend keeps rendering the refused value
    until something else happens to update the entity - which is the very
    bug this fixes, just with an error toast on top.
    """
    entity = _mk_number(hass, result=False)
    entity._attr_native_value = 32.0

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(16)

    assert entity.written == [16.0, 32.0]


@pytest.mark.asyncio
async def test_the_slider_moves_before_the_call(hass):
    """Optimistic movement is kept: the drag must not wait on the charger.

    Reverting only on failure is the point - a slow charger should not make
    the slider feel unresponsive - so the value is written first and put
    back only if the answer is no.
    """
    seen = {}

    async def observe(cpid, value, connector_id=0):
        seen["value_during_call"] = entity._attr_native_value
        seen["writes_before_call"] = list(entity.written)
        return True

    entity = _mk_number(hass, result=True)
    entity._attr_native_value = 32.0
    entity.central_system.set_max_charge_rate_amps = observe

    await entity.async_set_native_value(24)

    assert seen["value_during_call"] == 24.0
    assert seen["writes_before_call"] == [24.0]
    assert entity.written == [24.0]


@pytest.mark.asyncio
async def test_a_restored_none_value_reverts_to_none(hass):
    """Before the first successful set there is nothing to go back to.

    The entity restores its value on startup, so this is reachable on a
    fresh install: reverting must not invent a number the charger never
    confirmed.
    """
    entity = _mk_number(hass, result=False)
    entity._attr_native_value = None

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(16)

    assert entity._attr_native_value is None


@pytest.mark.asyncio
async def test_an_offline_charger_reverts_too(hass):
    """set_max_charge_rate_amps returns False when the charger is absent.

    Same shape as a refusal and the same consequence for the user, so it
    must not silently show a limit either.
    """
    entity = _mk_number(hass, result=False)
    entity._attr_native_value = 32.0

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(6)

    assert entity._attr_native_value == 32.0
    assert entity.calls == [("test_cpid", 6.0, 0)]
