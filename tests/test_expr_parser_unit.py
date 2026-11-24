"""Unit tests for the restricted-grammar expression parser (task 2.1)."""
from __future__ import annotations

import pytest
from pyspark.sql import functions as F

from event_forwarding.expr_parser import (
    BinOp,
    ExpressionError,
    Field,
    Neg,
    Num,
    compile_expression,
    field_names,
    parse_expression,
)


def test_precedence_and_left_associativity():
    assert parse_expression("1 + 2 * 3") == BinOp("+", Num("1"), BinOp("*", Num("2"), Num("3")))
    assert parse_expression("8 - 3 - 2") == BinOp("-", BinOp("-", Num("8"), Num("3")), Num("2"))
    assert parse_expression("8 / 4 / 2") == BinOp("/", BinOp("/", Num("8"), Num("4")), Num("2"))
    assert parse_expression("-(a + 1)") == Neg(BinOp("+", Field("a"), Num("1")))
    assert parse_expression("- -a") == Neg(Neg(Field("a")))


def test_field_names():
    assert field_names("risk * 2 + risk - other") == ["risk", "other"]


@pytest.mark.parametrize(
    "text",
    ["", "   ", "1 +", "(1", "1)", "1 2", "a b", "2 ** 3", "1.", ".5", "1e3", "a.b", "a[0]",
     "__import__('os')", "f(1)", "a % 2", "1 + * 2", "'x'", "(" * 60 + "1" + ")" * 60, "1.2.3", "12ab"],
)
def test_invalid_input_raises(text):
    with pytest.raises(ExpressionError):
        parse_expression(text)


def test_non_string_and_too_long_rejected():
    with pytest.raises(ExpressionError):
        parse_expression(None)
    with pytest.raises(ExpressionError):
        parse_expression("1+" * 300 + "1")


def test_column_results(spark):
    rows = [
        ('{"risk": 3, "d": 2.5, "z": 0}',),   # normal
        ('{"risk": "3"}',),                    # string, not a number
        ('{"risk": true}',),                   # boolean
        ('{"risk": null}',),                   # null
        ('{}',),                               # missing
        ('not json',),                         # unparsable
        ('{"risk": 1e999}',),                  # non-finite
    ]
    df = spark.createDataFrame(rows, ["data"])
    out = df.select(
        compile_expression("risk * 2").alias("times2"),
        compile_expression("(risk + 1) * -d / 2 - 1").alias("mixed"),
        compile_expression("risk / z").alias("div0"),
        compile_expression("7 / 2").alias("literal"),
        compile_expression("data_col_missing + 1", F.col("data")).alias("missing"),
    ).collect()
    assert out[0]["times2"] == 6.0
    assert out[0]["mixed"] == (3 + 1) * -2.5 / 2 - 1
    assert out[0]["div0"] is None
    assert out[0]["literal"] == 3.5
    assert all(r["missing"] is None for r in out)
    assert all(r["times2"] is None for r in out[1:])
