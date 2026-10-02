"""Vendor-specific behavior overrides.

The vendor is only known once the charger's BootNotification arrives, after the
ChargePoint has been constructed. A matching mixin is therefore applied by
swapping the live instance's class to ``(mixin, base)``.

Each vendor lives in its own module here and is registered in
``_VENDOR_MIXINS`` below. Mixins must override plain methods only: ocpp builds
its ``@on`` route map once in ``__init__``, so an overridden handler would not
be picked up after the swap.
"""

import logging

from .autel import AutelMixin

__all__ = ["AutelMixin", "apply_vendor_quirks"]

_LOGGER = logging.getLogger(__name__)

# Keyed by the normalized start of the reported vendor string.
_VENDOR_MIXINS: dict[str, type] = {"autel": AutelMixin}

_class_cache: dict[tuple[type, type], type] = {}


def _mixin_for(vendor) -> type | None:
    if not isinstance(vendor, str):
        return None
    name = vendor.strip().lower()
    for prefix, mixin in _VENDOR_MIXINS.items():
        if name.startswith(prefix):
            return mixin
    return None


def apply_vendor_quirks(cp, vendor) -> None:
    """Point ``cp`` at the class matching its vendor, replacing any earlier one.

    Runs on every BootNotification, so a charger that reports a different
    vendor later is switched back to its base class rather than stacked.
    """
    current = type(cp)
    base = getattr(current, "_quirk_base", current)
    mixin = _mixin_for(vendor)

    if mixin is None:
        target = base
    else:
        target = _class_cache.get((mixin, base))
        if target is None:
            target = type(
                f"{mixin.__name__.removesuffix('Mixin')}{base.__name__}",
                (mixin, base),
                {"_quirk_base": base},
            )
            _class_cache[(mixin, base)] = target

    if target is not current:
        _LOGGER.info(
            "%s: applying vendor quirks %s for vendor %r",
            getattr(cp, "id", "?"),
            target.__name__,
            vendor,
        )
        cp.__class__ = target
