"""Landing-zone generator for the end-to-end run (task 13.1). Not Event_Path code.

Writes, for one run: a config for 10 tenants, 1,170 landing-zone lines in 5 JSONL files
(1,000 matched, 150 unmatched, 20 bad), and ``expected.json`` with what the pipeline must output.

Rules of the generated config:

    T0..T3  sales/purchase  include [order_id, amount] (drops internal_note)  -> dest-tN-file
            security/alert  constant region = "eu"                           -> dest-tN-file
    T4, T5  sales/purchase  all fields                                       -> dest-tN-file
    T6      sales/purchase  all fields, fan-out to two destinations          -> dest-t6-file-a, dest-t6-file-b
    T7      sales/purchase  enabled: false                                   -> dest-t7-file (never written)
    T8      no rule, one destination                                         -> dest-t8-file (never written)
    T9      no rule, no destination
    identity/login is never forwarded.

Pattern: matched events go round-robin over the 11 enabled (tenant, product, schema) keys, and
unmatched events round-robin over the keys without an enabled rule, so the expected count per
destination follows from the counts. Field values come from the event number ``n`` plus a seeded
random amount and note. Line order is shuffled with the fixed seed before splitting into files.

Run by hand:  .\\.venv\\Scripts\\python.exe -m tests.end_to_end.generator
"""
from __future__ import annotations

import argparse
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tests.end_to_end import settings as s

PURCHASE_INCLUDE = ["order_id", "amount"]
REGION = {"region": "eu"}


@dataclass
class Run:
    run_id: str
    run_dir: Path
    paths: dict[str, Path]
    config: dict
    # destination_id -> list of expected output lines (event_id, product, schema, tenant_id, payload)
    expected_files: dict[str, list[dict]] = field(default_factory=dict)
    # expected Rejected_Output: (line, reason)
    expected_rejected: list[tuple[str, str]] = field(default_factory=list)
    unmatched_ids: list[str] = field(default_factory=list)


def build_config(paths: dict[str, Path]) -> dict:
    t = [s.tenant_id(i) for i in range(s.TENANT_COUNT)]
    destinations, rules = [], []

    def dest(dest_id: str, tenant: str) -> None:
        destinations.append({"id": dest_id, "tenant_id": tenant, "kind": "file", "format": "jsonl"})

    for i in range(4):
        dest(f"dest-t{i}-file", t[i])
        rules.append({"tenant_id": t[i], "product": "sales", "schema": "purchase", "enabled": True,
                      "include_fields": PURCHASE_INCLUDE, "destinations": [f"dest-t{i}-file"]})
        rules.append({"tenant_id": t[i], "product": "security", "schema": "alert", "enabled": True,
                      "constant_fields": dict(REGION), "destinations": [f"dest-t{i}-file"]})
    for i in (4, 5):
        dest(f"dest-t{i}-file", t[i])
        rules.append({"tenant_id": t[i], "product": "sales", "schema": "purchase", "enabled": True,
                      "destinations": [f"dest-t{i}-file"]})
    dest("dest-t6-file-a", t[6])
    dest("dest-t6-file-b", t[6])
    rules.append({"tenant_id": t[6], "product": "sales", "schema": "purchase", "enabled": True,
                  "destinations": ["dest-t6-file-a", "dest-t6-file-b"]})
    dest("dest-t7-file", t[7])
    rules.append({"tenant_id": t[7], "product": "sales", "schema": "purchase", "enabled": False,
                  "destinations": ["dest-t7-file"]})
    dest("dest-t8-file", t[8])

    return {
        "kafka": {"bootstrap_servers": s.BOOTSTRAP_SERVERS},
        "output_root": str(paths["out"]),
        "audit_root": str(paths["audit"]),
        "rejected_root": str(paths["rejected"]),
        "destinations": destinations,
        "rules": rules,
    }


def _shape(rule: dict, data: dict) -> dict:
    """Expected payload of ``data`` under ``rule`` (include list, then constants)."""
    include = rule.get("include_fields")
    payload = {k: data[k] for k in include if k in data} if include else dict(data)
    payload.update(rule.get("constant_fields") or {})
    return payload


def _data(product: str, n: int, rng: random.Random) -> dict:
    if product == "sales":
        return {"order_id": f"o{n}", "amount": rng.randint(1, 500), "internal_note": f"private-{rng.randint(0, 9999)}"}
    if product == "security":
        return {"alert_id": f"a{n}", "risk": n % 10}
    return {"user": f"u{n % 20}", "result": "success" if n % 2 == 0 else "failure"}


