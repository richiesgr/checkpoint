"""Unit tests for Run_Summary, Worker_Summary and the broker probe (Req 9.5, 9.6, 15.7)."""
import json
import socket

import pytest

from event_forwarding.kafka_util import (
    BrokerUnreachableError,
    parse_bootstrap_servers,
    probe_bootstrap_servers,
    require_broker,
)
from event_forwarding.summary_spark import build_run_summary, write_run_summary
from event_forwarding.summary_worker import build_worker_summary, write_worker_summary

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"


def _rows(df):
    return {(r["scope"], r["tenant_id"], r["destination_id"]): r.asDict() for r in df.collect()}


def test_run_summary_counts_total_and_per_tenant_destination(spark, tmp_path):
    read = spark.createDataFrame([(A,), (A,), (B,), (None,)], "tenant_id string")
    rejected = spark.createDataFrame([(None,)], "tenant_id string")
    dropped = spark.createDataFrame([(B, None)], "tenant_id string, destination_id string")
    published = spark.createDataFrame(
        [("delivery.file", f"{A}|dest-a".encode(), b"{}"), ("delivery.file", f"{A}|dest-a".encode(), b"{}")],
        "topic string, key binary, value binary",
    )
    summary = build_run_summary(read, rejected, dropped, published)
    rows = _rows(summary)
    assert rows[("total", None, None)]["read"] == 4
    assert rows[("total", None, None)]["rejected"] == 1
    assert rows[("total", None, None)]["dropped"] == 1
    assert rows[("total", None, None)]["published"] == 2
    assert rows[("tenant_destination", A, "dest-a")]["published"] == 2
    assert rows[("tenant_destination", A, None)]["read"] == 2
    assert all(isinstance(v, int) and v >= 0 for r in rows.values() for k, v in r.items() if k in ("read", "rejected", "dropped", "published"))

    write_run_summary(summary, tmp_path)
    parts = list((tmp_path / "run_summary").glob("part-*.json"))
    assert len(parts) == 1
    assert json.loads(parts[0].read_text().splitlines()[0])["scope"] in ("total", "tenant_destination")


def test_run_summary_of_empty_input_is_zero(spark):
    empty = spark.createDataFrame([], "tenant_id string")
    published = spark.createDataFrame([], "topic string, key binary, value binary")
    rows = _rows(build_run_summary(empty, empty, empty, published))
    assert rows[("total", None, None)]["read"] == 0
    assert rows[("total", None, None)]["published"] == 0


def test_worker_summary_counts_delivered_and_dead_lettered(spark, tmp_path):
    audit = spark.createDataFrame(
        [
            (A, "dest-a", "delivered"),
            (A, "dest-a", "delivered"),
            (A, "dest-a", "dead-lettered"),
            (B, "dest-b", "delivered"),
            (B, "dest-b", "dropped"),
        ],
        "tenant_id string, destination_id string, decision string",
    )
    summary = build_worker_summary(audit)
    rows = _rows(summary)
    assert (rows[("total", None, None)]["delivered"], rows[("total", None, None)]["dead_lettered"]) == (3, 1)
    assert rows[("tenant_destination", A, "dest-a")]["delivered"] == 2
    assert rows[("tenant_destination", B, "dest-b")]["dead_lettered"] == 0
    write_worker_summary(summary, tmp_path)
    assert len(list((tmp_path / "worker_summary").glob("part-*.json"))) == 1


def test_worker_summary_of_empty_input_is_zero(spark):
    empty = spark.createDataFrame([], "tenant_id string, destination_id string, decision string")
    total = _rows(build_worker_summary(empty))[("total", None, None)]
    assert (total["delivered"], total["dead_lettered"]) == (0, 0)


def test_probe_reaches_listening_socket_and_fails_on_closed_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert probe_bootstrap_servers(f"127.0.0.1:{port}")
    assert not probe_bootstrap_servers(f"127.0.0.1:{port}", timeout=0.5)
    with pytest.raises(BrokerUnreachableError, match=f"127.0.0.1:{port}"):
        require_broker(f"127.0.0.1:{port}", timeout=0.5)


def test_parse_bootstrap_servers_rejects_malformed():
    assert parse_bootstrap_servers("a:1, b:2") == [("a", 1), ("b", 2)]
    for bad in ("", "nohost", "h:abc", "h:70000"):
        with pytest.raises(ValueError):
            parse_bootstrap_servers(bad)
