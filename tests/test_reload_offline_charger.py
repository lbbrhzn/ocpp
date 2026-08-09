"""Reloading the entry while the charger is offline must not kill the platforms.

async_setup_entry forwards the entity platforms when a charger is
*configured* (CONF_CPIDS non-empty); async_unload_entry tore them down only
when one was *connected* (connections != 0). The two predicates diverge
exactly when every configured charger is offline at reload time: unload
skipped the teardown but claimed success, the next setup's forward collided
with the still-registered platforms ("has already been setup!"), and every
entity platform was dead until Core restarted - while the entry reported
loaded and the websocket server listened, so nothing looked wrong.

A reload with the charger offline is not an edge case. It is precisely the
reconfigure-to-fix-the-connection scenario the reconfigure flow exists for,
and it is how the live incident happened: a protocol mismatch kept the
charger in a refused-handshake loop, and submitting the corrected settings
bricked the integration instead of fixing it.

Home Assistant swallows the per-platform ValueError into an
"Error setting up entry" log line and setup still returns True, which is
why the pre-existing reload test passed for years while stepping on this
on every run. These tests assert on the log and the platform bookkeeping,
not on hass.data shape.
"""

from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import CONF_CPIDS, DOMAIN

from .const import MOCK_CONFIG_DATA, MOCK_CONFIG_DATA_1, MOCK_CONFIG_CP_APPEND

PLATFORM_DOMAINS = ("sensor", "switch", "number", "button")


async def _load_platform_components(hass):
    """Load the platform integrations globally, as any real install has.

    Without this the harness lets an over-eager unload off the hook: core
    short-circuits unloading a platform whose component is not loaded at
    all, a state that does not exist on a live system.
    """
    for domain in PLATFORM_DOMAINS:
        assert await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()


def _setup_errors(caplog):
    """Return the swallowed platform-collision log records."""
    return [
        record
        for record in caplog.records
        if "Error setting up entry" in record.getMessage()
        or "has already been setup" in record.getMessage()
    ]


def _platforms_holding(hass, entry_id):
    """Return which entity platforms currently hold this entry."""
    components = hass.data.get("entity_components", {})
    return {
        domain
        for domain in PLATFORM_DOMAINS
        if domain in components and entry_id in components[domain]._platforms
    }


async def test_reload_with_charger_offline_keeps_every_platform(
    hass, bypass_get_data, caplog
):
    """The live incident: configured charger, zero connections, one reload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA_1,
        entry_id="test_offline_reload",
        title="test_offline_reload",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _platforms_holding(hass, entry.entry_id) == set(PLATFORM_DOMAINS)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _setup_errors(caplog) == []
    assert _platforms_holding(hass, entry.entry_id) == set(PLATFORM_DOMAINS)
    # The slider must exist and belong to the live platforms, not linger as
    # an orphan of the pre-reload ones.
    assert hass.states.get("number.test_cpid_9001_connector_1_maximum_current")


async def test_a_second_offline_reload_is_also_clean(hass, bypass_get_data, caplog):
    """Users retry: reconfigure, still not connecting, reconfigure again.

    The first reload used to leave the stale platforms registered, so every
    further reload collided the same way. Two consecutive reloads pin that
    the bookkeeping stays consistent, not just that one pass survives.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA_1,
        entry_id="test_offline_reload_twice",
        title="test_offline_reload_twice",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _setup_errors(caplog) == []
    assert _platforms_holding(hass, entry.entry_id) == set(PLATFORM_DOMAINS)


async def test_reload_of_a_chargerless_entry_stays_clean(hass, bypass_get_data, caplog):
    """A fresh central system with no charger yet forwards no platforms.

    Unloading platforms that were never forwarded raises inside Home
    Assistant ("Config entry was never loaded!"), so the unload path must
    skip them here - the flag has to track what setup did, in both
    directions.
    """
    await _load_platform_components(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA},
        entry_id="test_fresh_reload",
        title="test_fresh_reload",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _platforms_holding(hass, entry.entry_id) == set()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _setup_errors(caplog) == []
    assert not [
        r for r in caplog.records if "Config entry was never loaded" in r.getMessage()
    ]


async def test_first_charger_discovery_reload_transitions_cleanly(
    hass, bypass_get_data, caplog
):
    """The growth edge: setup ran with no chargers, then the first arrives.

    Discovery appends the charger to CONF_CPIDS and the update listener
    reloads. At that moment the entry data says "charger configured" but
    the running setup never forwarded platforms - so an unload predicate
    reading the entry data would try to unload platforms that do not
    exist. The flag records what this setup actually did instead.
    """
    await _load_platform_components(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA},
        entry_id="test_growth_reload",
        title="test_growth_reload",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_CPIDS: [{"CP_new": {**MOCK_CONFIG_CP_APPEND}}]},
    )
    await hass.async_block_till_done()

    assert _setup_errors(caplog) == []
    assert not [
        r for r in caplog.records if "Config entry was never loaded" in r.getMessage()
    ]
    # The reload's fresh setup saw the charger and forwarded the platforms.
    assert _platforms_holding(hass, entry.entry_id) == set(PLATFORM_DOMAINS)
