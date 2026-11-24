"""Unit tests for the Isolation_Guard (task 4.4). Property test is task 4.8."""
from __future__ import annotations

from event_forwarding.config_model import (
    Config,
    Destination,
    KafkaSettings,
    Rule,
    build_destination_owner_df,
    build_rules_df,
)
from event_forwarding.isolation_guard import ISOLATION_VIOLATION, apply_isolation_guard

A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _config():
    return Config(
        kafka=KafkaSettings(bootstrap_servers="localhost:9092"),
        output_root="output",
        destinations=(
            Destination(id="dest-a", tenant_id=A, kind="file", format="jsonl", settings={}),
            Destination(id="dest-b", tenant_id=B, kind="file", format="jsonl", settings={}),
        ),
        rules=(
            Rule(tenant_id=A, product="sales", schema="purchase", destinations=("dest-a",)),
            Rule(tenant_id=B, product="sales", schema="purchase", destinations=("dest-b",)),
        ),
    )


def test_guard_keeps_owned_and_audits_violations(spark):
    cfg = _config()
    rules_df = build_rules_df(spark, cfg)
    owners_df = build_destination_owner_df(spark, cfg)
    rows = [
        ("ok", A, "sales", "purchase", "dest-a", "{}"),
        ("wrong-owner", B, "sales", "purchase", "dest-a", "{}"),
        ("wrong-product", A, "other", "purchase", "dest-a", "{}"),
        ("unknown-dest", A, "sales", "purchase", "dest-x", "{}"),
    ]
    df = spark.createDataFrame(
        rows, "event_id string, tenant_id string, product string, schema string, destination_id string, payload string"
    )
    guarded, violations = apply_isolation_guard(df, rules_df, owners_df)
    assert [r.event_id for r in guarded.collect()] == ["ok"]
    assert guarded.columns == df.columns
    bad = {r.event_id: r for r in violations.collect()}
    assert set(bad) == {"wrong-owner", "wrong-product", "unknown-dest"}
    assert all(r.reason == ISOLATION_VIOLATION for r in bad.values())
    assert "payload" not in violations.columns