def _bad_lines(run_id: str) -> list[tuple[str, str]]:
    """BAD_LINES lines cycling over five defects, each with its expected rejection reason."""
    good_tenant = s.tenant_id(0)
    makers = [
        lambda i: (f'{{"event_id":"{run_id}-bad-{i}","product":"sales","schema":"purchase"', "malformed_json"),
        lambda i: (json.dumps({"event_id": f"{run_id}-bad-{i}", "product": "sales", "schema": "purchase",
                               "tenant_id": "", "data": {"order_id": "x"}}), "invalid_tenant_id"),
        lambda i: (json.dumps({"event_id": f"{run_id}-bad-{i}", "product": "sales", "schema": "purchase",
                               "tenant_id": "not-a-uuid", "data": {"order_id": "x"}}), "invalid_tenant_id"),
        lambda i: (json.dumps({"event_id": f"{run_id}-bad-{i}", "product": "sales",
                               "tenant_id": good_tenant, "data": {"order_id": "x"}}), "missing_envelope_field"),
        lambda i: (json.dumps({"event_id": f"{run_id}-bad-{i}", "product": "sales", "schema": "purchase",
                               "tenant_id": good_tenant, "data": [1, 2, 3]}), "invalid_data"),
    ]
    return [makers[i % len(makers)](i) for i in range(s.BAD_LINES)]


def generate(run_dir: Path | None = None, run_id: str | None = None) -> Run:
    run_id = run_id or uuid.uuid4().hex[: s.RUN_ID_LENGTH]
    run_dir = Path(run_dir) if run_dir else s.run_dir_for(run_id)
    paths = s.run_paths(run_dir)
    paths["landing"].mkdir(parents=True, exist_ok=False)
    rng = random.Random(s.SEED)

    config = build_config(paths)
    run = Run(run_id, run_dir, paths, config)
    enabled = [r for r in config["rules"] if r["enabled"]]
    enabled_keys = {(r["tenant_id"], r["product"], r["schema"]) for r in enabled}
    all_keys = [(s.tenant_id(i), p, sc) for i in range(s.TENANT_COUNT) for p, sc in s.PRODUCT_SCHEMAS]
    matched_keys = [k for k in all_keys if k in enabled_keys]
    unmatched_keys = [k for k in all_keys if k not in enabled_keys]

    lines: list[str] = []
    n = 0
    for count, keys in ((s.MATCHED_EVENTS, matched_keys), (s.UNMATCHED_EVENTS, unmatched_keys)):
        for i in range(count):
            tenant, product, schema = keys[i % len(keys)]
            event = {"event_id": f"{run_id}-{n:05d}", "product": product, "schema": schema,
                     "tenant_id": tenant, "data": _data(product, n, rng)}
            lines.append(json.dumps(event))
            rules = [r for r in enabled if (r["tenant_id"], r["product"], r["schema"]) == (tenant, product, schema)]
            if not rules:
                run.unmatched_ids.append(event["event_id"])
            for rule in rules:
                for dest_id in rule["destinations"]:
                    run.expected_files.setdefault(dest_id, []).append(
                        {"event_id": event["event_id"], "product": product, "schema": schema,
                         "tenant_id": tenant, "payload": _shape(rule, event["data"])}
                    )
            n += 1

    run.expected_rejected = _bad_lines(run_id)
    lines.extend(line for line, _ in run.expected_rejected)
    rng.shuffle(lines)

    size = -(-len(lines) // s.LANDING_FILES)
    for f in range(s.LANDING_FILES):
        chunk = lines[f * size:(f + 1) * size]
        (paths["landing"] / f"events-{f + 1}.jsonl").write_text("\n".join(chunk) + "\n", encoding="utf-8")

    paths["config"].write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    paths["expected"].write_text(json.dumps({
        "run_id": run_id,
        "lines": len(lines),
        "matched_events": s.MATCHED_EVENTS,
        "unmatched_events": s.UNMATCHED_EVENTS,
        "bad_lines": s.BAD_LINES,
        "delivered_records": sum(len(v) for v in run.expected_files.values()),
        "records_per_destination": {k: len(v) for k, v in sorted(run.expected_files.items())},
        "never_written_destinations": sorted(
            d["id"] for d in config["destinations"] if d["id"] not in run.expected_files
        ),
        "rejected_reasons": sorted({r for _, r in run.expected_rejected}),
    }, indent=2), encoding="utf-8")
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one end-to-end run directory.")
    parser.add_argument("--run-dir", default=None, help="default: %%TEMP%%\\e2e-<run_id> (or $E2E_RUN_ROOT\\e2e-<run_id>)")
    args = parser.parse_args(argv)
    run = generate(Path(args.run_dir) if args.run_dir else None)
    print(f"run_id={run.run_id}")
    for name, path in run.paths.items():
        print(f"{name:16} {path}")
    print(run.paths["expected"].read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
