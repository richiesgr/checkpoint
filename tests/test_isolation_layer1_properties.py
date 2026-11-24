"""Property test for isolation layer 1 (task 4.8)."""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st
from pyspark.sql import functions as F

from event_forwarding.config_model import (
    Config,
    Destination,
    KafkaSettings,
    Rule,
    build_destination_owner_df,
    build_rules_df,
)
from event_forwarding.isolation_guard import ISOLATION_VIOLATION, apply_isolation_guard

PRODUCTS = ["sales", "crm"]
SCHEMAS = ["purchase", "refund"]
COLUMNS = "event_id string, tenant_id string, product string, schema string, destination_id string, payload string"


def _tenant(i: int) -> str:
    return f"{i:08x}-0000-4000-8000-{i:012x}"


@st.composite
def scenarios(draw):
    n_tenants = draw(st.integers(2, 5))
    tenants = [_tenant(i + 1) for i in range(n_tenants)]
    destinations, rules = [], []
    for ti, tenant in enumerate(tenants):
        own = [f"t{ti}-d{j}" for j in range(draw(st.integers(1, 3)))]
        destinations += [Destination(id=d, tenant_id=tenant, kind="file", format="jsonl") for d in own]
        for product in PRODUCTS:
            for schema in SCHEMAS:
                if draw(st.booleans()):
                    chosen = draw(st.lists(st.sampled_from(own), min_size=1, max_size=len(own), unique=True))
                    rules.append(
                        Rule(tenant_id=tenant, product=product, schema=schema,
                             destinations=tuple(chosen), enabled=draw(st.booleans()))
                    )
    config = Config(
        kafka=KafkaSettings(bootstrap_servers="localhost:9092"),
        output_root="output",
        destinations=tuple(destinations),
        rules=tuple(rules),
    )
    dest_ids = [d.id for d in destinations] + ["ghost-dest"]
    records = draw(
        st.lists(
            st.tuples(st.sampled_from(tenants), st.sampled_from(PRODUCTS + ["other"]),
                      st.sampled_from(SCHEMAS), st.sampled_from(dest_ids)),
            min_size=10, max_size=40,
        )
    )
    # Planted mismatch: a tenant's record aimed at another tenant's destination.
    victim = draw(st.sampled_from(destinations))
    thief = draw(st.sampled_from([t for t in tenants if t != victim.tenant_id]))
    records.append((thief, "sales", "purchase", victim.id))
    return config, records


def _expected_ok(config, tenant, product, schema, dest_id):
    dest = config.destination(dest_id)
    if dest is None or dest.tenant_id != tenant:
        return False
    return any(
        r.enabled and r.tenant_id == tenant and r.product == product and r.schema == schema
        and dest_id in r.destinations
        for r in config.rules
    )


# Feature: event-forwarding, Property 8: The Isolation_Guard admits only ownership-correct records
# **Validates: Requirements 8.2, 8.5, 8.6, 8.9, 8.12, 8.14, 11.3**
@settings(max_examples=2, deadline=None, suppress_health_check=list(HealthCheck))
@given(scenarios())
def test_isolation_guard_admits_only_ownership_correct_records(spark, scenario):
    config, records = scenario
    rows = [(f"e{i}", t, p, s, d, f"payload-{i}") for i, (t, p, s, d) in enumerate(records)]
    df = spark.createDataFrame(rows, COLUMNS)
    guarded, violations = apply_isolation_guard(
        df, build_rules_df(spark, config), build_destination_owner_df(spark, config)
    )
    # One Spark action per example: both sides tagged and unioned.
    tagged = guarded.select("event_id", "tenant_id", "product", "schema", "destination_id",
                            F.lit("guarded").alias("side"), F.lit(None).cast("string").alias("reason")).unionByName(
        violations.select("event_id", "tenant_id", "product", "schema", "destination_id",
                          F.lit("violation").alias("side"), "reason")
    )
    got = {r.event_id: r for r in tagged.collect()}

    assert len(got) == len(rows)  # every record lands on exactly one side
    for event_id, t, p, s, d, _ in rows:
        r = got[event_id]
        if _expected_ok(config, t, p, s, d):
            assert r.side == "guarded", (event_id, t, p, s, d)
        else:
            assert r.side == "violation" and r.reason == ISOLATION_VIOLATION, (event_id, t, p, s, d)
    # Everything guarded is owned by its destination's tenant.
    for r in got.values():
        if r.side == "guarded":
            assert config.destination(r.destination_id).tenant_id == r.tenant_id
    # The planted mismatch is excluded and audited.
    assert got[rows[-1][0]].side == "violation"
    assert "payload" not in violations.columns
