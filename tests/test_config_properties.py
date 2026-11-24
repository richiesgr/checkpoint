"""Property 2: Config_Store rejects every defect it is supposed to reject.

One defect is planted at a time into an otherwise valid two-tenant configuration. The whole
configuration must be rejected and the error must name the offender.

**Validates: Requirements 2.5, 2.6, 2.7, 2.8, 5.10, 8.3, 8.4, 14.8, 14.9**
"""
from __future__ import annotations

import copy
import uuid

import pytest
from hypothesis import given, settings, strategies as st

from event_forwarding.config_model import ConfigError, normalize_tenant_id, parse_config

TENANT_A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
TENANT_B = "9d4e1a72-3c5b-4f8e-a1d2-6b7c8e9f0a13"
ENV_VAR = "TENANT_B_HTTP_TOKEN"
GOOD_ENV = {ENV_VAR: "not-a-real-secret"}


def base_config() -> dict:
    return {
        "kafka": {"bootstrap_servers": "localhost:9092"},
        "output_root": "output",
        "destinations": [
            {"id": "dest-a-file", "tenant_id": TENANT_A, "kind": "file", "format": "jsonl"},
            {"id": "dest-b-file", "tenant_id": TENANT_B, "kind": "file", "format": "jsonl"},
            {
                "id": "dest-b-http",
                "tenant_id": TENANT_B,
                "kind": "http",
                "format": "json",
                "settings": {"url": "https://siem.example.com/events", "auth_token_env": ENV_VAR},
            },
        ],
        "rules": [
            {"tenant_id": TENANT_A, "product": "sales", "schema": "purchase", "destinations": ["dest-a-file"]},
            {
                "tenant_id": TENANT_B,
                "product": "sales",
                "schema": "purchase",
                "destinations": ["dest-b-file", "dest-b-http"],
            },
        ],
    }


_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
bad_tenants = st.text(alphabet=_SAFE + "-_ ", min_size=1, max_size=40).filter(
    lambda s: normalize_tenant_id(s) is None
)
bad_ids = st.one_of(
    st.text(alphabet=_SAFE + "-_ .!@#$%", min_size=1, max_size=20).filter(
        lambda s: not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in s)
    ),
    st.text(alphabet=_SAFE, min_size=65, max_size=80),
)
new_names = st.text(alphabet=_SAFE + "-_", min_size=1, max_size=30).map(lambda s: "x" + s)
bad_kinds = st.text(alphabet=_SAFE, min_size=1, max_size=12).filter(lambda s: s not in ("file", "http", "syslog"))
bad_paths = st.sampled_from(["../escape", "/etc/out", "\\\\server\\share", "C:\\out", "a/../../b", "..\\x"])
env_names = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=3, max_size=20)
valid_uuids = st.uuids().map(str)


def _plant_bad_destination_tenant(cfg, data):
    bad = data.draw(bad_tenants)
    cfg["destinations"][0]["tenant_id"] = bad
    return "dest-a-file"


def _plant_bad_rule_tenant(cfg, data):
    bad = data.draw(bad_tenants)
    cfg["rules"][0]["tenant_id"] = bad
    return repr(bad)


def _plant_ownership_mismatch(cfg, data):
    cfg["rules"][0]["destinations"] = ["dest-b-file"]
    return "dest-b-file"


def _plant_undefined_destination(cfg, data):
    name = data.draw(new_names)
    cfg["rules"][0]["destinations"].append(name)
    return name


def _plant_bad_kind(cfg, data):
    cfg["destinations"][0]["kind"] = data.draw(bad_kinds)
    return "dest-a-file"


def _plant_bad_format(cfg, data):
    fmt = data.draw(st.sampled_from(["csv", "xml", "avro", "", None, 7]))
    which = data.draw(st.sampled_from([0, 2]))
    cfg["destinations"][which]["format"] = fmt
    return cfg["destinations"][which]["id"]


def _plant_duplicate_id(cfg, data):
    cfg["destinations"][1]["id"] = "dest-a-file"
    cfg["destinations"][1]["tenant_id"] = data.draw(st.sampled_from([TENANT_A, TENANT_B]))
    return "dest-a-file"


def _plant_malformed_id(cfg, data):
    bad = data.draw(bad_ids)
    cfg["destinations"][0]["id"] = bad
    return bad


def _plant_escaping_path(cfg, data):
    cfg["destinations"][0]["settings"] = {"path": data.draw(bad_paths)}
    return "dest-a-file"


def _plant_unset_secret(cfg, data):
    name = data.draw(env_names)
    cfg["destinations"][2]["settings"]["auth_token_env"] = name
    return name


DEFECTS = [
    _plant_bad_destination_tenant,
    _plant_bad_rule_tenant,
    _plant_ownership_mismatch,
    _plant_undefined_destination,
    _plant_bad_kind,
    _plant_bad_format,
    _plant_duplicate_id,
    _plant_malformed_id,
    _plant_escaping_path,
    _plant_unset_secret,
]


def test_base_config_is_valid():
    cfg = parse_config(base_config(), env=GOOD_ENV)
    assert {d.id for d in cfg.destinations} == {"dest-a-file", "dest-b-file", "dest-b-http"}


@settings(max_examples=100, deadline=None)
@given(data=st.data(), defect=st.sampled_from(DEFECTS), empty_secret=st.booleans())
def test_every_planted_defect_rejects_whole_config(data, defect, empty_secret):
    cfg = copy.deepcopy(base_config())
    offender = defect(cfg, data)
    env = dict(GOOD_ENV)
    if defect is _plant_unset_secret and empty_secret:
        env[cfg["destinations"][2]["settings"]["auth_token_env"]] = ""

    with pytest.raises(ConfigError) as info:
        parse_config(cfg, env=env)

    assert info.value.category == "invalid"
    assert any(offender in e for e in info.value.errors), (offender, info.value.errors)
    # the secret value itself is never echoed
    assert GOOD_ENV[ENV_VAR] not in str(info.value)


@settings(max_examples=100, deadline=None)
@given(tenant=valid_uuids, upper=st.booleans())
def test_valid_uuid_forms_are_accepted_and_normalized(tenant, upper):
    cfg = base_config()
    cfg["destinations"][0]["tenant_id"] = tenant.upper() if upper else tenant
    cfg["rules"][0]["tenant_id"] = tenant
    parsed = parse_config(cfg, env=GOOD_ENV)
    assert parsed.destinations[0].tenant_id == str(uuid.UUID(tenant))
