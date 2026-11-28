"""Logging helpers shared by the processing job, the File_Sink_Worker and the CLIs.

Library modules only get a logger. Handlers are configured once, by the CLIs (``configure_logging``)
or by pytest (``log_cli`` in pytest.ini). Log lines carry ids, paths and counters, never event
payloads or secret values (same rule as the Audit_Log).
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path

LOGGER_ROOT = "event_forwarding"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Logger under the package root, for example ``event_forwarding.pipeline``."""
    short = name.rsplit(".", 1)[-1]
    return logging.getLogger(f"{LOGGER_ROOT}.{short}")


def configure_logging(level: int = logging.INFO) -> None:
    """Send logs to stderr (no-op for handlers when the host, such as pytest, already set some).

    Quiets the py4j bridge, which logs every Python/JVM call at INFO.
    """
    logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT, stream=sys.stderr)
    logging.getLogger(LOGGER_ROOT).setLevel(level)
    logging.getLogger("py4j").setLevel(logging.WARNING)


def remove_staging_dir(path: Path) -> None:
    """Delete a Spark staging directory, and warn when anything is left behind.

    Cleanup failures do not fail the batch (the final files are already in place), but they must
    not be silent: on Windows, paths over 260 characters cannot be deleted without long-path support.
    """
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        get_logger("staging").warning("staging directory not fully removed (path too long or locked?): %s", path)


def read_total_counters(summary_dir: str | Path) -> dict | None:
    """Counters of the ``scope = "total"`` row of a written summary, or None when absent.

    The summary is a small aggregate file written by Spark (one row per tenant and destination
    plus the total). Reading it on the driver reads counters, never event rows.
    """
    directory = Path(summary_dir)
    if not directory.is_dir():
        return None
    for part in sorted(directory.glob("part-*.json")):
        for line in part.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("scope") == "total":
                return {k: v for k, v in row.items() if k not in ("scope", "tenant_id", "destination_id")}
    return None
