"""Event envelope schema. ``data`` is kept as raw JSON text."""
from __future__ import annotations

from pyspark.sql.types import StringType, StructField, StructType

ENVELOPE_FIELDS = ("event_id", "product", "schema", "tenant_id", "data")

envelope_schema = StructType(
    [
        StructField("event_id", StringType()),
        StructField("product", StringType()),
        StructField("schema", StringType()),
        StructField("tenant_id", StringType()),
        StructField("data", StringType()),  # raw JSON text
    ]
)

# Reason codes written by the parse stage.
REASON_MALFORMED_JSON = "malformed_json"
REASON_MISSING_ENVELOPE_FIELD = "missing_envelope_field"
REASON_INVALID_DATA = "invalid_data"
REASON_INVALID_TENANT_ID = "invalid_tenant_id"

# Canonical UUID (8-4-4-4-12 hex), compared without trimming. Upper case is accepted and lower-cased.
UUID_REGEX = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
