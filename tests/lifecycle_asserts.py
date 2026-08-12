"""Shared assertions for config-entry lifecycle correctness.

Home Assistant swallows most lifecycle failures: a platform that raises
during setup becomes an "Error setting up entry" log line while setup
still returns True, an unload that never happened still reports success,
and a task that dies in teardown surfaces (sometimes) as an unretrieved
exception long after the code that caused it. Tests that assert only on
hass.data shape pass through all of it - which is how the #2054 platform
collision, the #2064 registry-fake leak and the Energy.Meter.Start poison
each stayed green for months.

These helpers make those failures assertable. The log-signature checks
are the secondary net; the structural asserts (platform bookkeeping,
entity identity across a reload) are the load-bearing ones, because they
hold even if Home Assistant rewords its log lines.
"""

from homeassistant.setup import async_setup_component

PLATFORM_DOMAINS = ("sensor", "switch", "number", "button")

# Logged synchronously, in-band, by the setup/reload/unload call the test
# itself makes - deterministic, and safe for the autouse tripwire.
SYNC_SWALLOWED_SIGNATURES = (
    # HA wraps a platform's setup exception into this line and the entry
    # still reports loaded (#2053: every platform dead, nothing failed).
    "Error setting up entry",
    # A forward colliding with still-registered platforms - the unload
    # that claimed success but never ran (#2054).
    "has already been setup",
    # The mirror image: unloading platforms that were never forwarded
    # (#2054's first-discovery edge).
    "Config entry was never loaded",
)

# Emitted when a fire-and-forget task dies unobserved. This surfaces at
# GC/event-loop-drain time, NOT in-band: the same test can be red on one
# machine and green on CI, and a surviving task can in principle land its
# message in a neighbouring test's window. The autouse tripwire therefore
# deliberately does NOT enforce this class; dedicated lifecycle tests,
# whose windows are controlled, do. Motivating incidents: the registry
# fakes leaking into teardown (#2064/#2065) and the Energy.Meter.Start
# object() poison - both invisible to every assertion then in the suite.
ASYNC_SWALLOWED_SIGNATURES = ("Task exception was never retrieved",)


def swallowed_lifecycle_errors(caplog, include_async=True):
    """Return the swallowed-failure records captured by caplog."""
    signatures = SYNC_SWALLOWED_SIGNATURES
    if include_async:
        signatures = signatures + ASYNC_SWALLOWED_SIGNATURES
    return [
        record
        for record in caplog.records
        if any(sig in record.getMessage() for sig in signatures)
    ]


def assert_no_swallowed_lifecycle_errors(caplog, include_async=True):
    """Fail if the log carries any swallowed lifecycle-failure signature."""
    offenders = swallowed_lifecycle_errors(caplog, include_async)
    assert not offenders, "Swallowed lifecycle errors in log:\n" + "\n".join(
        f"  {record.levelname}: {record.getMessage()}" for record in offenders
    )


def assert_platforms_hold(hass, entry_id, platforms=PLATFORM_DOMAINS):
    """Assert exactly these entity platforms currently hold this entry.

    Reads EntityComponent._platforms, which is core-private: a rename
    breaks this loudly (KeyError), not silently. Accepted because the
    public surface has no way to ask "who holds this entry", and the
    entity-identity asserts below do not depend on it.
    """
    components = hass.data.get("entity_components", {})
    holding = {
        domain
        for domain in PLATFORM_DOMAINS
        if domain in components and entry_id in components[domain]._platforms
    }
    assert holding == set(platforms), (
        f"Entity platforms holding {entry_id}: {sorted(holding)}, "
        f"expected {sorted(platforms)}"
    )


def live_entity(hass, entity_id, domain):
    """Return the entity object the given platform currently owns."""
    return hass.data["entity_components"][domain].get_entity(entity_id)


def assert_rebuilt(before, after):
    """Assert an entity was rebuilt - the load-bearing reload check.

    A clean reload tears platforms down and builds new ones, so the
    entity must be a fresh object. The broken path leaves the pre-reload
    platforms - and this exact instance - in place, whatever the logs
    say.
    """
    assert before is not None, "entity did not exist before the reload"
    assert after is not None, "entity missing after the reload"
    assert after is not before, "entity survived the reload - stale platforms"


async def load_platform_components(hass):
    """Load the platform integrations globally, as any real install has.

    Without this the harness lets an over-eager unload off the hook: core
    short-circuits unloading a platform whose component is not loaded at
    all, a state that does not exist on a live system.
    """
    for domain in PLATFORM_DOMAINS:
        assert await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()
