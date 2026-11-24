"""Property 9: the Sink_Isolation_Check rejects tampered or unowned messages (no Kafka)."""
from __future__ import annotations

import json
from collections import Counter

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from event_forwarding.sink_transform import sink_transform

A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
OWNERS = {"dest-a1": A, "dest-a2": A, "dest-b1": B}

KINDS = ("valid", "wrong_tenant", "wrong_key_tenant", "wrong_key_dest", "unknown_dest")


def _owners_df(spark):
    return spark.createDataFrame(
        [(d, t, f"out/{d}", "file", "jsonl") for d, t in OWNERS.items()],
        "destination_id string, tenant_id string, output_path string, kind string, format string",
    )


def _build(kind: str, dest: str, idx: int):
    """Return (key, value, event_id) for one planted row."""
    owner = OWNERS[dest]
    other = B if owner == A else A
    event_id = f"ev-{idx}"
    msg_tenant, msg_dest, key = owner, dest, f"{owner}|{dest}"
    if kind == "wrong_tenant":  # value tenant is not the destination's owner, key follows the value
        msg_tenant = other
        key = f"{other}|{dest}"
    elif kind == "wrong_key_tenant":  # key carries another tenant
        key = f"{other}|{dest}"
    elif kind == "wrong_key_dest":  # key names another destination of the same tenant
        key = f"{owner}|not-{dest}"
    elif kind == "unknown_dest":
        msg_dest = f"ghost-{idx}"
        key = f"{owner}|{msg_dest}"
    value = json.dumps(
        {
            "tenant_id": msg_tenant,
            "destination_id": msg_dest,
            "product": "sales",
            "schema": "purchase",
            "event_id": event_id,
            "payload": {"n": idx},
        }
    )
    return key, value, event_id


row_st = st.tuples(st.sampled_from(KINDS), st.sampled_from(sorted(OWNERS)))


# Feature: event-forwarding, Property 9: The Sink_Isolation_Check rejects tampered or unowned messages
# **Validates: Requirements 8.9, 8.10, 8.13, 12.5**
@settings(
    max_examples=2,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(plan=st.lists(row_st, min_size=3, max_size=10))
def test_sink_isolation_check_rejects_tampered(spark, make_kafka_rows, plan):
    plan = list(plan) + [("valid", "dest-a1"), ("wrong_tenant", "dest-b1")]
    rows, expected = [], {}
    for idx, (kind, dest) in enumerate(plan):
        key, value, event_id = _build(kind, dest, idx)
        rows.append((key, value, "delivery.file", 0, idx))
        expected[event_id] = (kind, dest, key, value)

    deliverable, dlq, audit = sink_transform(make_kafka_rows(rows), _owners_df(spark))
    d_rows, q_rows, a_rows = deliverable.collect(), dlq.collect(), audit.collect()

    valid_ids = {e for e, (k, *_r) in expected.items() if k == "valid"}
    tampered_ids = set(expected) - valid_ids

    # Zero tampered rows are deliverable; valid rows are deliverable exactly once, to their own destination.
    d_counts = Counter(r["event_id"] for r in d_rows)
    assert set(d_counts) == valid_ids
    assert all(c == 1 for c in d_counts.values())
    for r in d_rows:
        _kind, dest, key, value = expected[r["event_id"]]
        assert r["destination_id"] == dest
        assert r["tenant_id"] == OWNERS[dest]
        assert bytes(r["key"]) == key.encode() and bytes(r["value"]) == value.encode()

    # Each tampered row goes to the DLQ unchanged with isolation_violation, once.
    assert len(q_rows) == len(tampered_ids)
    by_value = {bytes(r["value"]): r for r in q_rows}
    assert len(by_value) == len(tampered_ids)
    for event_id in tampered_ids:
        _kind, _dest, key, value = expected[event_id]
        r = by_value[value.encode()]
        assert bytes(r["key"]) == key.encode()
        assert r["reason"] == "isolation_violation"

    # Exactly one audit row per tampered event; none for valid events.
    a_counts = Counter(r["event_id"] for r in a_rows)
    assert set(a_counts) == tampered_ids
    assert all(c == 1 for c in a_counts.values())
    for r in a_rows:
        assert r["decision"] == "dead-lettered"
        assert r["reason"] == "isolation_violation"
