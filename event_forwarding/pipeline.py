"""Processing-stage pipeline: parse, route, shape, guard, publish-ready (Spark Column logic only)."""
from __future__ import annotations

import json

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

from event_forwarding.envelope_schema import (
    ENVELOPE_FIELDS,
    REASON_INVALID_DATA,
    REASON_INVALID_TENANT_ID,
    REASON_MALFORMED_JSON,
    REASON_MISSING_ENVELOPE_FIELD,
    UUID_REGEX,
)

# Columns of the rejected DataFrame returned by parse_events.
REJECTED_COLUMNS = ("line", "source_file", "reason", "event_id", "product", "schema", "tenant_id")


def _field_variant(parsed: Column, name: str) -> Column:
    return F.try_variant_get(parsed, f"$.{name}", "variant")


def _is_absent_or_null(field: Column) -> Column:
    # SQL NULL means the key is absent; a variant null means JSON null.
    return field.isNull() | F.coalesce(F.is_variant_null(field), F.lit(False))


def parse_events(text_df: DataFrame) -> tuple[DataFrame, DataFrame]:
    """Split raw JSON lines into (valid, rejected) DataFrames.

    ``text_df`` has a string column ``value`` (as produced by the text source). If it also has a
    ``source_file`` column that is used, otherwise ``input_file_name()`` supplies it.

    Classification, in order: blank lines are skipped; ``malformed_json`` (not JSON or top-level
    not an object); ``missing_envelope_field`` (an envelope field is absent or JSON null);
    ``invalid_data`` (``data`` is not an object); ``invalid_tenant_id`` (``tenant_id`` is not a
    canonical UUID, including empty). A null ``tenant_id`` is a missing envelope field (Req 1.6).

    valid columns: event_id, product, schema, tenant_id (lower-case), data (raw JSON text), source_file.
    rejected columns: see REJECTED_COLUMNS; ``event_id``/``product``/``schema``/``tenant_id`` are
    best-effort raw values (null when unavailable) for audit; never the payload.
    """
    source = F.col("source_file") if "source_file" in text_df.columns else F.input_file_name()
    base = (
        text_df.select(F.col("value").alias("line"), source.alias("source_file"))
        .where(~F.coalesce(F.col("line").rlike(r"^\s*$"), F.lit(True)))
        .withColumn("_parsed", F.try_parse_json(F.col("line")))
    )

    parsed = F.col("_parsed")
    top_is_object = F.coalesce(F.schema_of_variant(parsed).startswith("OBJECT"), F.lit(False))

    fields = {name: _field_variant(parsed, name) for name in ENVELOPE_FIELDS}
    any_missing = F.lit(False)
    for name in ENVELOPE_FIELDS:
        any_missing = any_missing | _is_absent_or_null(fields[name])

    data_is_object = F.coalesce(F.schema_of_variant(fields["data"]).startswith("OBJECT"), F.lit(False))
    tenant_raw = F.try_variant_get(parsed, "$.tenant_id", "string")
    tenant_ok = F.coalesce(tenant_raw.rlike(UUID_REGEX), F.lit(False))

    reason = (
        F.when(~top_is_object, F.lit(REASON_MALFORMED_JSON))
        .when(any_missing, F.lit(REASON_MISSING_ENVELOPE_FIELD))
        .when(~data_is_object, F.lit(REASON_INVALID_DATA))
        .when(~tenant_ok, F.lit(REASON_INVALID_TENANT_ID))
    )

    classified = base.select(
        "line",
        "source_file",
        reason.alias("reason"),
        F.when(top_is_object, F.try_variant_get(parsed, "$.event_id", "string")).alias("event_id"),
        F.when(top_is_object, F.try_variant_get(parsed, "$.product", "string")).alias("product"),
        F.when(top_is_object, F.try_variant_get(parsed, "$.schema", "string")).alias("schema"),
        F.when(top_is_object, tenant_raw).alias("tenant_id"),
        F.when(top_is_object, F.to_json(fields["data"])).alias("data"),
    )

    rejected = classified.where(F.col("reason").isNotNull()).select(*REJECTED_COLUMNS)
    valid = classified.where(F.col("reason").isNull()).select(
        "event_id",
        "product",
        "schema",
        F.lower(F.col("tenant_id")).alias("tenant_id"),
        "data",
        "source_file",
    )
    return valid, rejected


