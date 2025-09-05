"""Test connector-aware metrics handling."""

import pytest

from custom_components.ocpp.chargepoint import _ConnectorAwareMetrics, Metric


def M(v=None, unit=None):
    """Help to create a Metric with a value and a None timestamp."""
    return Metric(v, unit)


def test_flat_set_get_contains_len_iter():
    """Flat access (connector 0) should work and be independent from per-connector."""
    m = _ConnectorAwareMetrics()

    # Flat = connector 0
    m["Power.Active.Import"] = M(1.5)
    assert "Power.Active.Import" in m
    assert len(m) == 1
    assert list(iter(m)) == ["Power.Active.Import"]
    assert m["Power.Active.Import"].value == 1.5

    # Per-connector should not affect flat view length/keys
    m[(2, "Power.Active.Import")] = M(7.0)
    assert len(m) == 1  # len() reports flat/conn-0 size only
    assert "Power.Active.Import" in m
    assert m[(2, "Power.Active.Import")].value == 7.0
    assert m["Power.Active.Import"].value == 1.5


def test_get_whole_connector_mapping_and_assign_full_mapping():
    """Accessing m[conn_id] returns the dict for that connector; setting dict replaces it."""
    m = _ConnectorAwareMetrics()

    # When first accessed, connector dict exists (created by defaultdict)
    conn1_map = m[1]
    assert isinstance(conn1_map, dict)
    assert conn1_map == {}

    # Replace entire mapping for connector 1
    m[1] = {"Voltage": M(229.9), "Current.Import": M(6.0)}
    assert m[(1, "Voltage")].value == 229.9
    assert m[(1, "Current.Import")].value == 6.0

    # Flat (connector 0) remains independent — do NOT access m["Voltage"] (would create)
    assert "Voltage" not in m
    assert "Current.Import" not in m
    assert len(m) == 0


def test_delete_per_connector_and_flat():
    """Deleting per-connector keys must not touch flat; deleting flat must not touch others."""
    m = _ConnectorAwareMetrics()

    m["A"] = M(10)  # flat
    m[(2, "A")] = M(20)  # connector 2

    # Delete per-connector key (check via the connector dict, not tuple get)
    del m[(2, "A")]
    assert "A" not in m[2]  # still present flat
    assert m["A"].value == 10

    # Delete flat key (verify flat keys(), avoid m["A"] which would recreate)
    del m["A"]
    assert "A" not in m

    # Recreate per-connector and verify it stays independent
    m[(2, "A")] = M(99)
    assert m[(2, "A")].value == 99
    assert "A" not in m  # flat untouched


def test_keys_values_items_are_flat_only():
    """keys()/values()/items() only reflect connector 0 (flat) mapping."""
    m = _ConnectorAwareMetrics()
    m["k0"] = M(0)
    m[(1, "k1")] = M(1)
    m[(2, "k2")] = M(2)

    assert list(m.keys()) == ["k0"]
    assert [v.value for v in m.values()] == [0]
    items = list(m.items())
    assert items == [("k0", m["k0"])]


def test_type_checks_on_setitem():
    """__setitem__ must enforce types for flat, per-connector tuple, and connector dict."""
    m = _ConnectorAwareMetrics()

    # Flat must be Metric
    with pytest.raises(TypeError):
        m["foo"] = 123  # not a Metric

    # Per-connector must be Metric
    with pytest.raises(TypeError):
        m[(1, "foo")] = 123  # not a Metric

    # Connector mapping must be dict[str, Metric]
    with pytest.raises(TypeError):
        m[1] = 123

    # Correct types should pass
    m["ok_flat"] = M(1)
    m[(1, "ok_pc")] = M(2)
    m[2] = {"ok_map": M(3)}

    assert m["ok_flat"].value == 1
    assert m[(1, "ok_pc")].value == 2
    assert m[(2, "ok_map")].value == 3


def test_clear_and_contains_tuple_semantics():
    """clear() empties everything; __contains__ tuple logic works without creating entries."""
    m = _ConnectorAwareMetrics()
    m["flat"] = M(1)
    m[(1, "pc")] = M(2)

    # Membership checks
    assert "flat" in m
    assert "pc" in m[1]  # check in the connector dict

    m.clear()

    # After clear, no flat keys and no connector dicts
    assert len(list(m.keys())) == 0


