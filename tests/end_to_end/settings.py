"""Fixed settings of the end-to-end run (task 13): counts, seed, tenants, paths.

Every run writes into its own directory ``<run root>/e2e-<run_id>/``. The run root is the system
temp directory (the Windows equivalent of /tmp, ``%TEMP%``), or ``E2E_RUN_ROOT`` when set. It is
outside the repository because Spark's staging paths under the run directory come close to the
Windows 260-character path limit (long paths are disabled by default). Layout:

    config.yaml        generated config (rules and destinations of the 10 tenants)
    expected.json      what the run must produce (counts per destination, rejected reasons)
    landing/           5 landing-zone files events-1.jsonl .. events-5.jsonl
    ckpt-processing/   Checkpoint_Location of the processing job
    ckpt-sink/         Sink_Checkpoint_Location of the File_Sink_Worker
    out/               customer files: out/<tenant_id>/<destination_id>/*.jsonl
    audit/             processing and delivery audit, Run_Summary, Worker_Summary
    rejected/          Rejected_Output of the processing job
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
RUNS_DIR = Path(os.environ.get("E2E_RUN_ROOT") or tempfile.gettempdir())
RUN_ID_LENGTH = 8


def run_dir_for(run_id: str) -> Path:
    return RUNS_DIR / f"e2e-{run_id}"

SEED = 20240613

# Event counts per run (decided with the user).
MATCHED_EVENTS = 1000  # valid, matched by at least one enabled rule: delivered
UNMATCHED_EVENTS = 150  # valid, no enabled rule: dropped, never delivered
BAD_LINES = 20  # malformed or invalid lines: rejected
LANDING_FILES = 5

TENANT_COUNT = 10
_TENANT_NAMESPACE = uuid.UUID("0b5c8a52-3f4e-4d1a-9a77-2c1e6d9b4f10")


def tenant_id(index: int) -> str:
    """Deterministic lowercase UUID of tenant ``index`` (0..9): the same in every run."""
    return str(uuid.uuid5(_TENANT_NAMESPACE, f"e2e-tenant-{index}"))


# The fixed product/schema set, as in docs/event-forwarding-assignment.md.
PRODUCT_SCHEMAS = (("sales", "purchase"), ("security", "alert"), ("identity", "login"))

BOOTSTRAP_SERVERS = f"{os.environ.get('KAFKA_TEST_HOST', 'localhost')}:{os.environ.get('KAFKA_TEST_PORT', '9092')}"
DELIVERY_TOPIC = "delivery.file"
DLQ_TOPIC = "delivery.file.dlq"


def run_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "config": run_dir / "config.yaml",
        "expected": run_dir / "expected.json",
        "landing": run_dir / "landing",
        "ckpt_processing": run_dir / "ckpt-processing",
        "ckpt_sink": run_dir / "ckpt-sink",
        "out": run_dir / "out",
        "audit": run_dir / "audit",
        "rejected": run_dir / "rejected",
    }
