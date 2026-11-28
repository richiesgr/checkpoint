"""Static AST scan of the Event_Path modules (Req 11.11, 11.12, 11.13).

Bans on the event path: pandas / pyarrow imports, collect/toPandas/take/head
calls, Python UDFs (udf, pandas_udf, mapInPandas, applyInPandasWithState)
and eval/exec. Only config_model.py is exempt (it is not in the scan list).
"""
import ast
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parent.parent / "event_forwarding"

EVENT_PATH_MODULES = [
    "pipeline.py",
    "isolation_guard.py",
    "kafka_publish.py",
    "audit_spark.py",
    "summary_spark.py",
    "envelope_schema.py",
    "sink_transform.py",
    "file_sink_write.py",
    "audit_worker.py",
    "summary_worker.py",
    "cli_spark.py",
    "cli_file_sink_worker.py",
    "log_util.py",
]
ALLOW_LISTED = {"config_model.py"}

BANNED_IMPORT_ROOTS = {"pandas", "pyarrow"}
BANNED_METHOD_CALLS = {"collect", "toPandas", "take", "head"}
BANNED_UDF_NAMES = {"udf", "pandas_udf", "mapInPandas", "applyInPandasWithState"}
BANNED_BUILTIN_CALLS = {"eval", "exec"}


def scan_source(source: str, filename: str = "<snippet>") -> list[str]:
    """Return a list of violation descriptions found in the source."""
    tree = ast.parse(source, filename=filename)
    found: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_IMPORT_ROOTS:
                    found.append(f"{filename}:{line} import {alias.name}")
                if alias.name.split(".")[-1] in BANNED_UDF_NAMES:
                    found.append(f"{filename}:{line} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BANNED_IMPORT_ROOTS:
                found.append(f"{filename}:{line} from {node.module} import")
            for alias in node.names:
                if alias.name in BANNED_UDF_NAMES or alias.name in BANNED_IMPORT_ROOTS:
                    found.append(f"{filename}:{line} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in BANNED_METHOD_CALLS:
                found.append(f"{filename}:{line} call .{func.attr}()")
            elif isinstance(func, ast.Name) and func.id in BANNED_BUILTIN_CALLS:
                found.append(f"{filename}:{line} call {func.id}()")
        if isinstance(node, ast.Name) and node.id in BANNED_UDF_NAMES:
            found.append(f"{filename}:{line} use of {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_UDF_NAMES:
            found.append(f"{filename}:{line} use of .{node.attr}")
    return found


def existing_modules() -> list[Path]:
    return [
        PKG_DIR / name
        for name in EVENT_PATH_MODULES
        if name not in ALLOW_LISTED and (PKG_DIR / name).is_file()
    ]


def test_module_list_is_not_empty():
    assert existing_modules(), "no Event_Path module found to scan"


def test_event_path_modules_have_no_banned_constructs():
    violations: list[str] = []
    for path in existing_modules():
        violations += scan_source(path.read_text(encoding="utf-8"), path.name)
    assert not violations, "banned constructs on the Event_Path:\n" + "\n".join(violations)


def test_config_model_is_the_only_allow_listed_module():
    assert ALLOW_LISTED == {"config_model.py"}
    assert not ALLOW_LISTED & set(EVENT_PATH_MODULES)


@pytest.mark.parametrize(
    "snippet",
    [
        "import pandas",
        "import pandas as pd",
        "from pandas import DataFrame",
        "import pyarrow.parquet as pq",
        "from pyarrow import parquet",
        "df.collect()",
        "df.toPandas()",
        "df.take(3)",
        "df.head()",
        "from pyspark.sql.functions import udf",
        "from pyspark.sql.functions import pandas_udf",
        "f = udf(lambda x: x)",
        "F.udf(lambda x: x)",
        "@pandas_udf('int')\ndef f(s): return s",
        "df.mapInPandas(f, schema)",
        "df.groupBy('a').applyInPandasWithState(f, 's', 'o', 'update', 'none')",
        "eval('1+1')",
        "exec('x = 1')",
    ],
)
def test_scanner_flags_each_banned_construct(snippet):
    assert scan_source(snippet), f"scanner missed: {snippet!r}"


def test_scanner_accepts_clean_code():
    clean = "from pyspark.sql import functions as F\ndf = df.filter(F.col('a') == 1)\n"
    assert scan_source(clean) == []
