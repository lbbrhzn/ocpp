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
on every run. The load-bearing assertion here is therefore structural -
the entity object must be a fresh instance created by the post-reload
platforms, because the bug's signature is the OLD platforms surviving
with their old entities. The log check is a secondary signal only, and
tracks Home Assistant core wording via lifecycle_asserts.
"""

import asyncio
from unittest.mock import patch

import pytest
import websockets.asyncio.server
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocpp.const import CONF_CPIDS, DOMAIN

from .const import MOCK_CONFIG_DATA, MOCK_CONFIG_DATA_1, MOCK_CONFIG_CP_APPEND
from .lifecycle_asserts import (
    assert_no_swallowed_lifecycle_errors,
    assert_platforms_hold,
    assert_rebuilt,
    live_entity,
    load_platform_components,
)


@pytest.fixture(name="bypass_websockets")
def bypass_websockets_fixture():
    """Stub only the websocket server.

    conftest's bypass_get_data historically also patched StateMachine.get
    to return a synthetic State for every lookup, which poisoned core's
    duplicate-entity check on reload; that patch is gone, but these tests
    keep their own minimal stub so reload correctness never depends on
    what the shared fixture carries.
    """
    future = asyncio.Future()
    future.set_result(websockets.asyncio.server.Server)
    with (
        patch("websockets.asyncio.server.serve", return_value=future),
        patch("websockets.asyncio.server.Server.close"),
        patch("websockets.asyncio.server.Server.wait_closed"),
    ):
        yield


async def test_reload_with_charger_offline_keeps_every_platform(
    hass, bypass_websockets, caplog
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
    assert_platforms_hold(hass, entry.entry_id)
    eid = "number.test_cpid_9001_connector_1_maximum_current"
    entity_before = live_entity(hass, eid, "number")
    assert entity_before is not None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # The structural check: a clean reload tears the platforms down and
    # builds new ones, so the entity must be a fresh object.
    entity_after = live_entity(hass, eid, "number")
    assert_rebuilt(entity_before, entity_after)
    assert_no_swallowed_lifecycle_errors(caplog)
    assert_platforms_hold(hass, entry.entry_id)


async def test_a_second_offline_reload_is_also_clean(hass, bypass_websockets, caplog):
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
    eid = "number.test_cpid_9001_connector_1_maximum_current"
    entity_start = live_entity(hass, eid, "number")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entity_mid = live_entity(hass, eid, "number")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entity_end = live_entity(hass, eid, "number")

    assert_rebuilt(entity_start, entity_mid)
    assert_rebuilt(entity_mid, entity_end)
    assert_no_swallowed_lifecycle_errors(caplog)
    assert_platforms_hold(hass, entry.entry_id)


async def test_reload_of_a_chargerless_entry_stays_clean(
    hass, bypass_websockets, caplog
):
    """A fresh central system with no charger yet forwards no platforms.

    Unloading platforms that were never forwarded raises inside Home
    Assistant ("Config entry was never loaded!"), so the unload path must
    skip them here - the flag has to track what setup did, in both
    directions.
    """
    await load_platform_components(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        # MOCK_CONFIG_DATA used directly, deliberately: these chargerless
        # tests are the natural regression tests for conftest's deepcopy -
        # if setup_config_entry ever goes back to mutating the shared
        # constant, this stops being chargerless mid-suite and fails here.
        data=MOCK_CONFIG_DATA,
        entry_id="test_fresh_reload",
        title="test_fresh_reload",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert_platforms_hold(hass, entry.entry_id, platforms=())

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert_no_swallowed_lifecycle_errors(caplog)


async def test_first_charger_discovery_reload_transitions_cleanly(
    hass, bypass_websockets, caplog
):
    """The growth edge: setup ran with no chargers, then the first arrives.

    Discovery appends the charger to CONF_CPIDS and the update listener
    reloads. At that moment the entry data says "charger configured" but
    the running setup never forwarded platforms - so an unload predicate
    reading the entry data would try to unload platforms that do not
    exist. The flag records what this setup actually did instead.
    """
    await load_platform_components(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA,
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

    assert_no_swallowed_lifecycle_errors(caplog)
    # The reload's fresh setup saw the charger and forwarded the platforms.
    assert_platforms_hold(hass, entry.entry_id)