# Columns of the routed DataFrame returned by route_events.
ROUTED_COLUMNS = (
    "event_id",
    "product",
    "schema",
    "tenant_id",
    "data",
    "source_file",
    "rule_index",
    "destination_id",
    "destination_kind",
    "destination_format",
)

DECISION_DROPPED = "dropped"
REASON_NO_MATCHING_RULE = "no_matching_rule"
REASON_DESTINATION_KIND_NOT_BUILT = "destination_kind_not_built"
BUILT_DESTINATION_KIND = "file"


def route_events(
    valid_df: DataFrame, rules_df: DataFrame, rejected_df: DataFrame | None = None
) -> tuple[DataFrame, DataFrame]:
    """Route valid events to the enabled rules that match them exactly (Default_Deny).

    A broadcast join on ``(tenant_id, product, schema)`` with plain string equality (case
    sensitive, no trimming, no wildcards) against the rules with ``enabled = true``. One routed
    row per matching (rule, destination), so an event matched by N rules fans out to N rows.
    Events whose matching destination is not a built kind (``http``/``syslog``) are dropped here.

    Returns ``(routed_df, audit_df)``. ``routed_df`` has ROUTED_COLUMNS (kind ``file`` only).
    ``audit_df`` has the processing audit columns (no payload) with decision ``dropped`` and reason
    ``no_matching_rule``, ``destination_kind_not_built`` or, when ``rejected_df`` (from
    ``parse_events``) is given, ``invalid_tenant_id`` for the rejected rows with that reason.
    """
    from event_forwarding.audit_spark import build_audit_rows

    active = F.broadcast(
        rules_df.where(F.col("enabled")).select(
            F.col("rule_index"),
            F.col("tenant_id").alias("_r_tenant_id"),
            F.col("product").alias("_r_product"),
            F.col("schema").alias("_r_schema"),
            F.col("destination_id"),
            F.col("destination_kind"),
            F.col("destination_format"),
        )
    )
    on = (
        (valid_df["tenant_id"] == F.col("_r_tenant_id"))
        & (valid_df["product"] == F.col("_r_product"))
        & (valid_df["schema"] == F.col("_r_schema"))
    )

    matched = valid_df.join(active, on, "inner")
    is_built = F.col("destination_kind") == F.lit(BUILT_DESTINATION_KIND)
    routed = matched.where(is_built).select(*ROUTED_COLUMNS)

    not_built = build_audit_rows(
        matched.where(~is_built), DECISION_DROPPED, REASON_DESTINATION_KIND_NOT_BUILT
    )
    unmatched = build_audit_rows(
        valid_df.join(active, on, "left_anti"), DECISION_DROPPED, REASON_NO_MATCHING_RULE
    )
    audit = unmatched.unionByName(not_built)
    if rejected_df is not None:
        invalid_tenant = build_audit_rows(
            rejected_df.where(F.col("reason") == F.lit(REASON_INVALID_TENANT_ID)),
            DECISION_DROPPED,
            REASON_INVALID_TENANT_ID,
        )
        audit = audit.unionByName(invalid_tenant)
    return routed, audit

# Columns of the shaped DataFrame returned by shape_events.
SHAPED_COLUMNS = (
    "event_id",
    "product",
    "schema",
    "tenant_id",
    "source_file",
    "rule_index",
    "destination_id",
    "destination_kind",
    "destination_format",
    "payload",
)


def _json_key(name: str) -> str:
    return json.dumps(name, ensure_ascii=False)