def test_get_variants_and_contains_behavior():
    """Exercise get() for flat keys, per-connector keys, connector dicts, and defaults."""
    m = _ConnectorAwareMetrics()

    # Seed some values
    m["Voltage"] = M(230.0, "V")  # flat (connector 0)
    m[(2, "Voltage")] = M(231.0, "V")  # connector 2

    # 1) get() existing flat key
    got = m.get("Voltage")
    assert isinstance(got, Metric)
    assert got.value == 230.0
    assert "Voltage" in m

    # 2) get() missing flat key -> default is returned
    default_metric = M(0.0, "V")
    got_default = m.get("Nope", default_metric)
    assert isinstance(got_default, Metric)
    assert got_default is default_metric
    assert got_default.value == 0.0
    assert "V" not in m

    # 3) get() existing tuple key
    got_c2 = m.get((2, "Voltage"))
    assert isinstance(got_c2, Metric)
    assert got_c2.value == 231.0
    assert 2 in m

    # 4) get() missing tuple key -> also inserts default, not the missing key
    missing_default = M(7.0)
    got_missing = m.get((2, "Missing"), missing_default)
    assert isinstance(got_missing, Metric)
    assert got_missing is missing_default
    assert got_missing.value == 7.0
    assert 2 in m and "Missing" not in m[2]


def test_delitem_all_paths_and_errors():
    """Cover deletion of flat keys, per-connector keys, and entire connector maps."""
    m = _ConnectorAwareMetrics()

    # Seed:
    m["Voltage"] = M(230.0, "V")  # flat (connector 0)
    m[(2, "Voltage")] = M(231.0, "V")
    m[(2, "Current.Import")] = M(6.0, "A")

    # A) Delete a flat key
    del m["Voltage"]
    assert "Voltage" not in m
    # Accessing again returns a fresh Metric(None, None) due to defaultdict
    after_del_flat = m["Voltage"]
    assert isinstance(after_del_flat, Metric)
    assert after_del_flat.value is None

    # B) Delete a (conn, meas) key
    del m[(2, "Current.Import")]
    assert "Current.Import" not in m[2]
    # Accessing again creates a fresh Metric(None, None)
    after_del_tuple = m[(2, "Current.Import")]
    assert isinstance(after_del_tuple, Metric)
    assert after_del_tuple.value is None
    # Remaining key for connector 2 is still there
    assert "Voltage" in m[2]

    # C) Delete an entire connector map
    del m[2]
    assert 2 not in m
    # Accessing m[2] recreates an empty mapping via top-level defaultdict
    recreated = m[2]
    assert isinstance(recreated, dict)
    assert 2 in m and recreated == {}

    # D) Deleting a missing flat key raises KeyError and does not create it
    with pytest.raises(KeyError):
        del m["DoesNotExist"]
    assert "DoesNotExist" not in m

    # E) Deleting a missing (conn, meas) raises KeyError, but creates the connector id
    with pytest.raises(KeyError):
        del m[(3, "NoSuchMeas")]
    assert 3 in m and m[3] == {}

    # F) Deleting a non-existent connector id raises KeyError and does not create it
    with pytest.raises(KeyError):
        del m[42]
    assert 42 not in m


def test_get_returns_default_when_inner_is_plain_dict():
    """Ensure get() returns provided default if inner mapping is a plain dict that raises KeyError."""
    m = _ConnectorAwareMetrics()

    # Replace connector 1 map with a plain dict (no defaultdict semantics)
    m[1] = {"Voltage": M(230.0, "V")}

    # Missing measurand under connector 1 -> __getitem__ would raise KeyError,
    # so get() must return the supplied default (and not insert anything).
    default_metric = M(99.0, "V")
    got = m.get((1, "Missing"), default_metric)
    assert got is default_metric

    # Confirm we didn't insert the missing key as a side effect
    assert "Missing" not in m[1]
    # And tuple __contains__ is still False
    assert (1, "Missing") not in m


def test_contains_tuple_semantics_true_false_and_missing_connector():
    """Exercise __contains__ for (connector, measurand) tuples."""
    m = _ConnectorAwareMetrics()

    # Seed values on different connectors
    m[(2, "Voltage")] = M(231.0, "V")
    m[(3, "Current.Import")] = M(6.0, "A")

    # Present tuple -> True
    assert (2, "Voltage") in m
    assert (3, "Current.Import") in m

    # Wrong measurand on existing connector -> False
    assert (2, "Current.Import") not in m
    assert (3, "Voltage") not in m

    # Missing connector entirely -> False (uses .get(conn, {}) in __contains__)
    assert 99 not in m  # connector 99 doesn't exist yet
    assert (99, "Voltage") not in m  # tuple contains should be False as well

    # After creating empty map for 99 (via direct access), measurand still absent -> False
    _ = m[99]  # creates empty mapping for connector 99
    assert (99, "Voltage") not in m
