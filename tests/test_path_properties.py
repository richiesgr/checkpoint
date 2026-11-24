"""Property 10: the output path depends only on configured ownership, never on message content."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

from event_forwarding.config_model import Config, Destination, KafkaSettings, Rule
from event_forwarding.file_sink_write import process_micro_batch

A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
# dest-c is configured but never receives a message: its directory must stay untouched.
OWNERS = {"dest-a1": A, "dest-a2": A, "dest-b1": B, "dest-c": B}
USED = ("dest-a1", "dest-a2", "dest-b1")

tampered_text = st.one_of(
    st.text(max_size=20),
    st.sampled_from(["..", "../..", "../../x", "a/b", "..\\..\\x", "/abs", "C:\\evil", "dest-b1", B, A]),
)


def _config(output_root: Path, audit_root: Path) -> Config:
    return Config(
        kafka=KafkaSettings(bootstrap_servers="localhost:9092"),
        output_root=str(output_root),
        audit_root=str(audit_root),
        destinations=tuple(
            Destination(id=d, tenant_id=t, kind="file", format="jsonl", settings={})
            for d, t in OWNERS.items()
        ),
        rules=(Rule(tenant_id=A, product="sales", schema="purchase", destinations=("dest-a1",)),),
    )


def _rows(plan, product, schema, payload_text):
    rows = []
    for idx, dest in enumerate(plan):
        tenant = OWNERS[dest]
        value = json.dumps(
            {
                "tenant_id": tenant,
                "destination_id": dest,
                "product": product,
                "schema": schema,
                "event_id": f"ev-{idx}",
                "payload": {"note": payload_text, payload_text: idx},
            }
        )
        rows.append((f"{tenant}|{dest}", value, "delivery.file", 0, idx))
    return rows


def _run(spark, make_kafka_rows, plan, product, schema, payload_text):
    """Run one micro-batch in a fresh temp tree; return (relative file set, dirs under output_root, dlq count)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        output_root = base / "out"
        output_root.mkdir()
        config = _config(output_root, base / "audit")
        captured = []
        process_micro_batch(
            make_kafka_rows(_rows(plan, product, schema, payload_text)),
            1,
            config,
            publish_dlq=lambda df: captured.append(df.count()),
        )
        files = {p.relative_to(output_root).as_posix() for p in output_root.rglob("*") if p.is_file()}
        top = {p.relative_to(output_root).as_posix() for p in output_root.rglob("*") if p.is_dir()}
        # Nothing may escape output_root: only the audit tree sits beside it.
        siblings = {p.name for p in base.iterdir()} - {"out", "audit"}
        return files, top, captured, siblings


# Feature: event-forwarding, Property 10: Output path depends only on configured ownership, never on message content
# **Validates: Requirements 5.1, 5.5, 8.8, 8.11**
@settings(
    max_examples=2,
    deadline=None,
    # Each example is several Spark jobs; shrinking a failure would run for many minutes.
    phases=(Phase.explicit, Phase.reuse, Phase.generate),
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    plan=st.lists(st.sampled_from(USED), min_size=1, max_size=6),
    product=tampered_text,
    schema=tampered_text,
    payload_text=tampered_text,
)
def test_output_path_depends_only_on_configured_ownership(
    spark, make_kafka_rows, plan, product, schema, payload_text
):
    plan = list(plan) + list(USED)  # every used destination gets at least one row
    tampered, dirs, dlq, siblings = _run(spark, make_kafka_rows, plan, product, schema, payload_text)
    baseline, _dirs, _dlq, _sib = _run(spark, make_kafka_rows, plan, "sales", "purchase", "plain")

    # Tampered content does not change any path.
    assert tampered == baseline
    assert tampered, "expected files to be written"
    assert dlq == [0] or all(n == 0 for n in dlq)
    assert not siblings

    # Every file lies directly under <tenant_id>/<destination_id>/ of a destination that received rows.
    allowed = {f"{OWNERS[d]}/{d}" for d in USED}
    for rel in tampered:
        parent, _, name = rel.rpartition("/")
        assert parent in allowed, rel
        assert name.startswith(parent.rpartition("/")[2] + "-") and name.endswith(".jsonl")
    assert {rel.rpartition("/")[0] for rel in tampered} == allowed

    # No other Destination directory is touched: no file lands outside a used destination's
    # directory (dest-c gets none), and every directory is a configured destination path or its
    # own .tmp staging dir. The write step may create an idle destination's own empty directory.
    configured = {f"{t}/{d}" for d, t in OWNERS.items()}
    expected_dirs = {A, B} | configured | {f"{p}/.tmp" for p in configured}
    assert dirs <= expected_dirs, dirs - expected_dirs
    assert not any(rel.startswith(f"{B}/dest-c/") for rel in tampered)
