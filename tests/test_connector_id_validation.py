"""Tests for the _connector_id validator used by the clear_profile service."""

import pytest
import voluptuous as vol

from custom_components.ocpp.api import _connector_id


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", 1),
        (1, 1),
        ("42", 42),
        (7.0, 7),
    ],
)
def test_valid_connector_ids(value, expected):
    """Integral, positive connector ids are accepted and returned as int."""
    assert _connector_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        None,
        1.5,
        "1.5",
        "1.0000000000000001",  # float() would round this to 1.0
        "inf",
        "nan",
        float("inf"),
        float("nan"),
        0,
        -1,
        True,
        False,
    ],
)
def test_invalid_connector_ids(value):
    """Fractional, non-numeric, non-positive and boolean values are rejected."""
    with pytest.raises(vol.Invalid):
        _connector_id(value)
