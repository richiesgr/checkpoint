"""Property 3: routing matches exactly the enabled, exact-equal rules.

Each Hypothesis example is one batch (many events, many rules) run through ``build_publish_ready``
in a single DataFrame and a single Spark action.

**Validates: Requirements 3.1, 3.3, 3.5, 11.2**
"""
from __future__ import annotations

import json
import uuid
from collections import Counter

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from event_forwarding.config_model import parse_config
from event_forwarding.pipeline import build_publish_ready

TENANT_A = str(uuid.UUID("3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"))
TENANT_B = str(uuid.UUID("9a1c5d2e-0b3f-4c6d-8e7a-1f2b3c4d5e6f"))
TENANTS = (TENANT_A, TENANT_B)

# Products/schemas differ only by case or surrounding whitespace.
PRODUCTS = ("web", "Web", "WEB", " web", "web ", "mail")
SCHEMAS = ("login", "Login", " login", "login\t", "audit")

# Event tenant forms: (text, canonical lower-case tenant or None when it is not a canonical UUID).
EVENT_TENANT_FORMS = [
    (TENANT_A, TENANT_A),
    (TENANT_A.upper(), TENANT_A),
    (TENANT_B, TENANT_B),
    (TENANT_B.upper(), TENANT_B),
    (TENANT_A.replace("-", ""), None),
    ("{" + TENANT_A + "}", None),
    (" " + TENANT_A, None),
    (TENANT_B + " ", None),
    ("", None),
    ("not-a-uuid", None),
]

event_st = st.tuples(
    st.sampled_from(EVENT_TENANT_FORMS), st.sampled_from(PRODUCTS), st.sampled_from(SCHEMAS)
)
# Rule tenants are written lower- or upper-case; config validation normalizes both.
rule_st = st.tuples(
    st.sampled_from(TENANTS),
    st.booleans(),  # upper-case spelling of the tenant in the config
    st.sampled_from(PRODUCTS),
    st.sampled_from(SCHEMAS),
    st.booleans(),  # enabled
)


def _config(rules):
    destinations = []
    rule_dicts = []
    for i, (tenant, upper, product, schema, enabled) in enumerate(rules):
        dest_id = f"d{i}"  # one destination per rule so each output row identifies its rule
        destinations.append({"id": dest_id, "tenant_id": tenant, "kind": "file", "format": "jsonl"})
        rule_dicts.append(
            {
                "tenant_id": tenant.upper() if upper else tenant,
                "product": product,
                "schema": schema,
                "enabled": enabled,
                "destinations": [dest_id],
            }
        )
    return parse_config(
        {
            "kafka": {"bootstrap_servers": "localhost:9092"},
            "output_root": "output",
            "destinations": destinations,
            "rules": rule_dicts,
        },
        env={},
    )


# Feature: event-forwarding, Property 3: Routing matches exactly the enabled, exact-equal rules
# **Validates: Requirements 3.1, 3.3, 3.5, 11.2**
@settings(
    max_examples=2,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(events=st.lists(event_st, min_size=1, max_size=12), rules=st.lists(rule_st, min_size=1, max_size=6))
def test_routing_matches_exactly_enabled_exact_rules(spark, events, rules):
    config = _config(rules)
    lines = []
    for n, ((tenant_text, _canonical), product, schema) in enumerate(events):
        lines.append(
            json.dumps(
                {"event_id": f"e{n}", "product": product, "schema": schema, "tenant_id": tenant_text, "data": {"k": n}}
            )
        )
    text_df = spark.createDataFrame([(line,) for line in lines], ["value"])
    publish_ready, _rejected, _audit = build_publish_ready(text_df, config)
    rows = publish_ready.collect()

    actual = Counter()
    for row in rows:
        value = json.loads(row["value"])
        # Key is <tenant_id>|<destination_id>; it must agree with the message body.
        assert row["key"] == f"{value['tenant_id']}|{value['destination_id']}"
        actual[(value["event_id"], value["destination_id"])] += 1

    expected = Counter()
    for n, ((_text, canonical), product, schema) in enumerate(events):
        if canonical is None:
            continue
        for i, (r_tenant, _upper, r_product, r_schema, enabled) in enumerate(rules):
            if enabled and r_tenant == canonical and r_product == product and r_schema == schema:
                expected[(f"e{n}", f"d{i}")] += 1

    assert actual == expected