def _variant_path(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    return f"$['{escaped}']"


def _constants_json(constants: dict | None) -> str:
    """Constant fields as the inside of a JSON object, e.g. ``"region":"eu","n":1`` (config-only)."""
    if not constants:
        return ""
    return ",".join(f"{_json_key(k)}:{json.dumps(v, ensure_ascii=False)}" for k, v in constants.items())


def _payload_text(rule, data_variant: Column, data_text: Column) -> Column:
    consts = _constants_json(rule.constant_fields)
    if rule.include_fields is not None:
        parts = []
        for name in rule.include_fields:
            value = F.try_variant_get(data_variant, _variant_path(name), "variant")
            # SQL NULL = field absent (omitted); a JSON null is a variant null and is kept.
            parts.append(F.when(value.isNotNull(), F.concat(F.lit(_json_key(name) + ":"), F.to_json(value))))
        if consts:
            parts.append(F.lit(consts))
        return F.concat(F.lit("{"), F.concat_ws(",", *parts), F.lit("}"))
    if not consts:
        return data_text
    # Constants override same-named data fields: drop those keys from data before appending constants.
    names = list(rule.constant_fields)
    as_map = F.from_json(data_text, "map<string,variant>")
    kept = F.map_filter(as_map, lambda k, _v: ~k.isin(*names))
    kept_text = F.to_json(kept)
    inner = F.substring(kept_text, 2, F.length(kept_text) - 2)  # strip the outer braces
    return F.concat(
        F.lit("{"),
        F.when(F.length(inner) > 0, F.concat(inner, F.lit(","))).otherwise(F.lit("")),
        F.lit(consts),
        F.lit("}"),
    )


def shape_events(routed_df: DataFrame, config) -> DataFrame:
    """Build the Delivery_Message ``payload`` (variant) for each routed row with Column ops only.

    Per rule (looked up by ``rule_index``, same numbering as ``build_rules_df``): the listed
    top-level fields of ``data`` that are present (absent ones omitted) or all fields when the
    rule has no include list, plus the rule's constant fields (string, number or boolean literals).
    Constants are the only added columns. Envelope fields ``event_id``, ``product``, ``schema`` and
    ``tenant_id`` are kept. The ``data`` column is dropped. The loop below is over configured
    rules, never over events.

    With no include list, a constant whose name equals a top-level ``data`` field overrides it, so
    the payload never has duplicate keys (config validation only checks include/constant clashes).
    """
    data_variant = F.parse_json(F.col("data"))
    data_text = F.to_json(data_variant)
    payload_text = None
    for index, rule in reversed(list(enumerate(config.rules))):
        branch = _payload_text(rule, data_variant, data_text)
        payload_text = (
            F.when(F.col("rule_index") == F.lit(index), branch)
            if payload_text is None
            else F.when(F.col("rule_index") == F.lit(index), branch).otherwise(payload_text)
        )
    if payload_text is None:
        payload_text = data_text
    return routed_df.select(
        *[c for c in SHAPED_COLUMNS if c != "payload"], F.parse_json(payload_text).alias("payload")
    )


def build_publish_ready(text_df: DataFrame, config) -> tuple[DataFrame, DataFrame, DataFrame]:
    """Chain parse, route, shape, Isolation_Guard and publish columns (pure DataFrame logic).

    Returns ``(publish_ready_df, rejected_df, audit_df)``. ``publish_ready_df`` has string columns
    ``topic, key, value``. ``rejected_df`` has REJECTED_COLUMNS. ``audit_df`` holds the processing
    audit rows (``no_matching_rule``, ``destination_kind_not_built``, ``invalid_tenant_id``,
    ``isolation_violation``) with no payload. Used by both tests and ``foreachBatch``.
    """
    from event_forwarding.config_model import build_destination_owner_df, build_rules_df
    from event_forwarding.isolation_guard import apply_isolation_guard
    from event_forwarding.kafka_publish import add_publish_columns

    spark = text_df.sparkSession
    rules_df = build_rules_df(spark, config)
    owner_df = build_destination_owner_df(spark, config)

    valid, rejected = parse_events(text_df)
    routed, route_audit = route_events(valid, rules_df, rejected)
    shaped = shape_events(routed, config)
    guarded, violations = apply_isolation_guard(shaped, rules_df, owner_df)
    publish_ready = add_publish_columns(guarded)
    audit = route_audit.unionByName(violations)
    return publish_ready, rejected, audit
