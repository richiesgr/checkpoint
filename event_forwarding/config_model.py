"""Config_Module: loads, validates and serves the forwarding configuration.

This is driver-side code that handles only the small configuration (rules, destinations, Kafka
settings). It is the one module exempt from the Requirement 11 bans. It never sees Event data.

The configuration is read once per process (Req 2.10). All validation errors are collected and
raised together as one ``ConfigError`` that names every offending rule or destination (Req 2.9).
Secret values are never stored or echoed: destinations reference them by environment variable
name only (Req 14.8, 14.9).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

import yaml


SUPPORTED_KINDS = ("file", "http", "syslog")
SUPPORTED_FORMATS = {
    "file": ("jsonl", "parquet"),
    "http": ("json",),
    "syslog": ("json",),
}
DESTINATION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BOOTSTRAP_ENTRY = re.compile(r"[^:\s,]+:\d{1,5}")
_SECRET_WORDS = ("token", "secret", "password", "passwd", "authorization", "auth", "api_key", "apikey")

MAX_INCLUDE_FIELDS = 200
MAX_CONSTANT_FIELDS = 100
MAX_DESTINATIONS_PER_RULE = 20

_TOP_KEYS = {"kafka", "output_root", "audit_root", "rejected_root", "destinations", "rules"}
_RULE_KEYS = {
    "tenant_id", "product", "schema", "enabled",
    "include_fields", "constant_fields", "destinations",
}
_DEST_KEYS = {"id", "tenant_id", "kind", "format", "settings"}
_KAFKA_KEYS = {"bootstrap_servers"}


class ConfigError(Exception):
    """Raised for any configuration problem.

    ``category`` is one of ``missing_path``, ``not_found``, ``unreadable``, ``unparseable`` or
    ``invalid`` (validation failed). ``errors`` lists every problem found.
    """

    def __init__(self, category: str, errors: list[str]):
        self.category = category
        self.errors = list(errors)
        super().__init__(f"configuration error ({category}):\n" + "\n".join(f"- {e}" for e in self.errors))


# ---------------------------------------------------------------- typed records
@dataclass(frozen=True)
class KafkaSettings:
    bootstrap_servers: str


@dataclass(frozen=True)
class Destination:
    id: str
    tenant_id: str
    kind: str
    format: str
    settings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    tenant_id: str
    product: str
    schema: str
    destinations: tuple
    enabled: bool = True
    include_fields: tuple | None = None
    constant_fields: dict | None = None


@dataclass(frozen=True)
class Config:
    kafka: KafkaSettings
    output_root: str
    destinations: tuple
    rules: tuple
    audit_root: str = "audit"
    rejected_root: str = "rejected"

    def destination(self, destination_id: str) -> Destination | None:
        for d in self.destinations:
            if d.id == destination_id:
                return d
        return None


# ---------------------------------------------------------------- helpers
def normalize_tenant_id(value: Any) -> str | None:
    """Lowercase canonical UUID (8-4-4-4-12), or None if ``value`` is not a canonical UUID string."""
    if isinstance(value, str) and _UUID_PATTERN.fullmatch(value):
        return str(uuid.UUID(value))
    return None


def destination_output_dir(config: Config, destination: Destination) -> Path:
    """Output directory of a file destination: ``<output_root>/<tenant_id>/<destination_id>``.

    Built only from configured ownership, never from message content (Req 5.5, 8.11).
    """
    return Path(config.output_root) / destination.tenant_id / destination.id


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in _SECRET_WORDS)


def _path_problem(raw: str, root: Path, expected: Path) -> str | None:
    """Return a problem description if ``raw`` (a configured path) is not the contained expected dir."""
    if not raw:
        return "path is empty"
    win = PureWindowsPath(raw)
    if raw.startswith(("/", "\\")) or win.drive or win.is_absolute() or PurePosixPath(raw).is_absolute():
        return "path is outside the output root (absolute path)"
    if ".." in re.split(r"[\\/]", raw):
        return "path is outside the output root ('..' segment)"
    root_resolved = root.resolve()
    target = (root / raw).resolve()
    if not target.is_relative_to(root_resolved):
        return "path is outside the output root"
    if target != expected.resolve():
        return f"path must be the derived directory '{expected.relative_to(root)}'"
    return None


# ---------------------------------------------------------------- validation
class _Collector:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")


def _check_unknown(c: _Collector, where: str, raw: Mapping, allowed: set[str]) -> None:
    for key in raw:
        if key not in allowed:
            c.add(where, f"unknown key {str(key)!r}")


def _parse_kafka(c: _Collector, raw: Any) -> KafkaSettings | None:
    where = "kafka"
    if not isinstance(raw, Mapping):
        c.add(where, "must be a mapping with 'bootstrap_servers'")
        return None
    _check_unknown(c, where, raw, _KAFKA_KEYS)
    servers = raw.get("bootstrap_servers")
    if not isinstance(servers, str) or not servers.strip():
        c.add(where, "'bootstrap_servers' must be a non-empty string")
        return None
    entries = [e.strip() for e in servers.split(",")]
    bad = [e for e in entries if not _BOOTSTRAP_ENTRY.fullmatch(e)]
    if bad:
        c.add(where, f"'bootstrap_servers' entries must look like host:port, got {bad!r}")
        return None
    return KafkaSettings(bootstrap_servers=servers.strip())


def _validate_destination_settings(
    c: _Collector, where: str, kind: str, settings: dict, env: Mapping[str, str]
) -> None:
    if kind == "http":
        url = settings.get("url")
        if not isinstance(url, str) or not re.fullmatch(r"https?://\S+", url):
            c.add(where, "kind 'http' requires setting 'url' (an http:// or https:// URL)")
    elif kind == "syslog":
        host = settings.get("host")
        port = settings.get("port")
        if not isinstance(host, str) or not host.strip():
            c.add(where, "kind 'syslog' requires setting 'host'")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            c.add(where, "kind 'syslog' requires setting 'port' (integer 1-65535)")
    for key, value in settings.items():
        if not isinstance(key, str):
            c.add(where, f"setting names must be strings, got {key!r}")
            continue
        if key.endswith("_env"):
            if not isinstance(value, str) or not _ENV_NAME_PATTERN.fullmatch(value):
                c.add(where, f"setting {key!r} must be an environment variable name")
            elif not env.get(value):
                c.add(where, f"environment variable {value} (setting {key!r}) is unset or empty")
        elif _is_secret_key(key):
            c.add(where, f"setting {key!r} looks like a secret; give it as an environment variable reference '{key}_env'")


def _parse_destinations(
    c: _Collector, raw: Any, env: Mapping[str, str], output_root: str | None
) -> tuple[list[Destination], set[str]]:
    result: list[Destination] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        c.add("destinations", "must be a list")
        return result, seen
    for index, item in enumerate(raw):
        label = f"destination #{index}"
        if not isinstance(item, Mapping):
            c.add(label, "must be a mapping")
            continue
        dest_id = item.get("id")
        if isinstance(dest_id, str):
            label = f"destination {dest_id!r}"
        _check_unknown(c, label, item, _DEST_KEYS)
        ok = True
        if not isinstance(dest_id, str) or not DESTINATION_ID_PATTERN.fullmatch(dest_id):
            c.add(label, f"id {dest_id!r} must match [A-Za-z0-9_-]{{1,64}}")
            ok = False
        elif dest_id in seen:
            c.add(label, "duplicate destination id")
            ok = False
        if isinstance(dest_id, str):
            seen.add(dest_id)
        tenant = normalize_tenant_id(item.get("tenant_id"))
        if tenant is None:
            c.add(label, f"tenant_id {item.get('tenant_id')!r} is not a valid UUID")
            ok = False
        kind = item.get("kind")
        if kind not in SUPPORTED_KINDS:
            c.add(label, f"kind {kind!r} is not supported (use one of {', '.join(SUPPORTED_KINDS)})")
            ok = False
        fmt = item.get("format")
        if kind in SUPPORTED_KINDS and fmt not in SUPPORTED_FORMATS[kind]:
            c.add(label, f"format {fmt!r} is not supported for kind {kind!r} (use one of {', '.join(SUPPORTED_FORMATS[kind])})")
            ok = False
        elif kind not in SUPPORTED_KINDS and not isinstance(fmt, str):
            c.add(label, "format must be a string")
            ok = False
        settings_raw = item.get("settings", {})
        if settings_raw is None:
            settings_raw = {}
        if not isinstance(settings_raw, Mapping):
            c.add(label, "settings must be a mapping")
            ok = False
            settings_raw = {}
        settings = dict(settings_raw)
        if kind in SUPPORTED_KINDS:
            _validate_destination_settings(c, label, kind, settings, env)
        if kind == "file" and ok and output_root is not None:
            root = Path(output_root)
            expected = root / tenant / dest_id
            if not expected.resolve().is_relative_to(root.resolve()):
                c.add(label, "path is outside the output root (symbolic link)")
            elif "path" in settings:
                if not isinstance(settings["path"], str):
                    c.add(label, "setting 'path' must be a string")
                else:
                    problem = _path_problem(settings["path"], root, expected)
                    if problem:
                        c.add(label, problem)
        if ok:
            result.append(Destination(id=dest_id, tenant_id=tenant, kind=kind, format=fmt, settings=settings))
    return result, seen


def _scalar_ok(value: Any) -> bool:
    # bool is a subclass of int; both are allowed JSON scalars. None, lists and maps are not.
    return isinstance(value, (str, int, float, bool))


def _parse_rules(c: _Collector, raw: Any, destinations: list[Destination], dest_ids: set[str]) -> list[Rule]:
    result: list[Rule] = []
    if not isinstance(raw, list):
        c.add("rules", "must be a list")
        return result
    by_id = {d.id: d for d in destinations}
    for index, item in enumerate(raw):
        label = f"rule #{index}"
        if not isinstance(item, Mapping):
            c.add(label, "must be a mapping")
            continue
        label = f"rule #{index} (tenant_id={item.get('tenant_id')!r}, product={item.get('product')!r}, schema={item.get('schema')!r})"
        ok = True
        if "calculated_fields" in item:
            c.add(label, "calculated fields are not supported; use 'constant_fields' for fixed values")
            ok = False
        _check_unknown(c, label, {k: v for k, v in item.items() if k != "calculated_fields"}, _RULE_KEYS)
        tenant = normalize_tenant_id(item.get("tenant_id"))
        if tenant is None:
            c.add(label, f"tenant_id {item.get('tenant_id')!r} is not a valid UUID")
            ok = False
        for key in ("product", "schema"):
            if not isinstance(item.get(key), str) or item.get(key) == "":
                c.add(label, f"'{key}' must be a non-empty string")
                ok = False
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            c.add(label, "'enabled' must be true or false")
            ok = False

        names: list[str] = []
        include = item.get("include_fields")
        if include is not None:
            if (
                not isinstance(include, list)
                or not all(isinstance(n, str) and n for n in include)
                or not 1 <= len(include) <= MAX_INCLUDE_FIELDS
            ):
                c.add(label, f"'include_fields' must be a list of 1 to {MAX_INCLUDE_FIELDS} non-empty names")
                ok = False
            else:
                names.extend(include)
                include = tuple(include)
        constants = item.get("constant_fields")
        if constants is not None:
            if (
                not isinstance(constants, Mapping)
                or not 1 <= len(constants) <= MAX_CONSTANT_FIELDS
                or not all(isinstance(k, str) and k for k in constants)
            ):
                c.add(label, f"'constant_fields' must be a map of 1 to {MAX_CONSTANT_FIELDS} fields")
                ok = False
            elif not all(_scalar_ok(v) for v in constants.values()):
                c.add(label, "'constant_fields' values must be strings, numbers or booleans")
                ok = False
            else:
                names.extend(constants)
                constants = dict(constants)
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            c.add(label, f"duplicate field name(s) across include/constant fields: {dupes}")
            ok = False

        refs = item.get("destinations")
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_DESTINATIONS_PER_RULE or not all(
            isinstance(r, str) for r in refs
        ):
            c.add(label, f"'destinations' must be a list of 1 to {MAX_DESTINATIONS_PER_RULE} destination ids")
            ok = False
        else:
            if len(set(refs)) != len(refs):
                c.add(label, "'destinations' lists the same destination more than once")
                ok = False
            for ref in refs:
                if ref not in dest_ids:
                    c.add(label, f"references undefined destination {ref!r}")
                    ok = False
                elif ref in by_id and tenant is not None and by_id[ref].tenant_id != tenant:
                    c.add(label, f"destination {ref!r} belongs to a different tenant ({by_id[ref].tenant_id})")
                    ok = False
        if ok:
            result.append(
                Rule(
                    tenant_id=tenant,
                    product=item["product"],
                    schema=item["schema"],
                    destinations=tuple(refs),
                    enabled=enabled,
                    include_fields=include if isinstance(include, tuple) else None,
                    constant_fields=constants if isinstance(constants, dict) else None,
                )
            )
    return result


def parse_config(raw: Any, env: Mapping[str, str] | None = None) -> Config:
    """Validate an already-loaded configuration mapping. Raises ConfigError listing every problem."""
    env = os.environ if env is None else env
    c = _Collector()
    if not isinstance(raw, Mapping):
        raise ConfigError("invalid", ["top level of the configuration must be a mapping"])
    _check_unknown(c, "config", raw, _TOP_KEYS)
    kafka = _parse_kafka(c, raw.get("kafka"))
    output_root = raw.get("output_root")
    if not isinstance(output_root, str) or not output_root.strip():
        c.add("output_root", "must be a non-empty string")
        output_root = None
    roots = {}
    for key, default in (("audit_root", "audit"), ("rejected_root", "rejected")):
        value = raw.get(key, default)
        if not isinstance(value, str) or not value.strip():
            c.add(key, "must be a non-empty string")
            value = default
        roots[key] = value
    destinations, dest_ids = _parse_destinations(c, raw.get("destinations"), env, output_root)
    rules = _parse_rules(c, raw.get("rules"), destinations, dest_ids)
    if c.errors:
        raise ConfigError("invalid", c.errors)
    return Config(
        kafka=kafka,
        output_root=output_root,
        destinations=tuple(destinations),
        rules=tuple(rules),
        audit_root=roots["audit_root"],
        rejected_root=roots["rejected_root"],
    )


# ---------------------------------------------------------------- loading
def load_config(path: str | os.PathLike | None, env: Mapping[str, str] | None = None) -> Config:
    """Read the JSON or YAML file at ``path`` once and return the validated Config."""
    if path is None or str(path) == "":
        raise ConfigError("missing_path", ["no configuration file path was given"])
    p = Path(path)
    if not p.exists():
        raise ConfigError("not_found", [f"configuration file does not exist: {p}"])
    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        category = "unparseable" if isinstance(exc, UnicodeDecodeError) else "unreadable"
        raise ConfigError(category, [f"configuration file {p} cannot be read: {type(exc).__name__}"]) from exc
    try:
        raw = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigError("unparseable", [f"configuration file {p} is not valid JSON or YAML: {exc}"]) from exc
    return parse_config(raw, env)


# ---------------------------------------------------------------- serialization
def config_to_dict(config: Config) -> dict:
    """Plain-data form of a Config; ``parse_config(config_to_dict(c))`` equals ``c``."""
    destinations = []
    for d in config.destinations:
        entry: dict[str, Any] = {"id": d.id, "tenant_id": d.tenant_id, "kind": d.kind, "format": d.format}
        if d.settings:
            entry["settings"] = dict(d.settings)
        destinations.append(entry)
    rules = []
    for r in config.rules:
        entry = {
            "tenant_id": r.tenant_id,
            "product": r.product,
            "schema": r.schema,
            "enabled": r.enabled,
            "destinations": list(r.destinations),
        }
        if r.include_fields is not None:
            entry["include_fields"] = list(r.include_fields)
        if r.constant_fields is not None:
            entry["constant_fields"] = dict(r.constant_fields)
        rules.append(entry)
    return {
        "kafka": {"bootstrap_servers": config.kafka.bootstrap_servers},
        "output_root": config.output_root,
        "audit_root": config.audit_root,
        "rejected_root": config.rejected_root,
        "destinations": destinations,
        "rules": rules,
    }


def serialize_config(config: Config, fmt: str = "yaml") -> str:
    """Serialize to JSON or YAML text."""
    data = config_to_dict(config)
    if fmt == "json":
        return json.dumps(data, indent=2, sort_keys=False)
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    raise ValueError(f"unsupported serialization format {fmt!r}")


# ---------------------------------------------------------------- small Spark DataFrames
def _literal_df(spark, rows, schema):
    """DataFrame of ``rows`` built from Spark literals in one partition.

    ``spark.createDataFrame(list)`` goes through a Python RDD, so every task of every scan starts a
    Python worker. On Windows there is no reusable worker daemon, which costs about 0.5 s per task.
    Literals keep these small config tables inside the JVM. Config values only, never Event data.
    """
    from pyspark.sql import functions as F

    one_row = spark.range(0, 1, 1, 1)
    if not rows:
        return one_row.select(
            *[F.lit(None).cast(f.dataType).alias(f.name) for f in schema.fields]
        ).where(F.lit(False))
    structs = [
        F.struct(*[F.lit(v).cast(f.dataType).alias(f.name) for v, f in zip(row, schema.fields)])
        for row in rows
    ]
    return one_row.select(F.inline(F.array(*structs)))

def build_rules_df(spark, config: Config):
    """Small broadcastable DataFrame: one row per (rule, destination).

    Columns: rule_index, tenant_id, product, schema, enabled, destination_id, destination_kind,
    destination_format. Built from configuration only (never from Event data).
    """
    from pyspark.sql.types import BooleanType, IntegerType, StringType, StructField, StructType

    schema = StructType(
        [
            StructField("rule_index", IntegerType(), False),
            StructField("tenant_id", StringType(), False),
            StructField("product", StringType(), False),
            StructField("schema", StringType(), False),
            StructField("enabled", BooleanType(), False),
            StructField("destination_id", StringType(), False),
            StructField("destination_kind", StringType(), False),
            StructField("destination_format", StringType(), False),
        ]
    )
    rows = []
    for index, rule in enumerate(config.rules):
        for dest_id in rule.destinations:
            dest = config.destination(dest_id)
            rows.append((index, rule.tenant_id, rule.product, rule.schema, rule.enabled, dest_id, dest.kind, dest.format))
    return _literal_df(spark, rows, schema)


def build_destination_owner_df(spark, config: Config):
    """Small DataFrame (destination_id, tenant_id, output_path, kind, format) of configured owners."""
    from pyspark.sql.types import StringType, StructField, StructType

    schema = StructType(
        [
            StructField("destination_id", StringType(), False),
            StructField("tenant_id", StringType(), False),
            StructField("output_path", StringType(), True),
            StructField("kind", StringType(), False),
            StructField("format", StringType(), False),
        ]
    )
    rows = []
    for d in config.destinations:
        path = str(destination_output_dir(config, d)) if d.kind == "file" else None
        rows.append((d.id, d.tenant_id, path, d.kind, d.format))
    return _literal_df(spark, rows, schema)
